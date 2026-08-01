"""SQLite/MySQL persistence layer for the persistent reconciler.

The design contract lives in ``doc/persistent-reconciler-design-2026-08-01.md``
§3-§5; this module is the only place that talks to the database. All
operations take an explicit connection so tests can drive an in-memory
SQLite database without process-global state.

This file implements the SQLite backend. The MySQL backend (production)
is plugged in by ``hindsight_memorial.db_mysql`` (see task #17); the public
API stays the same.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

log = logging.getLogger("hindsight_memorial.db")

UpsertOutcome = Literal["inserted", "updated", "skipped"]


# ── schema ──────────────────────────────────────────────────────────────


# DDL mirrors the design doc. SQLite has no native ENUM, so status is stored
# as TEXT; the application contract (see tests) is the only enforcement.
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS memory_units (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_id             TEXT    NOT NULL,
    unit_id             TEXT    NOT NULL,
    content             TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    document_id         TEXT    DEFAULT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    superseded_reason   TEXT    DEFAULT NULL,
    failure_reason      TEXT    DEFAULT NULL,
    ingested_at         TEXT    NOT NULL,
    processed_at        TEXT    DEFAULT NULL
);
"""


def init_db_on_conn(conn) -> None:
    """Apply the schema to the given connection. Idempotent.

    Dispatches on the connection type: the MySQL adapter carries its own
    DDL (native ENUM, AUTO_INCREMENT, column comments), while a raw
    sqlite3 connection gets the SQLite DDL below.
    """
    if not isinstance(conn, sqlite3.Connection):
        from . import db_mysql

        db_mysql.init_db_on_conn(conn)
        return
    _init_db_sqlite(conn)


def _init_db_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQLITE)
    # Unique constraint — written separately so the table definition
    # stays readable. ``IF NOT EXISTS`` is not supported for indexes in
    # older SQLite, so we swallow the "already exists" error.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX uq_bank_unit ON memory_units(bank_id, unit_id)"
        )
    except sqlite3.OperationalError as e:
        if "already exists" not in str(e):
            raise
    try:
        conn.execute(
            "CREATE INDEX idx_status_created ON memory_units(status, created_at DESC)"
        )
    except sqlite3.OperationalError as e:
        if "already exists" not in str(e):
            raise
    try:
        conn.execute(
            "CREATE INDEX idx_status_ingested ON memory_units(status, ingested_at DESC)"
        )
    except sqlite3.OperationalError as e:
        if "already exists" not in str(e):
            raise
    conn.commit()


# ── helpers ─────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    """Render a datetime as ISO 8601 with explicit UTC offset.

    SQLite stores everything as TEXT; the application contract pins the
    format to ISO 8601 (with offset) so ORDER BY comparisons are
    lexicographically correct.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


# ── upsert ──────────────────────────────────────────────────────────────


