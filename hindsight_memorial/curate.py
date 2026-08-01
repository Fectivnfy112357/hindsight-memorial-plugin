"""Curate (soft-delete + clear derived observations) for one or more memory_units.

This is the "cleanup half" of memorial: given a list of superseded UUIDs, run PATCH invalidate +
DELETE observations on each one. Failures are isolated per-id so one bad id doesn't abort the rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import db
from .client import HindsightAPIError, HindsightClient

log = logging.getLogger("hindsight_memorial.curate")


@dataclass(frozen=True)
class CurateResult:
    """Outcome of curating a single memory_id."""

    memory_id: str
    invalidated: bool
    observations_cleared: bool
    error: str | None = None


@dataclass
class CurateReport:
    """Aggregate result across a batch of memory_ids."""

    results: list[CurateResult] = field(default_factory=list)

    @property
    def invalidated_count(self) -> int:
        return sum(1 for r in self.results if r.invalidated)

    @property
    def observations_cleared_count(self) -> int:
        return sum(1 for r in self.results if r.observations_cleared)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.results),
            "invalidated": self.invalidated_count,
            "observations_cleared": self.observations_cleared_count,
            "errors": self.error_count,
            "results": [
                {
                    "memory_id": r.memory_id,
                    "invalidated": r.invalidated,
                    "observations_cleared": r.observations_cleared,
                    "error": r.error,
                }
                for r in self.results
            ],
        }


def curate_memory(
    client: HindsightClient,
    bank_id: str,
    memory_id: str,
    *,
    reason: str,
) -> CurateResult:
    """Soft-delete one memory and clear its derived observations.

    Strategy:
      1. PATCH state=invalidated (memory disappears from recall but is recoverable)
      2. DELETE /memories/{id}/observations (clear derived observations)

    Step 1 failing aborts step 2 — there's no point clearing observations if the fact itself is
    still live.
    """
    try:
        client.update_memory(bank_id, memory_id, state="invalidated", reason=reason)
    except HindsightAPIError as e:
        return CurateResult(
            memory_id=memory_id,
            invalidated=False,
            observations_cleared=False,
            error=f"patch failed: {e.status} {e.body[:200]}",
        )

    try:
        client.clear_memory_observations(bank_id, memory_id)
    except HindsightAPIError as e:
        # The fact is invalidated; just couldn't clear observations. Surface the partial state.
        return CurateResult(
            memory_id=memory_id,
            invalidated=True,
            observations_cleared=False,
            error=f"observations clear failed: {e.status} {e.body[:200]}",
        )

    return CurateResult(memory_id=memory_id, invalidated=True, observations_cleared=True)


def curate_many(
    client: HindsightClient,
    bank_id: str,
    memory_ids: list[str],
    *,
    reason: str,
) -> CurateReport:
    """Curate a list of memory_ids. Each id is attempted independently.

    Order is preserved for stable reporting; concurrency is left to the caller (typically the main
    entry point, which may use a thread pool if latency matters).
    """
    report = CurateReport()
    for mid in memory_ids:
        report.results.append(curate_memory(client, bank_id, mid, reason=reason))
    return report


def curate_superseded_in_db(
    conn,
    bank_id: str,
    unit_ids: list[str],
    *,
    reason: str,
) -> int:
    """Soft-mark local ``memory_units`` rows whose ids appear in
    ``unit_ids`` as superseded.

    This is the local-table mirror of :func:`curate_many`. After the
    Hindsight side has invalidated the matching facts there, the local
    rows that referenced those ids are flipped to ``status='superseded'``
    with the reflect reasoning (or a short summary) recorded in
    ``superseded_reason``.

    Eligibility (``status IN ('pending','processed')``) deliberately
    excludes 'processing' so we never overwrite the very row currently
    being reconciled. The poller calls this *after* marking its row
    'processing', so its own id is in 'processing' and is therefore safe
    even if it appears in the supersede list by accident (defence in
    depth on top of the ``exclude_ids`` filter in reflect).

    Returns the number of local rows actually flipped, mainly for
    logging.
    """
    if not unit_ids:
        return 0
    return db.mark_superseded_on_conn(conn, bank_id, unit_ids, reason=reason)


__all__ = [
    "CurateReport",
    "CurateResult",
    "curate_many",
    "curate_memory",
    "curate_superseded_in_db",
]