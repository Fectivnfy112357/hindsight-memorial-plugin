"""Shared retain → reflect → curate pipeline used by the webhook entry point.

Flow:

  1. Build a HindsightClient from MemorialConfig.
  2. Verify the resolved bank exists on the server.
  3. Run reflect() **once** to ask which existing facts the new one supersedes.
  4. Curate any UUIDs identified as superseded.

Why a single attempt: the webhook fires *after* ``retain.completed``, i.e. the
server has already committed and indexed the new fact. The old 30s initial
delay + retry loop existed because hooks fired *before* indexing caught up —
no longer applicable. If reflect itself fails, callers should treat it as a
hard failure and re-drive from the webhook later; we surface the error in
the result.
"""
from __future__ import annotations

import logging
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


@dataclass
class ReconcileResult:
    """Outcome of a single retain → reconcile pass."""

    status: str  # "ok" | "abandoned" | "skipped" | "list_banks_failed" | "reflect_failed" | "error"
    superseded_count: int = 0
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


# Type alias for the per-call config loader. The webhook entry point
# implements this to resolve the bank id from the event payload + env.
ConfigLoader = Callable[[str | None], MemorialConfig | None]


def run_reconcile(
    new_fact: str,
    *,
    load_cfg: ConfigLoader,
    cwd: str | None = None,
    dry_run: bool = False,
    exclude_unit_ids: list[str] | None = None,
) -> ReconcileResult:
    """Run the shared retain → reconcile pipeline.

    ``load_cfg`` is a webhook-supplied callable that takes ``cwd`` and
    returns a MemorialConfig (or None to mean "no config / skip cleanly").
    The pipeline itself does not care how bank ids are resolved; it only
    takes the resolved MemorialConfig and runs reflect+curate against it.

    ``exclude_unit_ids`` is forwarded to ``extract_superseded_ids`` so the
    just-retained fact's own id is filtered out of the reflect response —
    otherwise the reflect LLM sometimes lists the new fact alongside the
    ones it supersedes and memorial would PATCH-invalidate the very fact
    it just wrote.
    """
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
    except Exception:  # pragma: no cover - depends on env
        # Use log.exception so the full traceback lands in the log file.
        log.exception("client init failed bank=%s", cfg.bank_id)
        return ReconcileResult(status="error", error="client init failed (see logs)")

    try:
        bank_ids = client.list_banks()
    except HindsightAPIError as e:
        log.warning("list_banks failed bank=%s: %s", cfg.bank_id, e)
        return ReconcileResult(
            status="list_banks_failed",
            error=str(e),
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
        )

    if cfg.bank_id not in bank_ids:
        log.info(
            "bank '%s' not present on server (resolved via %s, %d available)",
            cfg.bank_id,
            cfg.bank_source,
            len(bank_ids),
        )
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

    try:
        reflect_resp = client.reflect(
            cfg.bank_id,
            query,
            structured_output=SUPERSEDED_SCHEMA,
            include_based_on=False,
        )
    except HindsightAPIError as e:
        log.warning("reflect failed: %s", e)
        return ReconcileResult(
            status="reflect_failed",
            error=str(e),
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
            new_fact_preview=new_fact[:120],
        )

    superseded = extract_superseded_ids(reflect_resp, exclude_ids=exclude_unit_ids)
    if not superseded:
        return ReconcileResult(
            status="abandoned",
            reason="no superseded facts detected",
            bank_id=cfg.bank_id,
            bank_source=cfg.bank_source,
            new_fact_preview=new_fact[:120],
        )

    return _curate_and_return(
        client=client,
        cfg=cfg,
        new_fact=new_fact,
        superseded=superseded,
    )


def _curate_and_return(
    *,
    client: HindsightClient,
    cfg: MemorialConfig,
    new_fact: str,
    superseded: list[str],
) -> ReconcileResult:
    reason = f"Superseded by newly retained fact: {new_fact[:200]}"
    report: CurateReport = curate_many(
        client, cfg.bank_id, superseded, reason=reason
    )
    return ReconcileResult(
        status="ok",
        superseded_count=len(superseded),
        bank_id=cfg.bank_id,
        bank_source=cfg.bank_source,
        new_fact_preview=new_fact[:120],
        observations_cleared=report.observations_cleared_count,
        errors=report.error_count,
        results=[asdict(r) for r in report.results],
    )


__all__ = [
    "ConfigLoader",
    "ReconcileResult",
    "run_reconcile",
]
