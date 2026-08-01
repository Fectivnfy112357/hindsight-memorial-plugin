"""Background reconciler poller.

The poller is the slow half of the new architecture. It runs as a
daemon thread, repeatedly picking the most-recent ``pending`` row from
the local ``memory_units`` table, calling :func:`run_reconcile` against
it, and updating the row to one of the terminal states:

  - ``processed``     — reflect + curate succeeded (or no supersede was found)
  - ``superseded``    — superseded by an *earlier* reconcile; written when
                        the in-flight row's reflect verdict names other
                        local rows
  - ``failed``        — ``run_reconcile`` raised an exception, or returned
                        ``reflect_failed``. The short reason is recorded
                        in ``failure_reason``; the full traceback goes to
                        the log.

Crash semantics: the poller marks a row ``processing`` before invoking
``run_reconcile``. If the process is killed mid-reconcile, the row is
left in ``processing``. This is accepted as the lesser evil — the
alternative (marking only on completion) would risk a second poller
re-running reflect on the same fact if the daemon is later restarted
in a multi-process deployment.

Lifecycle: ``start()`` is idempotent. ``stop(timeout)`` requests a
graceful shutdown and joins the thread; if the join times out, a
warning is logged but the process is allowed to exit anyway. The
daemon thread will not block process exit.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import db
from .curate import curate_superseded_in_db

log = logging.getLogger("hindsight_memorial.poller")


# Mirror the ReconcileResult from reconcile without taking a hard
# dependency on the module at import time (avoids circular imports in
# tests that monkeypatch reconcile).
ReconcileFn = Callable[[str, str, str], object]


class ReconcilerPoller:
    """Single-threaded drain of the ``memory_units`` queue."""

    def __init__(
        self,
        conn,
        run_reconcile: ReconcileFn,
        *,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self._conn = conn
        self._run_reconcile = run_reconcile
        self._poll_interval_sec = poll_interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="memorial-reconciler", daemon=True
        )
        self._thread.start()
        log.info(
            "reconciler poller started (interval=%.2fs)", self._poll_interval_sec
        )

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the worker to finish the current row and exit."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.warning("reconciler poller did not exit within %.1fs", timeout)
        else:
            log.info("reconciler poller stopped")
        self._thread = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── worker loop ────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:
                # The worker thread must never die: if it does, every
                # subsequent row sits at 'pending' forever. run_once
                # already catches per-row exceptions, so reaching here
                # is something more serious (e.g. DB connection lost).
                log.exception("reconciler poller caught a fatal error; backing off")
                # Back off so we don't busy-loop on a broken DB.
                self._stop_event.wait(5.0)
                continue
            if not processed:
                # No work — wait for the next tick, but also wake up
                # promptly when stop() is requested.
                self._stop_event.wait(self._poll_interval_sec)

    # ── one iteration (synchronous, testable) ──────────────────────

    def run_once(self) -> bool:
        """Process one row. Returns True if a row was processed, False
        if the queue was empty. Safe to call directly from tests.
        """
        row = db.fetch_pending_row_on_conn(self._conn)
        if row is None:
            return False

        bank_id = row["bank_id"]
        unit_id = row["unit_id"]
        content = row["content"]
        row_id = row["id"]

        # Flip to 'processing' first so concurrent observers (e.g. /healthz)
        # can tell that this row is in flight.
        self._conn.execute(
            "UPDATE memory_units SET status='processing' WHERE id=?",
            (row_id,),
        )
        self._conn.commit()

        try:
            result = self._run_reconcile(bank_id, unit_id, content)
        except Exception as e:
            short = f"{type(e).__name__}: {str(e)[:200]}"
            log.exception("reconcile raised for bank=%s unit=%s", bank_id, unit_id)
            db.mark_failed_on_conn(
                self._conn, bank_id, unit_id, reason=short
            )
            return True

        # Mark the row's terminal state. We only flip to 'failed' when
        # the reconcile reported it; otherwise we go to 'processed' even
        # if the verdict was 'abandoned' (no supersede) — that is the
        # normal "I looked, nothing to do" outcome.
        status = getattr(result, "status", None)
        if status in ("reflect_failed", "error", "list_banks_failed"):
            err = getattr(result, "error", None) or "reconcile reported failure"
            db.mark_failed_on_conn(
                self._conn, bank_id, unit_id, reason=str(err)[:500]
            )
            return True

        # Soft-mark any local rows the verdict named as superseded.
        results = getattr(result, "results", None) or []
        superseded_ids: list[str] = []
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("memory_id"), str):
                superseded_ids.append(r["memory_id"])
        if superseded_ids:
            curate_superseded_in_db(
                self._conn,
                bank_id,
                superseded_ids,
                reason=str(getattr(result, "reason", "") or "")[:500]
                or "superseded by newly retained fact",
            )

        db.mark_processed_on_conn(self._conn, bank_id, unit_id)
        return True


__all__ = ["ReconcilerPoller"]
