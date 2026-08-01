"""End-to-end replay of the 2026-07-30 incident against the new architecture.

Boots the real webhook server on a port, replays the exact byte-for-byte
payload from hindsight-memorial.log (the op d1b21d2e delivery that Hindsight
sent 5 times over 8 hours), and asserts:

  1. Every POST is answered fast — the handler is now ingest-only, so the
     reconcile no longer runs on the request thread at all. Hindsight's
     outbox would never time out and start its retry ladder.
  2. The 5 replays do not produce 5 separate reconciles. In the legacy
     architecture, the dispatcher's body-hash dedup caught the duplicates.
     In the new architecture, the dedup moves down to the database: the
     5 deliveries all call ``upsert_unit`` for the same ``(bank_id, unit_id)``;
     only the first inserts, the rest are no-ops at the upsert level. The
     poller then runs reflect exactly once.
  3. The local ``memory_units`` table has exactly one row for the incident
     unit, in the ``processed`` state once the poller has drained the queue.

The 2026-07-30 incident was caused by a path that no longer exists
(``handle_event`` running reflect inline). The new architecture makes
that path unreachable from the webhook — this script is the regression
guard that proves it.

Run: .venv/Scripts/python.exe scripts/replay_incident.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pin the SQLite path BEFORE importing memorial modules so the
# in-memory default is replaced with a file-backed DB. We use a temp
# file (not :memory:) so the poller — which runs on a separate thread —
# can see the data the handler wrote on the request thread. With
# ``:memory:`` each connection has its own private database, which
# defeats the cross-thread handoff.
_DB_PATH = os.path.join(
    os.environ.get("TMP", os.environ.get("TEMP", "/tmp")),
    f"hindsight_memorial_replay_{os.getpid()}.sqlite",
)
os.environ.setdefault("HINDSIGHT_POLLER_ENABLED", "1")
os.environ.setdefault("HINDSIGHT_POLLER_INTERVAL_SEC", "0.05")

# Tell the in-process DB to use the file we just created, not the
# in-memory default. We do this by monkey-patching ``db.get_connection``
# before the server starts.
import hindsight_memorial.db as db_mod
import hindsight_memorial.poller as poller_mod

_orig_get_connection = db_mod.get_connection


def _file_get_connection():
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db_mod.init_db_on_conn(conn)
    return conn


db_mod.get_connection = _file_get_connection

from hindsight_memorial import webhook_server  # noqa: E402
from hindsight_memorial.webhook_handlers import configure_logging  # noqa: E402

SECRET = b"replay-secret"
PORT = 19602

# Server reads the HMAC secret from this module-level variable inside
# its BaseHTTPRequestHandler subclass. We set it before the server
# threads start so all requests authenticate with the same secret the
# client uses.
webhook_server.SERVER_SECRET = SECRET

# Verbatim from hindsight-memorial.log line 2 — the body Hindsight replayed
# at 16:43:17, 16:48:48, 17:19:18, 19:19:48 and 00:20:18. Byte-identical each
# time, including the payload timestamp. ``data={}`` (no document_id) was
# the path the legacy code handled via the recent-units fallback; the new
# architecture preserves that fallback.
INCIDENT_BODY = (
    b'{"event":"retain.completed","bank_id":"hermes-agent",'
    b'"operation_id":"d1b21d2ebfc84c2290ff6059730a433f","status":"completed",'
    b'"timestamp":"2026-07-30T16:42:34.258901Z",'
    b'"data":{"tags":["identity","user"]}}'
)

# The new fact the incident was about (a name correction). The poller
# will run reflect on this fact and (in this script) the fake
# ``run_reconcile`` says "no supersede", so the row is marked processed
# after one pass.
INCIDENT_FACT = (
    "用户姓名最终确认为张春丽，此前多次更正过姓名记录"
)
INCIDENT_UNIT_ID = "ca7c25e7-ab34-4133-85fb-bd5b63375628"
INCIDENT_DOC_ID = "20260731_001522_cead67"


def _fake_run_reconcile(bank_id, unit_id, content):
    """Stand-in for the real run_reconcile. Returns 'abandoned' so the
    poller marks the row 'processed' after one pass, and the count of
    reflect calls is the proxy for "did the poller actually run".
    """
    reconcile_calls.append((bank_id, unit_id, time.monotonic()))
    from hindsight_memorial.reconcile import ReconcileResult
    return ReconcileResult(status="abandoned", bank_id=bank_id)


reconcile_calls: list[tuple[str, str, float]] = []


def post(body: bytes) -> tuple[int, dict, float]:
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/webhook/hindsight",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hindsight-Event": "retain.completed",
            "X-Hindsight-Signature": sig,
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
        return resp.status, payload, time.monotonic() - started


def _seed_pretend_unit():
    """Pretend Hindsight returned a single unit for the incident's
    document. In production this is the response from
    ``GET /v1/default/banks/hermes-agent/memories/list?document_id=...``
    — here we just insert a row directly so the handler's ``upsert``
    will pick it up."""
    # We cannot seed ``fetch_units`` before starting the server
    # because the fetch callable is built inside ``_make_fetch_units``
    # from env vars. The simplest way to inject a fixture is to
    # monkey-patch ``_make_fetch_units`` before the server is started.
    raise NotImplementedError  # wired below


def _build_fetcher():
    """Build a ``fetch_units(bank_id, document_id)`` closure that
    always returns the single incident unit. The ``document_id`` we
    return is the one from the recovered fallback, not from the
    webhook payload (because the payload has data={} in this incident).
    """
    unit = {
        "id": INCIDENT_UNIT_ID,
        "text": INCIDENT_FACT,
        "document_id": INCIDENT_DOC_ID,
        "mentioned_at": "2026-07-30T16:42:34Z",
        "date": "2026-07-30T00:00:00Z",
    }

    def fetch_units(bank_id, document_id):
        return [unit]

    def fetch_recent_doc(bank_id):
        # Within 60s of the event timestamp — fallback accepted.
        return (INCIDENT_DOC_ID, "2026-07-30T16:42:34Z")

    return fetch_units, fetch_recent_doc


def main() -> int:
    configure_logging(level="WARNING")
    # Clean up any prior DB file.
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)

    # Patch the fetcher factory so the handler sees our pretend unit
    # without needing a real Hindsight server.
    fetch_units, fetch_recent_doc = _build_fetcher()
    webhook_server._make_fetch_units = lambda: fetch_units
    webhook_server._make_fetch_recent_doc = lambda: fetch_recent_doc
    # Patch the poller's run_reconcile factory so we count reflect calls.
    webhook_server._build_poller_run_reconcile = lambda: _fake_run_reconcile

    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT),
        webhook_server._make_healthz_handler(None),  # poller is started in serve()
    )
    # We start the server ourselves so we can inject the poller stub.
    # Re-implement ``serve()`` here to keep the original unmodified.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.2)

    # Manually start the poller using the patched run_reconcile.
    poller_conn = db_mod.get_connection()
    db_mod.init_db_on_conn(poller_conn)
    p = poller_mod.ReconcilerPoller(
        poller_conn, _fake_run_reconcile, poll_interval_sec=0.05
    )
    p.start()

    print(f"\n{'='*70}")
    print("Replaying op d1b21d2e — the delivery Hindsight sent 5 times")
    print("(in the new architecture: handler is ingest-only, poller drains)")
    print(f"{'='*70}\n")

    latencies = []
    for i in range(5):
        status, payload, elapsed = post(INCIDENT_BODY)
        latencies.append(elapsed)
        print(
            f"  delivery {i+1}: HTTP {status} "
            f"status={payload.get('status', '?'):<9} "
            f"{elapsed*1000:6.1f} ms"
        )

    # Wait for the poller to drain the queue.
    print("\n  waiting for the poller to drain...")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        stats = db_mod.health_stats_on_conn(db_mod.get_connection())
        if stats["pending"] == 0:
            break
        time.sleep(0.05)
    time.sleep(0.1)
    p.stop(timeout=2.0)

    # Inspect the final DB state.
    final = db_mod.health_stats_on_conn(db_mod.get_connection())
    unit_count_row = db_mod.get_connection().execute(
        "SELECT COUNT(*) AS c FROM memory_units WHERE bank_id='hermes-agent' "
        "AND unit_id=?", (INCIDENT_UNIT_ID,)
    ).fetchone()

    slowest = max(latencies)
    print(f"\n{'='*70}")
    print(f"  slowest HTTP response : {slowest*1000:.1f} ms")
    print(f"  poller reflect calls  : {len(reconcile_calls)}  (was 5 during the incident)")
    print(f"  final db stats        : {final}")
    print(f"  unit rows for incident: {unit_count_row['c']}")
    print(f"{'='*70}\n")

    ok = True
    if slowest > 1.0:
        print(f"  FAIL: a response took {slowest:.1f}s — outbox would retry")
        ok = False
    else:
        print(f"  PASS: every response under 1s (handler is ingest-only)")

    if unit_count_row["c"] != 1:
        print(
            f"  FAIL: {unit_count_row['c']} rows for the incident unit, "
            "expected exactly 1 (dedup must apply at the unit level)"
        )
        ok = False
    else:
        print("  PASS: 5 replays collapsed to 1 row at the unit level")

    if len(reconcile_calls) != 1:
        print(
            f"  FAIL: {len(reconcile_calls)} poller passes, expected exactly 1"
        )
        ok = False
    else:
        print("  PASS: poller ran reflect exactly once")

    if final.get("processed", 0) != 1:
        print(
            f"  FAIL: {final.get('processed', 0)} rows in 'processed' state, "
            "expected 1 (the poller should have drained the queue)"
        )
        ok = False
    else:
        print("  PASS: the single row reached the 'processed' terminal state")

    server.shutdown()
    server.server_close()
    # Clean up the temp DB. On Windows the poller thread's connection
    # may still hold a file lock for a moment after stop(); ignore
    # failures and let the OS clean up via TEMP.
    if os.path.exists(_DB_PATH):
        try:
            os.remove(_DB_PATH)
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
