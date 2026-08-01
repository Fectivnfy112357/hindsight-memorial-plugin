"""Smoke-test the MySQL backend against a live server.

The unit tests drive the SQLite backend only (no live MySQL in CI), so
this script is what pins the MySQL path: it runs every statement in
``db.py`` through the ``_ConnAdapter`` and asserts the same contract the
SQLite tests assert. Run it once against a new deployment before
sending real webhook traffic.

Requires HINDSIGHT_MYSQL_* in the environment. Writes and then deletes
rows under a throwaway bank id, so it is safe to run against the
production table.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from hindsight_memorial import db
from hindsight_memorial.config import load_db_config

BANK = "__smoke_test_bank__"


def main() -> int:
    cfg = load_db_config()
    if cfg.backend != "mysql":
        print("HINDSIGHT_MYSQL_HOST is unset — nothing to smoke-test")
        return 1
    print(f"backend=mysql host={cfg.host}:{cfg.port} db={cfg.database}")

    conn = db.get_connection()
    db.init_db_on_conn(conn)
    print("init_db: ok")

    # Clean any leftovers from a previous run.
    conn.execute("DELETE FROM memory_units WHERE bank_id=?", (BANK,))
    conn.commit()

    created = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    o1 = db.upsert_unit_on_conn(
        conn, bank_id=BANK, unit_id="u-1", content="fact one",
        created_at=created, document_id="doc-1",
    )
    assert o1 == "inserted", o1
    o2 = db.upsert_unit_on_conn(
        conn, bank_id=BANK, unit_id="u-1", content="fact one",
        created_at=created, document_id="doc-1",
    )
    assert o2 == "skipped", o2
    o3 = db.upsert_unit_on_conn(
        conn, bank_id=BANK, unit_id="u-1", content="fact one revised",
        created_at=created, document_id="doc-1",
    )
    assert o3 == "updated", o3
    print("upsert: inserted/skipped/updated all correct")

    db.upsert_unit_on_conn(
        conn, bank_id=BANK, unit_id="u-2", content="fact two",
        created_at=datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc),
        document_id="doc-1",
    )
    row = db.fetch_pending_row_on_conn(conn)
    assert row is not None and row["unit_id"] == "u-2", row
    print("fetch_pending_row: newest created_at wins")

    # The poller's own 'processing' flip — the one raw statement outside db.py.
    conn.execute("UPDATE memory_units SET status='processing' WHERE id=?", (row["id"],))
    conn.commit()

    db.mark_processed_on_conn(conn, BANK, "u-2")
    n = db.mark_superseded_on_conn(conn, BANK, ["u-1", "u-2"], reason="smoke reason")
    assert n == 2, f"expected both rows flipped, got {n}"
    print(f"mark_processed + mark_superseded: {n} rows flipped")

    stats = db.health_stats_on_conn(conn)
    print(f"health_stats: {stats}")
    assert stats["total"] >= 2, stats

    conn.execute("DELETE FROM memory_units WHERE bank_id=?", (BANK,))
    conn.commit()
    print("cleanup: ok")
    print("\n=== MySQL backend smoke test PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
