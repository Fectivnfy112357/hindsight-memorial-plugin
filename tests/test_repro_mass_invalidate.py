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
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from hindsight_memorial import webhook_handlers as wh

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


class MassInvalidateTest(unittest.TestCase):
    """Defect A: unbounded, unvalidated trust in the reflect id list."""

    def test_unrelated_ids_are_invalidated_with_name_fact_reason(self):
        # reflect returns 25 ids, as it did at 00:22:36. One of them is the
        # index.html screenshot fact the user asked about; it lives in a
        # different document and shares no topic with the name fact.
        ids = [VICTIM_ID] + [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 25)]
        client = _fake_client(ids)

        body = _event(operation_id="03a6d3ec", document_id="20260731_001522_cead67")
        units = [{"id": NAME_UNIT_ID, "text": NAME_FACT}]

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            outcome = wh.handle_event(
                body,
                _headers(body),
                secret=SECRET,
                fetch_units=lambda b, d: units,
            )

        self.assertEqual(outcome.status, "ok")
        # All 25 were invalidated. No cap, no sanity check.
        self.assertEqual(outcome.total_superseded, 25)
        self.assertEqual(client.update_memory.call_count, 25)

        # And the victim carries a reason string about the name change.
        patched = {
            call.args[1]: call.kwargs["reason"]
            for call in client.update_memory.call_args_list
        }
        self.assertIn(VICTIM_ID, patched)
        self.assertIn("张春丽", patched[VICTIM_ID])
        self.assertTrue(patched[VICTIM_ID].startswith("Superseded by newly retained fact:"))


class ReplayTest(unittest.TestCase):
    """Defect B: no idempotency — the same operation_id reconciles every time."""

    def test_same_operation_id_reconciles_on_every_replay(self):
        client = _fake_client([])
        body = _event(operation_id="d1b21d2e", document_id="20260731_001522_cead67")
        units = [{"id": NAME_UNIT_ID, "text": NAME_FACT}]

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            for _ in range(5):  # the 5 replays observed in the log
                wh.handle_event(
                    body,
                    _headers(body),
                    secret=SECRET,
                    fetch_units=lambda b, d: units,
                )

        # Every replay pays for a fresh reflect. Each is a fresh chance for the
        # LLM to return a larger id set, and there is no dedup to stop it.
        self.assertEqual(client.reflect.call_count, 5)

    def test_data_empty_fallback_retargets_replay_at_newest_fact(self):
        """The data={} fallback makes replays actively dangerous.

        A stale replay of an *old* operation resolves its document via
        "whatever unit is newest in the bank right now" — so an 8-hour-old
        retry gets pointed at the current newest fact and re-reconciles it.
        """
        client = _fake_client([])
        body = _event(operation_id="d1b21d2e", document_id=None)  # data={}
        newest = [{"id": NAME_UNIT_ID, "text": NAME_FACT}]

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            outcome = wh.handle_event(
                body,
                _headers(body),
                secret=SECRET,
                fetch_units=lambda b, d: newest,
                fetch_recent_doc=lambda b: ("20260731_001522_cead67", "2026-07-30T16:43:44.290508Z"),
            )

        # The replayed event was silently retargeted at the name fact's document.
        self.assertEqual(outcome.document_id, "20260731_001522_cead67")
        self.assertEqual(client.reflect.call_count, 1)
        self.assertIn("张春丽", client.reflect.call_args[0][1])

    def test_data_empty_fallback_is_time_window_rejected(self):
        """5h-ladder replay: the event timestamp is fresh but the recovered
        unit's mentioned_at is hours old. The time-window guard added on
        2026-07-31 must drop this rather than retarget.

        This is the *closed* form of the test above: the regression the
        window check is built to catch.
        """
        client = _fake_client([])
        # Synthetic fresh event; the recovered unit sits 5h behind.
        body_dict = {
            "event": "retain.completed",
            "bank_id": "hermes-agent",
            "operation_id": "d1b21d2e",
            "status": "completed",
            "timestamp": "2026-07-31T01:22:36Z",
            "data": {"tags": ["identity", "user"]},
        }
        body = json.dumps(body_dict).encode()
        newest = [{"id": NAME_UNIT_ID, "text": NAME_FACT}]

        with mock.patch.dict(
            "os.environ", {"HINDSIGHT_API_URL": "http://api"}, clear=False
        ), mock.patch(
            "hindsight_memorial.reconcile.HindsightClient.from_memorial_config",
            return_value=client,
        ):
            outcome = wh.handle_event(
                body,
                _headers(body),
                secret=SECRET,
                fetch_units=lambda b, d: newest,
                # 5h gap: 'mentioned_at' 5h before event timestamp.
                fetch_recent_doc=lambda b: (
                    "20260731_001522_cead67",
                    "2026-07-30T20:22:36Z",
                ),
            )

        # Time-window guard fires: skip, never reaches reflect.
        self.assertEqual(outcome.status, "skipped")
        self.assertIsNone(outcome.document_id)
        self.assertEqual(client.reflect.call_count, 0)
        self.assertIn("outside 60s", outcome.reason)


if __name__ == "__main__":
    unittest.main()
