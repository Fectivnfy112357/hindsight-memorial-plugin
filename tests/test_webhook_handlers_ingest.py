"""Tests for the new ``handle_event`` contract: ingest-only, no reconcile.

The 2026-08-01 redesign moves reconcile out of the webhook path. The
handler is now responsible for:

  1. Signature verification.
  2. Event parsing.
  3. (Optional) recovering a missing ``document_id`` via the recent-units
     fallback bounded by the 60s window.
  4. Fetching the document's memory units from Hindsight.
  5. Upserting each unit into the local ``memory_units`` table.
  6. Returning 200 immediately.

Reflect + curate are no longer part of the webhook path. They run on the
poller thread, which picks up the just-ingested rows by status. Tests
that want to exercise the reconcile logic should go through
``test_reconcile_new_signature`` and ``test_poller`` instead.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import db, webhook_handlers as wh


SECRET = b"test-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def _retain_event(
    *,
    bank_id: str = "hindsight-memorial",
    document_id: str = "doc-abc",
    memory_unit_count: int = 1,
    event: str = "retain.completed",
    timestamp: str = "2026-07-30T07:30:00Z",
) -> bytes:
    payload = {
        "event": event,
        "bank_id": bank_id,
        "operation_id": "op-123",
        "status": "completed",
        "timestamp": timestamp,
        "data": {
            "document_id": document_id,
            "tags": ["auto"],
            "memory_unit_count": memory_unit_count,
        },
    }
    return json.dumps(payload).encode("utf-8")


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db_on_conn(conn)
    return conn


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM memory_units ORDER BY id").fetchall())


def _unit(unit_id: str, text: str = "fact", **extra: Any) -> dict[str, Any]:
    """Build a unit dict shaped like Hindsight's ``/memories/list`` response."""
    base = {
        "id": unit_id,
        "text": text,
        "document_id": "doc-abc",
        "mentioned_at": "2026-07-30T07:30:00Z",
        "date": "2026-07-30T00:00:00Z",
    }
    base.update(extra)
    return base


