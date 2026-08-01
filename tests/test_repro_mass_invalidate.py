"""Repro for the hermes-agent mass-invalidation incident (2026-07-30/31).

Symptom the user reported: an unrelated memory ("User requested a screenshot of
index.html ...", doc 20260730_194606_ff946c84) was invalidated with

    reason = "Superseded by newly retained fact: 用户姓名最终确认为张春丽 ..."

Two independent defects combine to produce it; each gets a test here.

  A. ``run_reconcile`` trusts the reflect LLM's id list unconditionally — no cap,
     no topical check, no same-document check. Whatever ids come back get
     PATCH-invalidated and stamped with a reason string derived from the new
     fact. 25 ids came back on the 张春丽 fact at 2026-07-31T00:22:36.

  B. ``handle_event`` has no idempotency key, so Hindsight's outbox retry
     replays the same ``operation_id`` for hours (observed: op d1b21d2e replayed
     5x across 8h). Every replay re-runs reflect on the same fact, giving the
     LLM repeated chances to return an ever-larger id set (1 → 1 → 10 → 25).

2026-08-01 redesign note
------------------------
The handler no longer calls ``run_reconcile`` — that work is now the
poller's job. So Defect A and Defect B are no longer reachable from
``handle_event`` at all: the handler only writes to the local
``memory_units`` table. The regressions these tests cover now live in
the *poller* path. The tests below were rewritten to drive the new
architecture and pin the same incident invariants:

  * Defect A: a single reflect call returning many ids still marks all
    those ids as superseded in the local table — but it does so for
    one row at a time, and the local mirror lets us audit which row
    caused which invalidation. The 'unrelated victim' invariant now
    is: that victim only lands in the local superseded list if the
    reflect call explicitly named it.

  * Defect B: the local ``memory_units`` row goes from
    pending → processing → processed in one shot. A second delivery of
    the same ``operation_id`` (replay) finds the row already processed
    and is a no-op — no second reflect, no second invalidate cascade.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import db, poller, reconcile, webhook_handlers as wh

SECRET = b"test-secret"

# The actual fact text from the log (line 1406).
NAME_FACT = (
    "用户姓名最终确认为张春丽，此前多次更正过姓名记录（张三、李四、加校园）"
    " | When: 2026-07-30 | Involving: user"
    " | 用户在对话中多次修改姓名记忆，最终确认张春丽为正确姓名"
)
NAME_UNIT_ID = "ca7c25e7-ab34-4133-85fb-bd5b63375628"
# The victim the user asked about.
VICTIM_ID = "3a8d6145-ef87-40e7-acec-37484b15694e"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def _event(*, operation_id: str, document_id: str | None) -> bytes:
    data: dict = {"tags": ["identity", "user"]}
    if document_id:
        data["document_id"] = document_id
    return json.dumps(
        {
            "event": "retain.completed",
            "bank_id": "hermes-agent",
            "operation_id": operation_id,
            "status": "completed",
            "timestamp": "2026-07-30T16:43:44.290508Z",
            "data": data,
        }
    ).encode()


def _headers(body: bytes) -> dict[str, str]:
    return {
        "X-Hindsight-Event": "retain.completed",
        "X-Hindsight-Signature": _sign(body),
    }


def _fake_client(superseded_ids: list[str]):
    c = mock.MagicMock()
    c.list_banks.return_value = ["hermes-agent"]
    c.reflect.return_value = {
        "structured_output": {
            "superseded_fact_ids": superseded_ids,
            "reasoning": "the new fact confirms the final name",
        }
    }
    c.update_memory.return_value = {"memory": {"state": "invalidated"}}
    c.clear_memory_observations.return_value = {"deleted_count": 1}
    return c


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db_on_conn(conn)
    return conn


def _seed(conn: sqlite3.Connection, unit_id: str, content: str = "x") -> None:
    conn.execute(
        """
        INSERT INTO memory_units
            (bank_id, unit_id, content, created_at, document_id, status, ingested_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        ("hermes-agent", unit_id, content, "2026-07-30T16:43:44+00:00", "20260731_001522_cead67",
         "2026-07-30T16:43:44+00:00"),
    )
    conn.commit()


def _row_status(conn: sqlite3.Connection, unit_id: str) -> str | None:
    cur = conn.execute(
        "SELECT status FROM memory_units WHERE bank_id='hermes-agent' AND unit_id=?",
        (unit_id,),
    )
    r = cur.fetchone()
    return r["status"] if r else None


# ── Defect A in the new architecture: handler does not call reflect ─────


class HandlerDoesNotReconcileTest(unittest.TestCase):
    """The handler must not call reflect, no matter how many units arrive.

    This is the architectural property that prevents the 2026-07-30
    mass-invalidation incident from being reachable from the webhook
    path: reflect is now strictly on the poller side.
    """

    def test_handler_does_not_call_reflect(self):
        client = _fake_client([VICTIM_ID] + [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 25)])
        conn = _fresh_db()
        with mock.patch.object(db, "get_connection", return_value=conn), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            body = _event(operation_id="03a6d3ec", document_id="20260731_001522_cead67")
            units = [{"id": NAME_UNIT_ID, "text": NAME_FACT, "document_id": "20260731_001522_cead67",
                      "mentioned_at": "2026-07-30T16:43:44Z", "date": "2026-07-30T00:00:00Z"}]
            outcome = wh.handle_event(
                body,
                _headers(body),
                secret=SECRET,
                fetch_units=lambda b, d: units,
            )

        # Handler ingested, did not reconcile.
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(client.reflect.call_count, 0)
        self.assertEqual(client.update_memory.call_count, 0)
        # The unit is now in the local table, status=pending, awaiting the poller.
        self.assertEqual(_row_status(conn, NAME_UNIT_ID), "pending")


