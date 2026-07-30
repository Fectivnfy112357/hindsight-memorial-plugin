"""Shared retain → reflect → curate pipeline used by both adapters.

Adapters (Claude Code CLI, Hermes plugin) all need the same logic:

  1. Build a HindsightClient from MemorialConfig.
  2. Verify the resolved bank exists on the server.
  3. Run reflect() with a bounded retry to absorb write→index visibility lag.
  4. Curate any UUIDs identified as superseded.

The only adapter-specific pieces are config loading and (for the CLI) how the
new_fact and cwd arrive. Both adapters call `run_reconcile(new_fact, *,
load_cfg, ...)` so the pipeline lives in exactly one place.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .client import HindsightAPIError, HindsightClient
from .config import MemorialConfig
from .curate import CurateReport, curate_many
from .reflect_query import (
    SUPERSEDED_SCHEMA,
    build_query,
    extract_superseded_ids,
)

log = logging.getLogger("hindsight_memorial.reconcile")

# Reflect retry policy.
#
# After a retain lands, the new fact may not be queryable via reflect for a
# short window (write→index visibility lag on the Hindsight backend). The
# pipeline below implements the user-specified semantics:
#
#   1. Sleep RECONCILE_INITIAL_DELAY seconds, then run reflect() once.
#      If that returns 1+ superseded UUIDs, curate them and return — no
#      further attempts.
#   2. If reflect raised an error OR returned no superseded UUIDs, sleep
#      the next value from RECONCILE_RETRY_DELAYS, then try reflect() again.
#   3. Repeat step 2 for as many retry delays as are configured. After the
#      last delay, return status="abandoned" or "reflect_failed".
#
# RECONCILE_RETRY_DELAYS is a tuple so adding a third retry is a one-line
# change. The tuple's length is the maximum number of *retries* — the first
# attempt is not counted as a retry because the user wants it preceded by
# RECONCILE_INITIAL_DELAY only.
RECONCILE_INITIAL_DELAY = 30.0
RECONCILE_RETRY_DELAYS = (30.0, 30.0)


@dataclass
class ReconcileResult:
    """Outcome of a single retain → reconcile pass."""

    status: str  # "ok" | "abandoned" | "skipped" | "list_banks_failed" | "reflect_failed" | "error"
    superseded_count: int = 0
    attempts: int = 0
    elapsed_seconds: float = 0.0
    bank_id: str | None = None
    bank_source: str | None = None
    new_fact_preview: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    observations_cleared: int = 0
    errors: int = 0
    error: str | None = None
    reason: str | None = None
    available_bank_count: int | None = None
    query_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "superseded_count": self.superseded_count,
            "attempts": self.attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "bank_id": self.bank_id,
            "bank_source": self.bank_source,
        }
        if self.new_fact_preview:
            out["new_fact_preview"] = self.new_fact_preview
        if self.results:
            out["results"] = self.results
        if self.observations_cleared:
            out["observations_cleared"] = self.observations_cleared
        if self.errors:
            out["errors"] = self.errors
        if self.error:
            out["error"] = self.error
        if self.reason:
            out["reason"] = self.reason
        if self.available_bank_count is not None:
            out["available_bank_count"] = self.available_bank_count
        if self.query_preview:
            out["query_preview"] = self.query_preview
        return out


# Type alias for the per-adapter config loader. Adapters implement this to
# surface their own bank-id resolution rules (Hermes: env > config > cwd
# directoryBankMap; Claude Code: env > ~/.hindsight/claude-code.json > cwd).
ConfigLoader = Callable[[str | None], MemorialConfig | None]


def run_reconcile(
    new_fact: str,
    *,
    load_cfg: ConfigLoader,
    cwd: str | None = None,
    dry_run: bool = False,
    sleep_fn: Callable[[float], None] | None = None,
) -> ReconcileResult:
    """Run the shared retain → reconcile pipeline.

    ``load_cfg`` is an adapter-supplied callable that takes ``cwd`` and
    returns a MemorialConfig (or None to mean "no config / skip cleanly").
    The pipeline itself does not care how bank ids are resolved; it only
    takes the resolved MemorialConfig and runs reflect+curate against it.

    ``sleep_fn`` defaults to ``time.sleep`` looked up per call, not at
    import time, so tests can ``mock.patch`` it cleanly.
    """
    if sleep_fn is None:
        sleep_fn = _time.sleep
    if not new_fact or not new_fact.strip():
        return ReconcileResult(
            status="skipped",
            reason="no new_fact supplied",
        )

    cfg = load_cfg(cwd)
    if cfg is None:
        return ReconcileResult(
            status="skipped",
            reason="no config loader returned a MemorialConfig",
        )
    if not cfg.bank_id:
        return ReconcileResult(
            status="skipped",
            reason=(
                f"no bank_id resolved (config bank_source={cfg.bank_source})"
            ),
        )
    if not cfg.api_url:
        return ReconcileResult(
            status="skipped",
            reason="no api_url configured",
        )

    try:
        client = HindsightClient.from_memorial_config(cfg)
    except Exception as e:  # pragma: no cover - depends on env
        return ReconcileResult(status="error", error=f"client init failed: {e}")

    try:
        bank_ids = client.list_banks()
    except HindsightAPIError as e:
        return ReconcileResult(
            status="list_banks_failed",
            error=str(e),
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
        )

    if cfg.bank_id not in bank_ids:
        return ReconcileResult(
            status="skipped",
            reason=(
                f"bank '{cfg.bank_id}' not present on server "
                f"(resolved via {cfg.bank_source})"
            ),
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
            available_bank_count=len(bank_ids),
        )

    query = build_query(new_fact, bank_id=cfg.bank_id)

    if dry_run:
        return ReconcileResult(
            status="dry_run",
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
            new_fact_preview=new_fact[:120],
            query_preview=query[:200],
        )

    # Attempt 1: wait INITIAL, reflect, return as soon as we have a hit.
    attempts = 0
    total_delay = 0.0
    superseded: list[str] = []
    last_error: str | None = None

    sleep_fn(RECONCILE_INITIAL_DELAY)
    total_delay += RECONCILE_INITIAL_DELAY
    attempts = 1

    try:
        reflect_resp = client.reflect(
            cfg.bank_id,
            query,
            structured_output=SUPERSEDED_SCHEMA,
            include_based_on=False,
        )
    except HindsightAPIError as e:
        last_error = str(e)
        log.warning("reflect attempt %d failed: %s", attempts, e)
    else:
        superseded = extract_superseded_ids(reflect_resp)
        if superseded:
            return _curate_and_return(
                client=client,
                cfg=cfg,
                new_fact=new_fact,
                superseded=superseded,
                attempts=attempts,
                total_delay=total_delay,
            )

    # Retries: each entry in RECONCILE_RETRY_DELAYS adds (sleep + reflect).
    for delay in RECONCILE_RETRY_DELAYS:
        sleep_fn(delay)
        total_delay += delay
        attempts += 1

        try:
            reflect_resp = client.reflect(
                cfg.bank_id,
                query,
                structured_output=SUPERSEDED_SCHEMA,
                include_based_on=False,
            )
        except HindsightAPIError as e:
            last_error = str(e)
            log.warning("reflect attempt %d failed: %s", attempts, e)
            continue

        superseded = extract_superseded_ids(reflect_resp)
        if superseded:
            return _curate_and_return(
                client=client,
                cfg=cfg,
                new_fact=new_fact,
                superseded=superseded,
                attempts=attempts,
                total_delay=total_delay,
            )

    if last_error is not None:
        return ReconcileResult(
            status="reflect_failed",
            error=last_error,
            attempts=attempts,
            elapsed_seconds=total_delay,
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
            new_fact_preview=new_fact[:120],
        )

    return ReconcileResult(
        status="abandoned",
        reason=(
            f"no superseded facts detected within retry window "
            f"({attempts} attempt(s), {total_delay:.0f}s elapsed)"
        ),
        attempts=attempts,
        elapsed_seconds=total_delay,
        bank_id=cfg.bank_id,
        bank_source=cfg.bank_source,
        new_fact_preview=new_fact[:120],
    )


def _curate_and_return(
    *,
    client: HindsightClient,
    cfg: MemorialConfig,
    new_fact: str,
    superseded: list[str],
    attempts: int,
    total_delay: float,
) -> ReconcileResult:
    reason = f"Superseded by newly retained fact: {new_fact[:200]}"
    report: CurateReport = curate_many(
        client, cfg.bank_id, superseded, reason=reason
    )
    return ReconcileResult(
        status="ok",
        superseded_count=len(superseded),
        attempts=attempts,
        elapsed_seconds=total_delay,
        bank_id=cfg.bank_id,
        bank_source=cfg.bank_source,
        new_fact_preview=new_fact[:120],
        observations_cleared=report.observations_cleared_count,
        errors=report.error_count,
        results=[asdict(r) for r in report.results],
    )


__all__ = [
    "ConfigLoader",
    "RECONCILE_REFLECT_DELAYS",
    "ReconcileResult",
    "run_reconcile",
]
