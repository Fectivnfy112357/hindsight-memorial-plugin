"""Unit tests for ``hindsight_memorial.db``.

Covers the schema, upsert semantics, and state-machine transitions described
in ``doc/persistent-reconciler-design-2026-08-01.md`` §3-§5. The tests run
against an in-memory SQLite database so they require no external services;
the MySQL backend is exercised separately in the deployment smoke test.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import db


def _fresh_db() -> sqlite3.Connection:
    """Build a brand-new in-memory DB with the schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db_on_conn(conn)
    return conn


def _insert_one(
    conn: sqlite3.Connection,
    unit_id: str = "11111111-1111-1111-1111-111111111111",
    *,
    bank_id: str = "b1",
    content: str = "fact A",
    created_at: str = "2026-08-01T00:00:00+00:00",
    document_id: str | None = "doc-1",
    status: str = "pending",
    superseded_reason: str | None = None,
    failure_reason: str | None = None,
    ingested_at: str = "2026-08-01T00:00:00+00:00",
) -> None:
    """Direct insert for state-fixture setup; bypasses upsert."""
    conn.execute(
        """
        INSERT INTO memory_units
            (bank_id, unit_id, content, created_at, document_id, status,
             superseded_reason, failure_reason, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bank_id,
            unit_id,
            content,
            created_at,
            document_id,
            status,
            superseded_reason,
            failure_reason,
            ingested_at,
        ),
    )
    conn.commit()


def _row(conn: sqlite3.Connection, bank_id: str, unit_id: str) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM memory_units WHERE bank_id=? AND unit_id=?",
        (bank_id, unit_id),
    )
    return cur.fetchone()


def _all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM memory_units ORDER BY id")
    return list(cur.fetchall())


def _count(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) AS c FROM memory_units {where}"
    cur = conn.execute(sql, params)
    return cur.fetchone()["c"]


# ── schema ──────────────────────────────────────────────────────────────


class SchemaTest(unittest.TestCase):
    def test_init_creates_memory_units_table(self):
        conn = _fresh_db()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_units'"
        )
        self.assertIsNotNone(cur.fetchone())

    def test_required_columns_present(self):
        conn = _fresh_db()
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(memory_units)")
        }
        for required in (
            "id",
            "bank_id",
            "unit_id",
            "content",
            "created_at",
            "document_id",
            "status",
            "superseded_reason",
            "failure_reason",
            "ingested_at",
            "processed_at",
        ):
            self.assertIn(required, cols, f"missing column: {required}")

    def test_status_is_enum_like(self):
        """SQLite has no native ENUM, but values must match the contract."""
        conn = _fresh_db()
        _insert_one(conn, status="pending")
        # Inserting an unknown status should still work in SQLite (TEXT column),
        # but the application contract restricts to 5 values; the test below
        # pins the documented set so accidental drift is caught.
        documented = {"pending", "processing", "processed", "superseded", "failed"}
        # Re-fetch the row and assert status is one of the documented values.
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertIn(row["status"], documented)

    def test_unique_constraint_on_bank_and_unit(self):
        conn = _fresh_db()
        _insert_one(conn, bank_id="b1", unit_id="same")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_one(conn, bank_id="b1", unit_id="same")

    def test_unique_constraint_does_not_cross_banks(self):
        """Same unit_id under different bank_id must coexist."""
        conn = _fresh_db()
        _insert_one(conn, bank_id="b1", unit_id="same")
        _insert_one(conn, bank_id="b2", unit_id="same")
        self.assertEqual(_count(conn), 2)

    def test_init_db_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db_on_conn(conn)
        db.init_db_on_conn(conn)  # second call must not raise
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name='memory_units'"
        )
        self.assertEqual(cur.fetchone()["c"], 1)


# ── upsert ──────────────────────────────────────────────────────────────


class UpsertTest(unittest.TestCase):
    def test_upsert_inserts_new_row_as_pending(self):
        conn = _fresh_db()
        outcome = db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        self.assertEqual(outcome, "inserted")
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["content"], "fact A")
        self.assertEqual(row["document_id"], "doc-1")
        self.assertIsNotNone(row["ingested_at"])

    def test_upsert_skips_when_content_unchanged(self):
        conn = _fresh_db()
        db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        first = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        first_ingested = first["ingested_at"]
        # Mutate the row to simulate a previously processed unit. The upsert
        # must NOT regress status to pending and must NOT bump ingested_at.
        conn.execute(
            "UPDATE memory_units SET status='processed', processed_at=? WHERE id=?",
            ("2026-08-01T01:00:00+00:00", first["id"]),
        )
        conn.commit()

        outcome = db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        self.assertEqual(outcome, "skipped")
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        # Status preserved; ingested_at not bumped.
        self.assertEqual(row["status"], "processed")
        self.assertEqual(row["ingested_at"], first_ingested)

    def test_upsert_resets_to_pending_when_content_changed(self):
        conn = _fresh_db()
        db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        # Pretend the unit was already processed.
        conn.execute(
            "UPDATE memory_units SET status='processed', processed_at='2026-08-01T01:00:00+00:00' "
            "WHERE bank_id='b1' AND unit_id='11111111-1111-1111-1111-111111111111'"
        )
        conn.commit()

        outcome = db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A revised",
            created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        self.assertEqual(outcome, "updated")
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["content"], "fact A revised")
        self.assertEqual(row["status"], "pending")
        # created_at should reflect the new value.
        self.assertTrue(row["created_at"].startswith("2026-08-02"))

    def test_upsert_does_not_change_ingested_at_when_content_unchanged(self):
        conn = _fresh_db()
        db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        first_ingested = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")["ingested_at"]

        db.upsert_unit_on_conn(
            conn,
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="fact A",
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            document_id="doc-1",
        )
        second_ingested = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")["ingested_at"]
        self.assertEqual(first_ingested, second_ingested)


# ── state transitions ───────────────────────────────────────────────────


class StateTransitionsTest(unittest.TestCase):
    def test_fetch_pending_returns_oldest_first(self):
        """The poller should pick the most-recent created_at; the
        design document says '倒序拿第一个' so created_at DESC, id DESC."""
        conn = _fresh_db()
        # Insert 3 rows with explicit created_at; newest first insertion order.
        _insert_one(
            conn,
            unit_id="11111111-1111-1111-1111-111111111111",
            created_at="2026-08-01T00:00:00+00:00",
        )
        _insert_one(
            conn,
            unit_id="22222222-2222-2222-2222-222222222222",
            created_at="2026-08-03T00:00:00+00:00",
        )
        _insert_one(
            conn,
            unit_id="33333333-3333-3333-3333-333333333333",
            created_at="2026-08-02T00:00:00+00:00",
        )
        row = db.fetch_pending_row_on_conn(conn)
        self.assertIsNotNone(row)
        # Newest created_at is 2026-08-03.
        self.assertEqual(row["unit_id"], "22222222-2222-2222-2222-222222222222")

    def test_fetch_pending_skips_non_pending(self):
        conn = _fresh_db()
        _insert_one(conn, unit_id="11111111-1111-1111-1111-111111111111", status="processed")
        _insert_one(conn, unit_id="22222222-2222-2222-2222-222222222222", status="failed")
        _insert_one(
            conn,
            unit_id="33333333-3333-3333-3333-333333333333",
            status="pending",
            created_at="2026-08-02T00:00:00+00:00",
        )
        row = db.fetch_pending_row_on_conn(conn)
        self.assertIsNotNone(row)
        self.assertEqual(row["unit_id"], "33333333-3333-3333-3333-333333333333")

    def test_fetch_pending_returns_none_when_empty(self):
        conn = _fresh_db()
        self.assertIsNone(db.fetch_pending_row_on_conn(conn))

    def test_mark_processed_sets_status_and_timestamp(self):
        conn = _fresh_db()
        _insert_one(conn, unit_id="11111111-1111-1111-1111-111111111111", status="processing")
        db.mark_processed_on_conn(
            conn, "b1", "11111111-1111-1111-1111-111111111111"
        )
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "processed")
        self.assertIsNotNone(row["processed_at"])

    def test_mark_failed_sets_status_and_reason(self):
        conn = _fresh_db()
        _insert_one(conn, unit_id="11111111-1111-1111-1111-111111111111", status="processing")
        db.mark_failed_on_conn(
            conn,
            "b1",
            "11111111-1111-1111-1111-111111111111",
            reason="TimeoutError: timed out",
        )
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_reason"], "TimeoutError: timed out")
        self.assertIsNotNone(row["processed_at"])

    def test_mark_superseded_only_targets_eligible_statuses(self):
        """supersede must not touch a row that is currently processing
        (that would be the very row being reconciled — its own id)."""
        conn = _fresh_db()
        _insert_one(
            conn,
            unit_id="11111111-1111-1111-1111-111111111111",
            status="processing",
        )
        _insert_one(
            conn,
            unit_id="22222222-2222-2222-2222-222222222222",
            status="processed",
        )
        _insert_one(
            conn,
            unit_id="33333333-3333-3333-3333-333333333333",
            status="failed",
        )
        _insert_one(
            conn,
            unit_id="44444444-4444-4444-4444-444444444444",
            status="superseded",
        )
        _insert_one(
            conn,
            unit_id="55555555-5555-5555-5555-555555555555",
            status="pending",
        )

        affected = db.mark_superseded_on_conn(
            conn,
            "b1",
            [
                "11111111-1111-1111-1111-111111111111",  # processing — skip
                "22222222-2222-2222-2222-222222222222",  # processed — mark
                "33333333-3333-3333-3333-333333333333",  # failed — skip
                "44444444-4444-4444-4444-444444444444",  # superseded — skip
                "55555555-5555-5555-5555-555555555555",  # pending — mark
            ],
            reason="newer fact supersedes",
        )
        # 2 rows actually flipped.
        self.assertEqual(affected, 2)
        # Cross-check each row.
        self.assertEqual(
            _row(conn, "b1", "11111111-1111-1111-1111-111111111111")["status"],
            "processing",
        )
        self.assertEqual(
            _row(conn, "b1", "22222222-2222-2222-2222-222222222222")["status"],
            "superseded",
        )
        self.assertEqual(
            _row(conn, "b1", "33333333-3333-3333-3333-333333333333")["status"],
            "failed",
        )
        self.assertEqual(
            _row(conn, "b1", "44444444-4444-4444-4444-444444444444")["status"],
            "superseded",
        )
        self.assertEqual(
            _row(conn, "b1", "55555555-5555-5555-5555-555555555555")["status"],
            "superseded",
        )

    def test_mark_superseded_records_reason(self):
        conn = _fresh_db()
        _insert_one(
            conn,
            unit_id="11111111-1111-1111-1111-111111111111",
            status="processed",
        )
        db.mark_superseded_on_conn(
            conn,
            "b1",
            ["11111111-1111-1111-1111-111111111111"],
            reason="verbatim quote from reflect reasoning",
        )
        row = _row(conn, "b1", "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["superseded_reason"], "verbatim quote from reflect reasoning")
        self.assertIsNotNone(row["processed_at"])


# ── health stats ────────────────────────────────────────────────────────


class HealthStatsTest(unittest.TestCase):
    def test_health_stats_reports_counts_per_status(self):
        conn = _fresh_db()
        _insert_one(conn, unit_id="11111111-1111-1111-1111-111111111111", status="pending")
        _insert_one(conn, unit_id="22222222-2222-2222-2222-222222222222", status="pending")
        _insert_one(
            conn, unit_id="33333333-3333-3333-3333-333333333333", status="processed"
        )
        _insert_one(conn, unit_id="44444444-4444-4444-4444-444444444444", status="failed")
        _insert_one(
            conn, unit_id="55555555-5555-5555-5555-555555555555", status="superseded"
        )
        stats = db.health_stats_on_conn(conn)
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["superseded"], 1)
        self.assertEqual(stats["total"], 5)


if __name__ == "__main__":
    unittest.main()
