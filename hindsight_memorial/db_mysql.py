"""MySQL backend for the persistent reconciler.

This module is the production counterpart to :mod:`hindsight_memorial.db`.
It is loaded lazily by :func:`hindsight_memorial.db.get_connection` only
when the deployment has MySQL configured (``HINDSIGHT_MYSQL_HOST`` set
and PyMySQL importable). For tests and the local "ingest-only" mode,
:mod:`hindsight_memorial.db`'s in-memory SQLite is used.

Why a separate module: tests must not depend on a live MySQL server, and
the production image needs PyMySQL. Splitting keeps ``db.py`` (which
defines the *interface*) free of the production-only dependency, so
``tests/test_db.py`` can run on a clean checkout with no third-party
packages installed.

The SQL emitted here is a thin translation of the SQLite DDL/DML in
:mod:`hindsight_memorial.db` to MySQL syntax. The only material
differences are:

  * AUTOINCREMENT  →  ``BIGINT UNSIGNED NOT NULL AUTO_INCREMENT``
  * TEXT          →  ``VARCHAR(255)`` / ``TEXT`` per column
  * ENUM          →  native ``ENUM(...)``
  * TIMESTAMP     →  native ``DATETIME``

Rather than duplicate every statement in :mod:`hindsight_memorial.db`,
this module wraps the PyMySQL connection in :class:`_ConnAdapter`, which
exposes the sqlite3 ``Connection.execute(sql, params)`` shape and
rewrites ``?`` placeholders to ``%s``. That keeps a single copy of the
SQL — the one in ``db.py`` — running on both backends, which is what
makes the SQLite test suite meaningful coverage for production.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from .config import DBConfig, load_db_config

log = logging.getLogger("hindsight_memorial.db_mysql")

# MySQL's ENUM is a column-level type, not a free-standing type. The
# five documented terminal states appear here in the same order as the
# schema in :mod:`hindsight_memorial.db`.
_STATUS_ENUM = (
    "'pending','processing','processed','superseded','failed'"
)

# ── DDL ─────────────────────────────────────────────────────────────────
#
# The PK is BIGINT UNSIGNED to match the SQLite INTEGER PRIMARY KEY
# autoincrement. ``AUTO_INCREMENT`` requires an explicit key on the
# column; we declare it on the PK.
#
# Verified against the deployment target (MySQL 5.7.44): 5.7 parses the
# ``DESC`` in an index definition and ignores it — reverse scans use the
# index either way — so the same DDL runs on 5.7 and 8.

_DDL = f"""
CREATE TABLE IF NOT EXISTS memory_units (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT
                        COMMENT '自增主键,仅本地使用,与 Hindsight 无关',
    bank_id             VARCHAR(255)    NOT NULL
                        COMMENT 'Hindsight bank id,事件里带来的记忆库标识',
    unit_id             VARCHAR(64)     NOT NULL
                        COMMENT 'Hindsight memory_unit 的 UUID,与 bank_id 组成幂等键',
    content             TEXT            NOT NULL
                        COMMENT '该 memory_unit 的事实正文,reflect 的输入',
    created_at          DATETIME        NOT NULL
                        COMMENT '该事实在 Hindsight 侧的产生时间(UTC),poller 按此倒序取件',
    document_id         VARCHAR(255)    DEFAULT NULL
                        COMMENT '来源文档 id;webhook 的 data 为空对象时由 fallback 恢复,可能为 NULL',
    status              ENUM({_STATUS_ENUM}) NOT NULL DEFAULT 'pending'
                        COMMENT '状态机:pending 待处理 / processing 处理中 / processed 已完成 / superseded 被更新事实取代 / failed reflect 失败',
    superseded_reason   TEXT            DEFAULT NULL
                        COMMENT '被取代的原因,取自 reflect LLM 的 reasoning(截断 500 字符)',
    failure_reason      TEXT            DEFAULT NULL
                        COMMENT '失败摘要(截断 500 字符),完整堆栈只进日志',
    ingested_at         DATETIME        NOT NULL
                        COMMENT '本地入库时间(UTC),内容未变的重复投递不刷新此列',
    processed_at        DATETIME        DEFAULT NULL
                        COMMENT '进入终态(processed/superseded/failed)的时间(UTC)',
    PRIMARY KEY (id),
    UNIQUE KEY uq_bank_unit (bank_id, unit_id)
        COMMENT '单元级幂等键:Hindsight 重投同一事件时折叠为一行',
    KEY idx_status_created (status, created_at DESC)
        COMMENT 'poller 取件:WHERE status=pending ORDER BY created_at DESC LIMIT 1',
    KEY idx_status_ingested (status, ingested_at DESC)
        COMMENT '按入库时间排查积压用'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  ROW_FORMAT=DYNAMIC
  COMMENT='hindsight-memorial 本地对账表:webhook 落库 + poller 消费的持久化状态机'
"""


# ── sqlite3-shaped connection adapter ──────────────────────────────────
#
# ``db.py`` holds the single copy of the SQL and calls it in the sqlite3
# style: ``conn.execute(sql, params)`` returning a cursor, with ``?``
# placeholders. PyMySQL connections have no ``.execute()`` and use
# ``%s``. Rather than fork every statement, we adapt the connection.

_PLACEHOLDER_RE = re.compile(r"\?")

# db.py renders timestamps as ISO 8601 with an explicit UTC offset,
# because SQLite stores them as TEXT and needs lexicographic ordering.
# MySQL's DATETIME columns want a datetime (or 'YYYY-MM-DD HH:MM:SS').
# We convert only strings that match this exact shape — a full ISO
# timestamp in UTC and nothing else — so a fact whose *content* merely
# mentions a date is untouched.
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")


def _adapt_param(value: Any) -> Any:
    if isinstance(value, str) and _ISO_UTC_RE.match(value):
        # Naive UTC — the column carries no timezone, and everything we
        # write is UTC by construction.
        return datetime.fromisoformat(value).replace(tzinfo=None)
    return value


class _Result:
    """Buffered query result with the sqlite3 cursor surface db.py uses.

    Rows are read out before the underlying cursor is closed, so no
    cursor stays open past the connection lock (see :class:`_ConnAdapter`).
    """

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class _ConnAdapter:
    """Wrap a PyMySQL connection in the sqlite3 ``Connection`` shape.

    Both the webhook request threads and the poller thread share one
    connection (see :func:`get_connection`), and PyMySQL's wire protocol
    is not safe for concurrent use. Every statement therefore runs under
    ``_stmt_lock``, held from cursor creation until the rows have been
    buffered — so two threads can never interleave on the socket.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._stmt_lock = threading.RLock()

    def execute(self, sql: str, params: tuple | list = ()) -> _Result:
        mysql_sql = _PLACEHOLDER_RE.sub("%s", sql)
        adapted = tuple(_adapt_param(p) for p in params)
        with self._stmt_lock:
            with self._conn.cursor() as cur:
                cur.execute(mysql_sql, adapted)
                rows = list(cur.fetchall() or [])
                rowcount = cur.rowcount
        return _Result(rows, rowcount)

    def commit(self) -> None:
        with self._stmt_lock:
            self._conn.commit()

    def close(self) -> None:
        with self._stmt_lock:
            self._conn.close()

    def ping(self, reconnect: bool = True) -> None:
        with self._stmt_lock:
            self._conn.ping(reconnect=reconnect)

    def cursor(self):
        """Escape hatch for MySQL-only statements (the DDL in
        :func:`init_db_on_conn`). Not used by ``db.py``."""
        return self._conn.cursor()


# ── connection management ──────────────────────────────────────────────

# A single MySQL connection is fine for this workload: the poller is
# serial, the webhook handler is short-lived, and the volume is
# low (one webhook per retain.completed). If concurrent load ever
# shows up we can switch to a pool here without changing the call
# sites — they only depend on ``get_connection()``.
_lock = threading.Lock()
_connection: Any | None = None
_config: DBConfig | None = None


def _open_connection(cfg: DBConfig):
    """Open a fresh MySQL connection. The caller is responsible for
    closing it (or handing it to the module-level cache)."""
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )


