"""Unit tests for ``hindsight_memorial.poller.ReconcilerPoller``.

The poller is the slow half of the new architecture: it drains the local
``memory_units`` table by picking the most-recent pending row, running
``run_reconcile`` against it, and then either marking it processed (on
success), superseded (if reflect found an id we already have), or failed
(on exception). These tests drive the same code path via ``run_once``
(the synchronous per-iteration entry point) so the daemon thread itself
is not exercised in the unit test — that part is covered by the
integration smoke in task #18.
"""
from __future__ import annotations

import sqlite3
import sys
import time
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import db, poller
from hindsight_memorial.curate import CurateResult
from hindsight_memorial.reconcile import ReconcileResult


# ── helpers ─────────────────────────────────────────────────────────────


def _fresh_db() -> sqlite3.Connection:
    """Build a brand-new in-memory DB with the schema applied. The
    connection is marked ``check_same_thread=False`` so it can be used
    by the poller worker thread (the SQLite default refuses to share
    a connection across threads)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db_on_conn(conn)
    return conn


def _seed_pending(
    conn: sqlite3.Connection,
    unit_id: str,
    *,
    bank_id: str = "b1",
    content: str = "fact A",
    created_at: str = "2026-08-01T00:00:00+00:00",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO memory_units
            (bank_id, unit_id, content, created_at, document_id, status, ingested_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (bank_id, unit_id, content, created_at, "doc-1", "2026-08-01T00:00:00+00:00"),
    )
    conn.commit()
    return cur.lastrowid


def _row_status(conn: sqlite3.Connection, unit_id: str) -> str | None:
    cur = conn.execute(
        "SELECT status FROM memory_units WHERE bank_id='b1' AND unit_id=?", (unit_id,)
    )
    r = cur.fetchone()
    return r["status"] if r else None


# ── main processing path ───────────────────────────────────────────────


class ProcessOneTest(unittest.TestCase):
    def test_processes_one_pending_row(self):
        conn = _fresh_db()
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111")

        fake_result = ReconcileResult(status="abandoned", bank_id="b1")
        fake_reconcile = mock.Mock(return_value=fake_result)
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)

        processed = p.run_once()
        self.assertTrue(processed)
        self.assertEqual(_row_status(conn, "11111111-1111-1111-1111-111111111111"), "processed")
        fake_reconcile.assert_called_once()
        # First positional arg of run_reconcile is bank_id; second is unit_id.
        call = fake_reconcile.call_args
        self.assertEqual(call.args[0], "b1")
        self.assertEqual(call.args[1], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(call.args[2], "fact A")

    def test_returns_false_when_no_pending(self):
        conn = _fresh_db()
        fake_reconcile = mock.Mock()
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        self.assertFalse(p.run_once())
        fake_reconcile.assert_not_called()

    def test_marks_failed_when_reconcile_raises(self):
        conn = _fresh_db()
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111")
        fake_reconcile = mock.Mock(side_effect=RuntimeError("reflect timeout"))
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        processed = p.run_once()
        self.assertTrue(processed)
        self.assertEqual(_row_status(conn, "11111111-1111-1111-1111-111111111111"), "failed")
        # failure_reason recorded
        cur = conn.execute(
            "SELECT failure_reason FROM memory_units WHERE bank_id='b1' "
            "AND unit_id='11111111-1111-1111-1111-111111111111'"
        )
        self.assertIn("reflect timeout", cur.fetchone()["failure_reason"])

    def test_marks_failed_when_reconcile_returns_reflect_failed(self):
        conn = _fresh_db()
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111")
        fake_reconcile = mock.Mock(
            return_value=ReconcileResult(
                status="reflect_failed", error="API 500 boom", bank_id="b1"
            )
        )
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        p.run_once()
        self.assertEqual(_row_status(conn, "11111111-1111-1111-1111-111111111111"), "failed")
        cur = conn.execute(
            "SELECT failure_reason FROM memory_units WHERE bank_id='b1' "
            "AND unit_id='11111111-1111-1111-1111-111111111111'"
        )
        self.assertEqual(cur.fetchone()["failure_reason"], "API 500 boom")

    def test_picks_most_recent_pending_first(self):
        conn = _fresh_db()
        _seed_pending(
            conn,
            "11111111-1111-1111-1111-111111111111",
            created_at="2026-08-01T00:00:00+00:00",
            content="old",
        )
        _seed_pending(
            conn,
            "22222222-2222-2222-2222-222222222222",
            created_at="2026-08-03T00:00:00+00:00",
            content="newest",
        )
        _seed_pending(
            conn,
            "33333333-3333-3333-3333-333333333333",
            created_at="2026-08-02T00:00:00+00:00",
            content="middle",
        )
        seen: list[str] = []
        fake_reconcile = mock.Mock(
            side_effect=lambda bank_id, unit_id, content: (
                seen.append(unit_id),
                ReconcileResult(status="abandoned"),
            )[-1]
        )
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        # First call: newest created_at.
        p.run_once()
        self.assertEqual(seen[0], "22222222-2222-2222-2222-222222222222")
        p.run_once()
        self.assertEqual(seen[1], "33333333-3333-3333-3333-333333333333")
        p.run_once()
        self.assertEqual(seen[2], "11111111-1111-1111-1111-111111111111")
        # Fourth call: nothing left.
        self.assertFalse(p.run_once())

    def test_skips_already_processed_rows(self):
        conn = _fresh_db()
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111")
        conn.execute(
            "UPDATE memory_units SET status='processed' WHERE bank_id='b1' "
            "AND unit_id='11111111-1111-1111-1111-111111111111'"
        )
        conn.commit()
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=mock.Mock())
        self.assertFalse(p.run_once())


# ── supersede handling ─────────────────────────────────────────────────


class SupersedeTest(unittest.TestCase):
    def test_marks_local_rows_superseded(self):
        """When reflect returns ids of other local rows, those rows must
        be flipped to 'superseded' — they have been invalidated on the
        Hindsight side and the local mirror should reflect that."""
        conn = _fresh_db()
        # The row we will reconcile.
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111", content="new")
        # A row that the LLM will claim is superseded by 'new'.
        _seed_pending(
            conn,
            "22222222-2222-2222-2222-222222222222",
            created_at="2026-07-30T00:00:00+00:00",
            content="old fact",
        )
        fake_reconcile = mock.Mock(
            return_value=ReconcileResult(
                status="ok",
                bank_id="b1",
                superseded_count=1,
                results=[
                    {
                        "memory_id": "22222222-2222-2222-2222-222222222222",
                        "invalidated": True,
                        "observations_cleared": True,
                    }
                ],
            )
        )
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        p.run_once()

        # Self row is processed.
        self.assertEqual(
            _row_status(conn, "11111111-1111-1111-1111-111111111111"), "processed"
        )
        # The other row is superseded.
        self.assertEqual(
            _row_status(conn, "22222222-2222-2222-2222-222222222222"), "superseded"
        )
        # superseded_reason recorded.
        cur = conn.execute(
            "SELECT superseded_reason FROM memory_units WHERE bank_id='b1' "
            "AND unit_id='22222222-2222-2222-2222-222222222222'"
        )
        self.assertIsNotNone(cur.fetchone()["superseded_reason"])

    def test_does_not_touch_self_via_supersede(self):
        """The freshly retained unit must never be flipped to 'superseded'
        by its own reflect verdict (defence in depth: run_reconcile already
        excludes self, but if the LLM bypasses that we still must not
        damage the row currently in 'processing' state)."""
        conn = _fresh_db()
        self_id = "11111111-1111-1111-1111-111111111111"
        _seed_pending(conn, self_id, content="new fact")
        fake_reconcile = mock.Mock(
            return_value=ReconcileResult(
                status="ok",
                bank_id="b1",
                superseded_count=1,
                # The 'supersede' target is the row itself — pathological.
                results=[
                    {
                        "memory_id": self_id,
                        "invalidated": True,
                        "observations_cleared": True,
                    }
                ],
            )
        )
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=fake_reconcile)
        p.run_once()
        # Self is processed (its own mark_superseded call must not have
        # moved it out of 'processing' before we wrote 'processed').
        self.assertEqual(_row_status(conn, self_id), "processed")


# ── lifecycle ──────────────────────────────────────────────────────────


class LifecycleTest(unittest.TestCase):
    def test_start_then_stop_joins_thread(self):
        conn = _fresh_db()
        p = poller.ReconcilerPoller(
            conn=conn,
            run_reconcile=mock.Mock(),
            poll_interval_sec=0.01,
        )
        p.start()
        try:
            self.assertTrue(p.is_alive())
        finally:
            p.stop(timeout=2.0)
        self.assertFalse(p.is_alive())

    def test_start_is_idempotent(self):
        """Two start() calls must not spawn a second thread."""
        conn = _fresh_db()
        p = poller.ReconcilerPoller(
            conn=conn,
            run_reconcile=mock.Mock(),
            poll_interval_sec=0.01,
        )
        p.start()
        first_thread = p._thread
        p.start()
        second_thread = p._thread
        try:
            self.assertIs(first_thread, second_thread)
        finally:
            p.stop(timeout=2.0)

    def test_stop_is_safe_before_start(self):
        conn = _fresh_db()
        p = poller.ReconcilerPoller(conn=conn, run_reconcile=mock.Mock())
        p.stop()  # must not raise

    def test_stop_is_safe_after_stop(self):
        conn = _fresh_db()
        p = poller.ReconcilerPoller(
            conn=conn, run_reconcile=mock.Mock(), poll_interval_sec=0.01
        )
        p.start()
        p.stop(timeout=2.0)
        p.stop()  # must not raise

    def test_restart_drains_pending_rows_left_by_previous_run(self):
        """The architectural property that makes the system survive a
        container restart: rows the previous poller didn't get to
        (status='pending') are picked up by a freshly started poller
        as if nothing happened. This is the 2026-08-01 design doc
        §13 verification point #5."""
        conn = _fresh_db()
        # Seed 2 pending rows that no poller has ever touched.
        _seed_pending(conn, "11111111-1111-1111-1111-111111111111", content="fact A")
        _seed_pending(conn, "22222222-2222-2222-2222-222222222222", content="fact B")

        # "Previous run" — process one row, then crash (stop hard).
        first = poller.ReconcilerPoller(
            conn=conn,
            run_reconcile=mock.Mock(
                return_value=ReconcileResult(status="abandoned", bank_id="b1")
            ),
            poll_interval_sec=0.01,
        )
        first.start()
        # Wait until at least one row is processed (or 2 s passes).
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            cur = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_units WHERE status='pending'"
            )
            if cur.fetchone()["c"] <= 1:
                break
            time.sleep(0.02)
        first.stop(timeout=2.0)

        # Restart the poller. It must pick up the surviving pending row.
        second = poller.ReconcilerPoller(
            conn=conn,
            run_reconcile=mock.Mock(
                return_value=ReconcileResult(status="abandoned", bank_id="b1")
            ),
            poll_interval_sec=0.01,
        )
        second.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                cur = conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_units WHERE status='pending'"
                )
                if cur.fetchone()["c"] == 0:
                    break
                time.sleep(0.02)
            cur = conn.execute(
                "SELECT status, COUNT(*) AS c FROM memory_units GROUP BY status"
            )
            counts = {row["status"]: row["c"] for row in cur.fetchall()}
            self.assertEqual(counts.get("pending", 0), 0)
            self.assertEqual(counts.get("processed", 0), 2)
        finally:
            second.stop(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