# Patch the db module's "module-level connection" used by the handler.
# The handler reads from ``hindsight_memorial.db`` directly, so we swap
# in our in-memory connection for the duration of each test.
class _DbSwap:
    """Context manager that points the module at a test-local connection."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        self._patcher = mock.patch.object(db, "upsert_unit_on_conn", wraps=db.upsert_unit_on_conn)
        # Wrap so the calls still hit the test conn, but we can also
        # assert call counts via mock_calls.
        self._patcher.start()
        # The handler is expected to call db.upsert_unit (no _on_conn
        # suffix) — we will change the handler to use a module-level
        # conn lookup. For now, expose the conn via a side channel.
        wh._TEST_CONN = self._conn
        return self._conn

    def __exit__(self, *exc):
        self._patcher.stop()
        wh._TEST_CONN = None


def _patch_db(conn: sqlite3.Connection):
    """Replace the module-level connection that the handler reads from."""
    wh._TEST_CONN = conn
    return conn


def _unpatch_db():
    wh._TEST_CONN = None


def _make_db_getter(conn: sqlite3.Connection):
    """Build a context manager that patches ``db.get_connection`` to
    return the test conn for the duration of the test."""
    return mock.patch.object(db, "get_connection", return_value=conn)


# ── ingest behaviour ───────────────────────────────────────────────────


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        self._db_patcher = _make_db_getter(self.conn)
        self._db_patcher.start()
        self.addCleanup(self._db_patcher.stop)

    def _headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Hindsight-Event": "retain.completed",
            "X-Hindsight-Signature": _sign(body),
        }

    def _run(self, *, body: bytes, units, env=None):
        if env:
            with mock.patch.dict(os.environ, env, clear=False):
                return wh.handle_event(
                    body,
                    self._headers(body),
                    secret=SECRET,
                    fetch_units=lambda b, d: units,
                )
        return wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=lambda b, d: units,
        )

    def test_ingests_one_unit_as_pending(self):
        body = _retain_event()
        units = [_unit("11111111-1111-1111-1111-111111111111", "user moved to Shenzhen")]
        outcome = self._run(body=body, units=units)
        self.assertEqual(outcome.status, "ok")
        rows = _rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["bank_id"], "hindsight-memorial")
        self.assertEqual(rows[0]["content"], "user moved to Shenzhen")
        self.assertEqual(rows[0]["unit_id"], "11111111-1111-1111-1111-111111111111")
        # created_at is taken from mentioned_at (preferred) — not from
        # local time.
        self.assertTrue(rows[0]["created_at"].startswith("2026-07-30T07:30"))

    def test_ingests_multiple_units_in_one_call(self):
        body = _retain_event(memory_unit_count=3)
        units = [
            _unit(f"{i}{i}{i}{i}{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}-{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}{i}", f"fact {i}")
            for i in range(1, 4)
        ]
        # Make them valid UUIDs.
        units = [
            _unit("11111111-1111-1111-1111-111111111111", "fact 1"),
            _unit("22222222-2222-2222-2222-222222222222", "fact 2"),
            _unit("33333333-3333-3333-3333-333333333333", "fact 3"),
        ]
        outcome = self._run(body=body, units=units)
        self.assertEqual(outcome.status, "ok")
        rows = _rows(self.conn)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "pending" for r in rows))

    def test_replay_does_not_re_insert(self):
        """Same webhook body delivered twice → second call should not
        create a new row for the same (bank_id, unit_id)."""
        body = _retain_event()
        units = [_unit("11111111-1111-1111-1111-111111111111", "fact 1")]
        first = self._run(body=body, units=units)
        self.assertEqual(first.status, "ok")
        # Mark the existing row 'processed' to simulate the poller
        # having drained it.
        self.conn.execute(
            "UPDATE memory_units SET status='processed' "
            "WHERE bank_id='hindsight-memorial'"
        )
        self.conn.commit()

        outcome = self._run(body=body, units=units)
        self.assertEqual(outcome.status, "ok")
        rows = _rows(self.conn)
        # Still exactly one row; the second ingest was a no-op.
        self.assertEqual(len(rows), 1)
        # And the row's status was preserved (not reset to pending),
        # because the content matched.
        self.assertEqual(rows[0]["status"], "processed")

    def test_content_change_resets_to_pending(self):
        body = _retain_event()
        units1 = [_unit("11111111-1111-1111-1111-111111111111", "old fact")]
        self._run(body=body, units=units1)
        # Mark it processed.
        self.conn.execute(
            "UPDATE memory_units SET status='processed' "
            "WHERE bank_id='hindsight-memorial'"
        )
        self.conn.commit()
        # Now Hindsight sends the same unit_id with different text.
        units2 = [_unit("11111111-1111-1111-1111-111111111111", "old fact (revised)")]
        self._run(body=body, units=units2)
        rows = _rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "old fact (revised)")
        self.assertEqual(rows[0]["status"], "pending")

    def test_uses_mentioned_at_then_date_for_created_at(self):
        body = _retain_event()
        # Unit has mentioned_at → that wins.
        units = [
            _unit(
                "11111111-1111-1111-1111-111111111111",
                mentioned_at="2026-08-15T12:00:00Z",
                date="2026-08-10T00:00:00Z",
            )
        ]
        self._run(body=body, units=units)
        row = _rows(self.conn)[0]
        self.assertTrue(row["created_at"].startswith("2026-08-15T12"))

    def test_falls_back_to_date_when_mentioned_at_missing(self):
        body = _retain_event()
        units = [
            _unit(
                "11111111-1111-1111-1111-111111111111",
                mentioned_at=None,
                date="2026-08-10T00:00:00Z",
            )
        ]
        self._run(body=body, units=units)
        row = _rows(self.conn)[0]
        self.assertTrue(row["created_at"].startswith("2026-08-10"))


# ── signature / malformed input ────────────────────────────────────────


class RejectTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        self._db_patcher = _make_db_getter(self.conn)
        self._db_patcher.start()
        self.addCleanup(self._db_patcher.stop)

    def _headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Hindsight-Event": "retain.completed",
            "X-Hindsight-Signature": _sign(body),
        }

    def test_bad_signature_does_not_ingest(self):
        body = _retain_event()
        headers = self._headers(body)
        headers["X-Hindsight-Signature"] = "sha256=" + ("0" * 64)
        outcome = wh.handle_event(
            body, headers, secret=SECRET, fetch_units=lambda b, d: []
        )
        self.assertEqual(outcome.status, "ignored")
        self.assertEqual(_rows(self.conn), [])

    def test_non_retain_event_does_not_ingest(self):
        body = _retain_event(event="consolidation.completed")
        outcome = wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=lambda b, d: [],
        )
        self.assertEqual(outcome.status, "ignored")
        self.assertEqual(_rows(self.conn), [])

    def test_no_units_does_not_ingest(self):
        body = _retain_event(memory_unit_count=2)
        outcome = wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=lambda b, d: [],
        )
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(_rows(self.conn), [])


# ── fallback path ──────────────────────────────────────────────────────


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.conn = _fresh_db()
        self._db_patcher = _make_db_getter(self.conn)
        self._db_patcher.start()
        self.addCleanup(self._db_patcher.stop)

    def _headers(self, body: bytes) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Hindsight-Event": "retain.completed",
            "X-Hindsight-Signature": _sign(body),
        }

    def _body_no_doc_id(self, *, timestamp: str = "2026-07-30T07:30:00Z") -> bytes:
        return json.dumps(
            {
                "event": "retain.completed",
                "bank_id": "hindsight-memorial",
                "operation_id": "op-1",
                "status": "completed",
                "timestamp": timestamp,
                "data": {"memory_unit_count": 1},
            }
        ).encode()

    def test_fallback_finds_recent_unit_and_ingests(self):
        body = self._body_no_doc_id()
        recent_unit = _unit(
            "99999999-9999-9999-9999-999999999999",
            "recent",
            document_id="doc-recovered",
            mentioned_at="2026-07-30T07:30:00Z",
        )

        def fetch_recent_doc(bank_id):
            return ("doc-recovered", "2026-07-30T07:30:00Z")

        def fetch_units(bank_id, document_id):
            self.assertEqual(document_id, "doc-recovered")
            return [_unit(
                "11111111-1111-1111-1111-111111111111",
                "recovered fact",
                document_id="doc-recovered",
            )]

        outcome = wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=fetch_units,
            fetch_recent_doc=fetch_recent_doc,
        )
        self.assertEqual(outcome.status, "ok")
        rows = _rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(rows[0]["document_id"], "doc-recovered")

    def test_fallback_rejected_outside_window_does_not_ingest(self):
        body = self._body_no_doc_id(timestamp="2026-07-30T07:30:00Z")

        def fetch_recent_doc(bank_id):
            # Recovered unit's timestamp is 5 hours before the event —
            # way outside the 60s window.
            return ("doc-stale", "2026-07-30T02:30:00Z")

        def fetch_units(bank_id, document_id):
            raise AssertionError(
                "fetch_units must NOT be called when fallback is rejected"
            )

        outcome = wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=fetch_units,
            fetch_recent_doc=fetch_recent_doc,
        )
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(_rows(self.conn), [])

    def test_no_fallback_when_docid_present(self):
        """The fallback path must not run when the payload already has
        a document_id — the explicit path is always preferred."""
        body = _retain_event(document_id="doc-explicit")
        units = [_unit(
            "11111111-1111-1111-1111-111111111111",
            "explicit",
            document_id="doc-explicit",
        )]

        def fetch_recent_doc(bank_id):
            raise AssertionError("fetch_recent_doc must not be called when docid is present")

        outcome = wh.handle_event(
            body,
            self._headers(body),
            secret=SECRET,
            fetch_units=lambda b, d: units,
            fetch_recent_doc=fetch_recent_doc,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(_rows(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