def get_connection():
    """Return a long-lived MySQL connection wrapped in :class:`_ConnAdapter`.

    PyMySQL connections are not thread-safe for concurrent queries; the
    adapter serialises every statement on its own lock. The poller is
    single-threaded; the webhook handler runs on a ``ThreadingHTTPServer``
    thread but each call into the DB is brief (one upsert, one SELECT
    for health) so contention is negligible.
    """
    global _connection, _config
    if _connection is not None:
        try:
            _connection.ping(reconnect=True)
            return _connection
        except Exception:  # pragma: no cover - depends on server state
            log.warning("MySQL connection lost; reopening")
            _connection = None
    with _lock:
        if _connection is None:
            cfg = load_db_config()
            if cfg.backend != "mysql":
                raise RuntimeError(
                    "db_mysql.get_connection called but HINDSIGHT_MYSQL_HOST is unset"
                )
            _config = cfg
            log.info(
                "opening MySQL connection host=%s db=%s", cfg.host, cfg.database
            )
            _connection = _ConnAdapter(_open_connection(cfg))
        return _connection


def init_db_on_conn(conn) -> None:
    """Apply the MySQL schema. Idempotent (``CREATE TABLE IF NOT EXISTS``)."""
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def reset_for_tests() -> None:
    """Drop the cached connection so the next call rebuilds it. Tests
    only — never call this from production code."""
    global _connection
    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
        _connection = None


__all__ = ["get_connection", "init_db_on_conn", "reset_for_tests"]