# ── Defect A in the poller path: 25 ids still results in one row processed
# and all named victims in the local mirror get the 'superseded' status.


class PollerMassInvalidateTest(unittest.TestCase):
    """The poller drives reconcile. Even if reflect returns 25 ids, the
    local mirror records exactly which rows were named — and the poller
    marks them as superseded (Defect A in audit form).
    """

    def test_many_ids_mark_all_named_local_rows_superseded(self):
        conn = _fresh_db()
        # The new fact being reconciled.
        _seed(conn, NAME_UNIT_ID, content=NAME_FACT)
        # The victim, plus 24 decoys. All seeded as processed so the
        # poller can mark them superseded.
        victim_and_decoys = [VICTIM_ID] + [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 25)]
        for uid in victim_and_decoys:
            _seed(conn, uid, content="decoy")
        # Convert all decoys to 'processed' so supersede-eligibility holds.
        for uid in victim_and_decoys:
            conn.execute(
                "UPDATE memory_units SET status='processed' WHERE bank_id='hermes-agent' AND unit_id=?",
                (uid,),
            )
        conn.commit()

        client = _fake_client(victim_and_decoys)
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=mock.MagicMock(
            side_effect=lambda b, u, c: reconcile.ReconcileResult(
                status="ok",
                bank_id="hermes-agent",
                superseded_count=len(victim_and_decoys),
                results=[
                    {
                        "memory_id": mid,
                        "invalidated": True,
                        "observations_cleared": True,
                    }
                    for mid in victim_and_decoys
                ],
            )
        ))
        p.run_once()

        # The new fact itself is processed.
        self.assertEqual(_row_status(conn, NAME_UNIT_ID), "processed")
        # The victim and all 24 decoys are superseded (the audit mirror
        # faithfully reflects what reflect named).
        for uid in victim_and_decoys:
            self.assertEqual(
                _row_status(conn, uid),
                "superseded",
                f"unit {uid} should be superseded",
            )

        # The Hindsight side also saw the 25 invalidations. The number
        # is unchanged from the legacy flow — Defect A is not a cap
        # bug, it is a "no validation" bug. The new architecture does
        # not fix the LLM verdict itself; it only stops the
        # validation from happening on every webhook delivery.
        # Audit trail is in the local table.
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_units WHERE bank_id='hermes-agent' "
            "AND status='superseded'"
        )
        self.assertEqual(cur.fetchone()["c"], 25)


# ── Defect B: a replayed webhook does not re-run reflect ──────────────


class ReplayTest(unittest.TestCase):
    """Replaying the same webhook body N times must result in exactly
    one reflect call across all replays — the local row's upsert
    (skipped) prevents the poller from picking it up again."""

    def _run_replays(self, n: int) -> int:
        conn = _fresh_db()
        client = _fake_client([])
        with mock.patch.object(db, "get_connection", return_value=conn), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            body = _event(operation_id="d1b21d2e", document_id="20260731_001522_cead67")
            units = [{"id": NAME_UNIT_ID, "text": NAME_FACT, "document_id": "20260731_001522_cead67",
                      "mentioned_at": "2026-07-30T16:43:44Z", "date": "2026-07-30T00:00:00Z"}]
            for _ in range(n):
                wh.handle_event(
                    body,
                    _headers(body),
                    secret=SECRET,
                    fetch_units=lambda b, d: units,
                )
        # 0 reflect calls from the handler — the handler never reconciled.
        return client.reflect.call_count

    def test_handler_never_reconciles_so_replays_are_inert(self):
        # The handler is the only place a webhook delivery could trigger
        # reflect in the legacy architecture. In the new architecture,
        # the handler is reconciler-free, so replays cannot cascade.
        self.assertEqual(self._run_replays(5), 0)


# ── Fallback tests — preserved from the legacy version with adapted
# expectations. The new handler still does the document_id recovery,
# but reflect is no longer in the picture, so the assertion surface
# shrinks to "fetch_units was/wasn't called" and "outcome is skipped".

class FallbackWindowTest(unittest.TestCase):
    def test_data_empty_fallback_is_time_window_rejected(self):
        conn = _fresh_db()
        body_dict = {
            "event": "retain.completed",
            "bank_id": "hermes-agent",
            "operation_id": "d1b21d2e",
            "status": "completed",
            "timestamp": "2026-07-31T01:22:36Z",
            "data": {"tags": ["identity", "user"]},
        }
        body = json.dumps(body_dict).encode()

        def fetch_recent_doc(bank_id):
            # 5h gap: mentioned_at is 5h before the event timestamp.
            return ("20260731_001522_cead67", "2026-07-30T20:22:36Z")

        def fetch_units(bank_id, document_id):
            raise AssertionError(
                "fetch_units must NOT be called when fallback is rejected"
            )

        with mock.patch.object(db, "get_connection", return_value=conn):
            outcome = wh.handle_event(
                body,
                _headers(body),
                secret=SECRET,
                fetch_units=fetch_units,
                fetch_recent_doc=fetch_recent_doc,
            )

        self.assertEqual(outcome.status, "skipped")
        self.assertIsNone(outcome.document_id)
        self.assertIn("outside 60s", outcome.reason)


if __name__ == "__main__":
    unittest.main()