def upsert_unit_on_conn(
    conn: sqlite3.Connection,
    *,
    bank_id: str,
    unit_id: str,
    content: str,
    created_at: datetime,
    document_id: str | None,
) -> UpsertOutcome:
    """Idempotent write of one unit. Returns 'inserted', 'updated', or 'skipped'.

    Semantics (mirrors design doc §4):
      - New (bank_id, unit_id): insert with status='pending'.
      - Existing with same content: no-op (return 'skipped'). All fields
        keep their current values — including ingested_at, which lets the
        fallback 60s-window check stay accurate on repeat deliveries.
      - Existing with different content: update content/created_at/document_id,
        reset status='pending', bump ingested_at.

    We do a SELECT first to learn the prior state, then issue INSERT or
    UPDATE. This is two round-trips but the column set is small and the
    decision tree is what lets us return a precise outcome string. (A
    pure ON CONFLICT approach can't tell you whether the conflict path
    actually mutated anything.)
    """
    cur = conn.execute(
        "SELECT content FROM memory_units WHERE bank_id=? AND unit_id=?",
        (bank_id, unit_id),
    )
    prior = cur.fetchone()
    if prior is None:
        conn.execute(
            """
            INSERT INTO memory_units
                (bank_id, unit_id, content, created_at, document_id, status, ingested_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (bank_id, unit_id, content, _iso(created_at), document_id, _now_iso()),
        )
        conn.commit()
        return "inserted"

    if prior["content"] == content:
        # Content unchanged. Leave the row alone — status, ingested_at,
        # processed_at all preserved. This is what lets the fallback
        # 60s-window check remain accurate across replays.
        conn.commit()
        return "skipped"

    # Content changed: rewrite the row, reset to pending, bump ingested_at.
    conn.execute(
        """
        UPDATE memory_units
        SET content=?, created_at=?, document_id=?, status='pending', ingested_at=?
        WHERE bank_id=? AND unit_id=?
        """,
        (content, _iso(created_at), document_id, _now_iso(), bank_id, unit_id),
    )
    conn.commit()
    return "updated"


# ── poller queries ──────────────────────────────────────────────────────


def fetch_pending_row_on_conn(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the most-recent pending row, or None if there is no work.

    Ordering matches the design doc §5: ``ORDER BY created_at DESC, id DESC``
    so two rows that share created_at are stable across runs (id is the
    autoincrement tiebreaker).
    """
    cur = conn.execute(
        """
        SELECT id, bank_id, unit_id, content, created_at, document_id
        FROM memory_units
        WHERE status='pending'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def mark_processed_on_conn(
    conn: sqlite3.Connection,
    bank_id: str,
    unit_id: str,
) -> None:
    """Flip a row to status='processed'. Caller is responsible for status
    coherence — typically called right after fetch_pending_row_on_conn."""
    conn.execute(
        """
        UPDATE memory_units
        SET status='processed', processed_at=?
        WHERE bank_id=? AND unit_id=?
        """,
        (_now_iso(), bank_id, unit_id),
    )
    conn.commit()


def mark_failed_on_conn(
    conn: sqlite3.Connection,
    bank_id: str,
    unit_id: str,
    *,
    reason: str,
) -> None:
    """Flip a row to status='failed' with a short reason string.

    Caller is expected to log the full traceback separately; the
    failure_reason column holds only a compact summary so DB queries
    stay cheap.
    """
    conn.execute(
        """
        UPDATE memory_units
        SET status='failed', failure_reason=?, processed_at=?
        WHERE bank_id=? AND unit_id=?
        """,
        (reason[:500], _now_iso(), bank_id, unit_id),
    )
    conn.commit()


def mark_superseded_on_conn(
    conn: sqlite3.Connection,
    bank_id: str,
    unit_ids: list[str],
    *,
    reason: str,
) -> int:
    """Soft-mark rows whose ids appear in ``unit_ids`` as superseded.

    Eligibility: status IN ('pending', 'processed'). Rows that are
    currently 'processing' must NEVER be touched — that would be the
    very row being reconciled (the just-retained unit's own id), and
    flipping it would lose the audit trail of the in-flight work.
    Rows that are already 'failed' or 'superseded' are also left alone
    — the former because we don't want to overwrite diagnostic
    information, the latter because they're already in the terminal
    state.

    Returns the number of rows actually flipped, mainly for logging.
    """
    if not unit_ids:
        return 0
    placeholders = ",".join(["?"] * len(unit_ids))
    cur = conn.execute(
        f"""
        UPDATE memory_units
        SET status='superseded', superseded_reason=?, processed_at=?
        WHERE bank_id=?
          AND unit_id IN ({placeholders})
          AND status IN ('pending','processed')
        """,
        (reason[:500], _now_iso(), bank_id, *unit_ids),
    )
    conn.commit()
    return cur.rowcount or 0


# ── health stats ────────────────────────────────────────────────────────


def health_stats_on_conn(conn: sqlite3.Connection) -> dict[str, int]:
    """Return row counts per status plus a 'total'. For /healthz."""
    cur = conn.execute(
        "SELECT status, COUNT(*) AS c FROM memory_units GROUP BY status"
    )
    out: dict[str, int] = {
        "pending": 0,
        "processing": 0,
        "processed": 0,
        "superseded": 0,
        "failed": 0,
        "total": 0,
    }
    for row in cur.fetchall():
        s = row["status"]
        c = row["c"]
        if s in out:
            out[s] = c
        out["total"] += c
    return out


# ── connection acquisition ─────────────────────────────────────────────
#
# Production code should not import sqlite3 directly. Instead, it
# calls ``get_connection()`` which returns a backend-appropriate
# connection. The MySQL backend (task #17) overrides this function
# when ``HINDSIGHT_MYSQL_HOST`` is set; the SQLite path is the
# in-memory default used by tests.
from .config import load_db_config


def get_connection():
    """Return a connection to the local persistence backend.

    Resolution order:
      1. If the deployment has MySQL configured (HINDSIGHT_MYSQL_HOST
         set and PyMySQL importable), open a long-lived MySQL
         connection. This is the production path.
      2. Otherwise, return an in-memory SQLite connection. This is the
         test path AND the "ingest-only" local mode (see design doc
         §12.2). The in-memory DB has the schema applied on first
         access, so callers do not have to remember to call
         ``init_db_on_conn``.

    The returned connection is owned by the caller for the duration of
    a single operation. For SQLite, the connection is fresh on every
    call (the in-memory DB is single-process and short-lived anyway).
    For MySQL, the module-level cache in :mod:`db_mysql` returns the
    long-lived connection.
    """
    cfg = load_db_config()
    if cfg.backend == "mysql":
        # Imported lazily so tests do not require PyMySQL.
        from . import db_mysql

        return db_mysql.get_connection()
    # SQLite in-memory fallback. Per-call connection keeps the
    # ``:memory:`` lifetime aligned with the request: as soon as the
    # connection is closed, the data is gone. This is the desired
    # behaviour for tests; production never reaches this branch.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db_on_conn(conn)
    return conn


__all__ = [
    "UpsertOutcome",
    "fetch_pending_row_on_conn",
    "get_connection",
    "health_stats_on_conn",
    "init_db_on_conn",
    "mark_failed_on_conn",
    "mark_processed_on_conn",
    "mark_superseded_on_conn",
    "upsert_unit_on_conn",
]
