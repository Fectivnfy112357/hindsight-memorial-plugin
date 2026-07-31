"""Async dispatch + idempotency for incoming webhook events.

Why this exists
---------------
Hindsight's outbox wants a prompt HTTP response. A full reconcile takes
10-70s because it makes an LLM ``reflect`` call, so answering
synchronously blew past the outbox timeout and triggered its retry
ladder (observed intervals: +5min, +30min, +2h, +5h). Every retry
re-ran reflect on the same fact, giving the LLM repeated chances to
return a larger superseded-id set. On 2026-07-30/31 one fact was
reconciled 24 times and the invalidation count escalated
1 -> 1 -> 10 -> 25.

The fix is two-part and both parts live here:

  1. Answer 200 as soon as the payload is authenticated; run the
     reconcile on a single background worker.
  2. Drop replays via a dedup table, so a retry that is already in
     flight (or already finished) never starts a second reconcile.

Why the dedup key is a hash of the raw body
-------------------------------------------
* ``operation_id`` is optional -- ``parse_event`` tolerates its absence,
  so it cannot be relied on as the key.
* Retries are byte-identical (same operation_id *and* same payload
  timestamp), so the body hash matches exactly.
* ``document_id`` would be actively wrong: the same document can
  legitimately be ingested more than once, and dropping that would
  silently skip real work.

Why a single worker
-------------------
The incident log shows interleaved reconciles corrupting each other's
log lines (a "processed document=X" line reporting a different document
than the one actually reconciled). Serial execution keeps the log
readable, avoids concurrent reflect calls racing on the same bank, and
acts as natural rate limiting. Throughput is irrelevant here -- nothing
waits on this cleanup.

Crash semantics
---------------
A key is marked ``in_flight`` at enqueue time and ``done`` when the
worker finishes. If the process dies mid-flight the key stays
``in_flight`` and that event is never reprocessed: we accept losing one
cleanup pass, which merely leaves a memory un-curated. The alternative
(marking only on completion) would let a retry arriving mid-flight
start a second concurrent reconcile of the same fact -- exactly the
failure mode that caused the incident.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

log = logging.getLogger("hindsight_memorial.dispatch")

# Dedup table size. Bounded by count rather than by wall-clock TTL on
# purpose: the retry window runs to at least 5 hours, and any TTL short
# enough to be useful risks expiring a key moments before its retry
# lands. A count cap has no such edge.
DEFAULT_MAX_KEYS = 1000

IN_FLIGHT = "in_flight"
DONE = "done"

# Queue sentinel that tells the worker to exit.
_STOP = object()

# submit() outcomes
QUEUED = "queued"
DUPLICATE = "duplicate"


def event_key(raw_body: bytes) -> str:
    """Idempotency key for one webhook delivery: sha256 of the raw body."""
    return hashlib.sha256(raw_body).hexdigest()


class Dispatcher:
    """Serial background executor with body-hash idempotency.

    ``process(raw_body, headers)`` is the slow work (in production:
    ``webhook_handlers.handle_event``). It runs on the worker thread; its
    return value is logged, not returned to the HTTP caller, because the
    caller has already been answered by then.
    """

    def __init__(
        self,
        process: Callable[[bytes, dict[str, str]], Any],
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._process = process
        self._max_keys = max_keys
        self._seen: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="memorial-dispatch",
            daemon=True,
        )
        self._thread.start()
        log.info("dispatch worker started (max_keys=%d)", self._max_keys)

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to finish the current item and exit."""
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("dispatch worker did not exit within %.1fs", timeout)
        self._thread = None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until the queue drains. For tests; returns False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False

    # ── submission ───────────────────────────────────────────────────

    def submit(self, raw_body: bytes, headers: dict[str, str]) -> str:
        """Enqueue one delivery unless it is a replay.

        Returns ``QUEUED`` or ``DUPLICATE``. Dedup happens *before*
        enqueue -- admitting duplicates to the queue would only defer the
        problem, not solve it.
        """
        key = event_key(raw_body)
        with self._lock:
            prior = self._seen.get(key)
            if prior is not None:
                log.info(
                    "duplicate delivery dropped: key=%s prior_state=%s bytes=%d",
                    key[:12],
                    prior,
                    len(raw_body),
                )
                return DUPLICATE
            self._seen[key] = IN_FLIGHT
            self._evict_locked()

        self._queue.put((key, raw_body, headers))
        log.info(
            "event queued: key=%s bytes=%d queue_depth=%d",
            key[:12],
            len(raw_body),
            self._queue.qsize(),
        )
        return QUEUED

    def _evict_locked(self) -> None:
        """Trim the dedup table to ``max_keys``. Caller must hold the lock."""
        while len(self._seen) > self._max_keys:
            evicted_key, evicted_state = self._seen.popitem(last=False)
            # Worth a log line: an eviction while still in_flight means the
            # table is too small for the current retry window, and a replay
            # of that event would be processed a second time.
            if evicted_state == IN_FLIGHT:
                log.warning(
                    "evicted an in_flight dedup key (table full at %d) key=%s",
                    self._max_keys,
                    evicted_key[:12],
                )

    # ── worker ───────────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    log.info("dispatch worker stopping")
                    return
                key, raw_body, headers = item
                self._process_one(key, raw_body, headers)
            except BaseException:
                # The worker thread must never die: if it does, every
                # subsequent event queues up and is silently never
                # processed. _process_one already catches Exception, so
                # reaching here means something more serious.
                log.critical(
                    "dispatch worker caught a fatal error and is continuing",
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    def _process_one(
        self, key: str, raw_body: bytes, headers: dict[str, str]
    ) -> None:
        started = time.monotonic()
        log.info("processing start: key=%s", key[:12])
        try:
            outcome = self._process(raw_body, headers)
        except Exception:
            # Full traceback into the log file. The HTTP caller was
            # answered long ago, so this log line is the only record.
            log.exception(
                "processing failed: key=%s elapsed=%.1fs",
                key[:12],
                time.monotonic() - started,
            )
        else:
            log.info(
                "processing done: key=%s elapsed=%.1fs outcome=%s",
                key[:12],
                time.monotonic() - started,
                _summarise(outcome),
            )
        finally:
            with self._lock:
                # Only downgrade a key we still own -- eviction may have
                # dropped it while the reconcile was running.
                if key in self._seen:
                    self._seen[key] = DONE

    # ── introspection (tests / healthz) ──────────────────────────────

    def stats(self) -> dict[str, int]:
        with self._lock:
            in_flight = sum(1 for v in self._seen.values() if v == IN_FLIGHT)
            return {
                "keys": len(self._seen),
                "in_flight": in_flight,
                "queue_depth": self._queue.qsize(),
            }

    def state_of(self, raw_body: bytes) -> str | None:
        with self._lock:
            return self._seen.get(event_key(raw_body))


def _summarise(outcome: Any) -> str:
    """Compact one-line rendering of a WebhookOutcome for the log."""
    status = getattr(outcome, "status", None)
    if status is None:
        return repr(outcome)[:200]
    return (
        f"status={status} bank={getattr(outcome, 'bank_id', None)} "
        f"document={getattr(outcome, 'document_id', None)} "
        f"units={getattr(outcome, 'units_processed', 0)} "
        f"superseded={getattr(outcome, 'total_superseded', 0)} "
        f"observations_cleared={getattr(outcome, 'total_observations_cleared', 0)}"
    )


__all__ = [
    "DEFAULT_MAX_KEYS",
    "DONE",
    "DUPLICATE",
    "Dispatcher",
    "IN_FLIGHT",
    "QUEUED",
    "event_key",
]
