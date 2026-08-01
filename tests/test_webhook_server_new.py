"""Unit tests for the new ``webhook_server`` entry point.

The 2026-08-01 redesign removes the in-process Dispatcher. The server
is now: HTTP admission → call ``handle_event`` (which ingests to the
local DB) → return 200. There is no async worker, no dedup-by-body-hash,
and no in-memory queue — the local ``memory_units`` table IS the
deduplication boundary (one row per ``(bank_id, unit_id)``, see
``db.upsert_unit``).

The slow half (reflect + curate) now lives on the poller thread, which
the server's ``main()`` starts and stops alongside the HTTP server. The
``/healthz`` endpoint reports both the database row counts and the
poller's lifecycle state so an operator can tell at a glance whether
ingest is keeping up.
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

from hindsight_memorial import db, poller, webhook_server


def _signed_body(secret: bytes = b"test") -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event": "retain.completed",
            "bank_id": "bank-1",
            "operation_id": "op-1",
            "status": "completed",
            "timestamp": "2026-01-01T00:00:00Z",
            "data": {"document_id": "doc-1", "memory_unit_count": 1},
        }
    ).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, {
        "X-Hindsight-Signature": sig,
        "X-Hindsight-Event": "retain.completed",
    }


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db_on_conn(conn)
    return conn


# ── process_post: admission only ───────────────────────────────────────


class ProcessPostTest(unittest.TestCase):
    def test_valid_event_ingests_and_acks_200(self):
        body, headers = _signed_body()
        conn = _fresh_db()
        # The handler will call fetch_units(bank_id, document_id) which
        # in turn would hit Hindsight. We don't have a server; patch
        # out the factory so the handler sees a stubbed unit list.
        units = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "text": "hello",
                "document_id": "doc-1",
                "mentioned_at": "2026-01-01T00:00:00Z",
                "date": "2026-01-01T00:00:00Z",
            }
        ]
        with mock.patch.object(db, "get_connection", return_value=conn), \
             mock.patch.object(
                 webhook_server, "_make_fetch_units", return_value=lambda b, d: units
             ):
            payload, status = webhook_server._process_post(
                body, headers, secret=b"test"
            )
        self.assertEqual(status, 200)
        body_obj = json.loads(payload)
        self.assertEqual(body_obj["status"], "accepted")
        # The unit is in the local DB.
        cur = conn.execute("SELECT * FROM memory_units")
        rows = list(cur.fetchall())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit_id"], "11111111-1111-1111-1111-111111111111")

    def test_bad_signature_returns_200_ignored(self):
        body, headers = _signed_body()
        headers["X-Hindsight-Signature"] = "sha256=" + ("0" * 64)
        conn = _fresh_db()
        with mock.patch.object(db, "get_connection", return_value=conn):
            payload, status = webhook_server._process_post(
                body, headers, secret=b"test"
            )
        # 200 (not 401) — a retry cannot fix a bad signature; inviting
        # a retry just restarts the ladder. See design doc §6.2.
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "ignored")
        # Nothing was ingested.
        cur = conn.execute("SELECT * FROM memory_units")
        self.assertEqual(len(list(cur.fetchall())), 0)

    def test_no_dispatcher_needed(self):
        """The 2026-08-01 entry point takes no dispatcher argument —
        that is the architectural property that lets the dispatch
        code be deleted (see design doc §11)."""
        import inspect
        sig = inspect.signature(webhook_server._process_post)
        self.assertNotIn("dispatcher", sig.parameters)


# ── /healthz: db stats + poller state ──────────────────────────────────


class HealthzTest(unittest.TestCase):
    def test_healthz_reports_db_row_counts(self):
        conn = _fresh_db()
        # Seed a few rows in different states.
        for i, status in enumerate(["pending", "pending", "processed", "failed"]):
            conn.execute(
                """
                INSERT INTO memory_units
                    (bank_id, unit_id, content, created_at, document_id, status, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("b1", f"u{i}", f"c{i}", "2026-01-01T00:00:00+00:00", "d1", status, "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()
        # Build a poller that points at this conn (not started, so
        # is_alive() returns False).
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=mock.Mock())
        with mock.patch.object(db, "get_connection", return_value=conn):
            body, status = webhook_server._healthz(p)
        self.assertEqual(status, 200)
        body_obj = json.loads(body)
        self.assertEqual(body_obj["status"], "ok")
        self.assertEqual(body_obj["pending"], 2)
        self.assertEqual(body_obj["processed"], 1)
        self.assertEqual(body_obj["failed"], 1)
        self.assertEqual(body_obj["total"], 4)
        self.assertIn("poller_running", body_obj)

    def test_healthz_reports_poller_running(self):
        conn = _fresh_db()
        p = poller.ReconcilerPoller(
            conn=conn, run_reconcile=mock.Mock(), poll_interval_sec=0.01
        )
        p.start()
        try:
            with mock.patch.object(db, "get_connection", return_value=conn):
                body, status = webhook_server._healthz(p)
            body_obj = json.loads(body)
            self.assertTrue(body_obj["poller_running"])
        finally:
            p.stop(timeout=2.0)


# ── main() lifecycle ───────────────────────────────────────────────────


class MainLifecycleTest(unittest.TestCase):
    def test_main_starts_and_stops_poller(self):
        """``main()`` is what the production container runs. It must
        create the DB, start the poller, serve HTTP, and on shutdown
        stop the poller cleanly. The HTTP server itself is hard to
        drive in a unit test (it blocks), so we patch the
        ThreadingHTTPServer to a stub that records lifecycle calls."""
        fake_conn = mock.MagicMock()
        with mock.patch.object(db, "get_connection", return_value=fake_conn), \
             mock.patch.object(db, "init_db_on_conn") as mock_init, \
             mock.patch.object(
                 webhook_server.poller, "ReconcilerPoller"
             ) as PollerCls, mock.patch.object(
                 webhook_server, "ThreadingHTTPServer"
             ) as ServerCls, mock.patch.object(
                 webhook_server, "_resolve_secret", return_value=b"test"
             ):
            instance = PollerCls.return_value
            server = ServerCls.return_value
            # serve_forever returns immediately under the stub.
            server.serve_forever.return_value = None

            with mock.patch("sys.argv", ["webhook_server", "--port", "0"]):
                webhook_server.main()

        # Schema was applied.
        mock_init.assert_called_once_with(fake_conn)
        # Poller was constructed with the same fake conn, started, stopped.
        PollerCls.assert_called_once()
        poller_args = PollerCls.call_args.args
        self.assertIs(poller_args[0], fake_conn)
        instance.start.assert_called_once()
        instance.stop.assert_called_once()
        # The HTTP server was created with the right host/port.
        ServerCls.assert_called_once()
        call_args = ServerCls.call_args.args
        self.assertEqual(call_args[0], ("0.0.0.0", 0))
        # And it was asked to serve + close.
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
