"""End-to-end replay of the 2026-07-30 incident against a live server.

Boots the real webhook server on a port, replays the exact byte-for-byte
payloads from hindsight-memorial.log (the op d1b21d2e delivery that Hindsight
sent 5 times over 8 hours), and asserts:

  1. every POST is answered fast enough that Hindsight's outbox would never
     time out and start its retry ladder, even though the reconcile itself
     is slow;
  2. the 4 replays never reach the reconcile — only the first does.

Run: .venv/Scripts/python.exe scripts/replay_incident.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hindsight_memorial import webhook_server  # noqa: E402
from hindsight_memorial.dispatch import Dispatcher  # noqa: E402
from hindsight_memorial.webhook_handlers import configure_logging  # noqa: E402

SECRET = b"replay-secret"
PORT = 19602

# Verbatim from hindsight-memorial.log line 2 — the body Hindsight replayed
# at 16:43:17, 16:48:48, 17:19:18, 19:19:48 and 00:20:18. Byte-identical each
# time, including the payload timestamp.
INCIDENT_BODY = (
    b'{"event":"retain.completed","bank_id":"hermes-agent",'
    b'"operation_id":"d1b21d2ebfc84c2290ff6059730a433f","status":"completed",'
    b'"timestamp":"2026-07-30T16:42:34.258901Z",'
    b'"data":{"tags":["identity","user"]}}'
)

# How long a real reconcile took during the incident (10-70s observed). We
# use a smaller value so the script finishes, but it is still far longer than
# any sane webhook timeout.
FAKE_RECONCILE_SECONDS = 3.0

reconcile_calls: list[float] = []


def slow_reconcile(raw_body: bytes, headers: dict) -> object:
    """Stand-in for handle_event: as slow as the real thing, no network."""
    reconcile_calls.append(time.monotonic())
    time.sleep(FAKE_RECONCILE_SECONDS)

    class _Outcome:
        status = "abandoned"
        bank_id = "hermes-agent"
        document_id = "20260731_001522_cead67"
        units_processed = 1
        total_superseded = 0
        total_observations_cleared = 0

    return _Outcome()


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


def main() -> int:
    configure_logging(level="INFO")

    dispatcher = Dispatcher(slow_reconcile)
    dispatcher.start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), webhook_server._make_handler(SECRET, dispatcher)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    print(f"\n{'='*70}")
    print("Replaying op d1b21d2e — the delivery Hindsight sent 5 times")
    print(f"reconcile takes {FAKE_RECONCILE_SECONDS}s; watch the response times")
    print(f"{'='*70}\n")

    latencies = []
    for i in range(5):
        status, payload, elapsed = post(INCIDENT_BODY)
        latencies.append(elapsed)
        print(
            f"  delivery {i+1}: HTTP {status} "
            f"status={payload['status']:<9} {elapsed*1000:6.1f} ms"
        )

    print("\n  waiting for the background worker to drain...")
    dispatcher.wait_idle(timeout=30)
    time.sleep(0.3)

    slowest = max(latencies)
    print(f"\n{'='*70}")
    print(f"  slowest HTTP response : {slowest*1000:.1f} ms")
    print(f"  reconciles executed   : {len(reconcile_calls)}  (was 5 during the incident)")
    print(f"  dispatcher stats      : {dispatcher.stats()}")
    print(f"{'='*70}\n")

    ok = True
    if slowest > 1.0:
        print(f"  FAIL: a response took {slowest:.1f}s — outbox would retry")
        ok = False
    else:
        print(f"  PASS: every response under 1s despite a {FAKE_RECONCILE_SECONDS}s reconcile")

    if len(reconcile_calls) != 1:
        print(f"  FAIL: {len(reconcile_calls)} reconciles ran, expected exactly 1")
        ok = False
    else:
        print("  PASS: 4 replays dropped, reconcile ran exactly once")

    dispatcher.stop()
    server.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
