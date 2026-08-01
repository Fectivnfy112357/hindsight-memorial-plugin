"""Shared retain → reflect → curate pipeline used by the poller.

Flow:

  1. Build a HindsightClient from MemorialConfig.
  2. Verify the resolved bank exists on the server.
  3. Run reflect() **once** to ask which existing facts the new one supersedes.
  4. Curate any UUIDs identified as superseded (PATCH state=invalidated
     + DELETE observations).

Why a single attempt: the webhook fires *after* ``retain.completed``, i.e. the
server has already committed and indexed the new fact. The old 30s initial
delay + retry loop existed because hooks fired *before* indexing caught up —
no longer applicable. If reflect itself fails, callers should treat it as a
hard failure and re-drive from the webhook later; we surface the error in
the result.

Note: the pipeline is now driven by the poller, one row at a time, from the
local ``memory_units`` table. The function therefore takes the
``(bank_id, unit_id, content)`` triple explicitly instead of an event object,
and computes ``exclude_unit_ids=[unit_id]`` internally — see commit a4ac52d
for the original defence against the LLM listing the just-retained fact
itself in its own supersede list.
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
    bank_id: str,
    unit_id: str,
    content: str,
    *,
    load_cfg: ConfigLoader,
    dry_run: bool = False,
) -> ReconcileResult:
    """Run the shared retain → reconcile pipeline for a single memory unit.

    The poller drives this function once per pending row in the local
    ``memory_units`` table. The freshly retained unit's id is
    automatically excluded from the reflect verdict (see commit
    a4ac52d — without this, the reflect LLM sometimes lists the new
    fact itself alongside the ones it supersedes, and memorial would
    PATCH-invalidate the very fact it just wrote).

    ``load_cfg`` is a webhook/supplied callable that takes ``cwd`` and
    returns a MemorialConfig (or None to mean "no config / skip cleanly").
    The pipeline itself does not care how bank ids are resolved; it only
    takes the resolved MemorialConfig and runs reflect+curate against it.
    """
    if not content or not content.strip():
        return ReconcileResult(
            status="skipped",
            reason="no content supplied",
        )

    cfg = load_cfg(None)
    if cfg is None:
        return ReconcileResult(
            status="skipped",
            reason="no config loader returned a MemorialConfig",
        )
    if not cfg.api_url:
        return ReconcileResult(
            status="skipped",
            reason="no api_url configured",
        )

    # Bank id is the source of truth from the caller; we deliberately
    # do NOT re-resolve it via cwd (the old behaviour). The
    # MemorialConfig's bank_id is ignored for routing; we use the
    # explicit parameter so a misconfigured MemorialConfig can't
    # accidentally route a unit to the wrong bank.
    effective_bank_id = bank_id
    effective_bank_source = "caller"

    if not effective_bank_id:
        return ReconcileResult(
            status="skipped",
            reason="no bank_id supplied",
        )

    try:
        client = HindsightClient.from_memorial_config(
            MemorialConfig(
                api_url=cfg.api_url,
                api_key=cfg.api_key,
                bank_id=effective_bank_id,
                bank_source=effective_bank_source,
            )
        )
    except Exception:  # pragma: no cover - depends on env
        # Use log.exception so the full traceback lands in the log file.
        log.exception("client init failed bank=%s", effective_bank_id)
        return ReconcileResult(status="error", error="client init failed (see logs)")

    try:
        bank_ids = client.list_banks()
    except HindsightAPIError as e:
        log.warning("list_banks failed bank=%s: %s", effective_bank_id, e)
        return ReconcileResult(
            status="list_banks_failed",
            error=str(e),
            bank_id=effective_bank_id,
            bank_source=effective_bank_source,
        )

    if effective_bank_id not in bank_ids:
        log.info(
            "bank '%s' not present on server (resolved via %s, %d available)",
            effective_bank_id,
            effective_bank_source,
            len(bank_ids),
        )
        return ReconcileResult(
            status="skipped",
            reason=(
                f"bank '{effective_bank_id}' not present on server "
                f"(resolved via {effective_bank_source})"
            ),
            bank_id=effective_bank_id,
            bank_source=effective_bank_source,
            available_bank_count=len(bank_ids),
        )

    query = build_query(content, bank_id=effective_bank_id)

    if dry_run:
        return ReconcileResult(
            status="dry_run",
            bank_id=effective_bank_id,
            bank_source=effective_bank_source,
            new_fact_preview=content[:120],
            query_preview=query[:200],
        )

    try:
        reflect_resp = client.reflect(
            effective_bank_id,
            query,
            structured_output=SUPERSEDED_SCHEMA,
        )
    except HindsightAPIError as e:
        log.warning("reflect failed: %s", e)
        return ReconcileResult(
            status="reflect_failed",
            error=str(e),
            bank_id=effective_bank_id,
            bank_source=effective_bank_source,
            new_fact_preview=content[:120],
        )

    # structured_only=True (the default) pins the 2026-08-01 fix for
    # issue #2: an empty structured list must never fall through to
    # scanning reasoning prose for UUIDs.
    superseded = extract_superseded_ids(
        reflect_resp,
        exclude_ids=[unit_id] if unit_id else None,
        structured_only=True,
    )

    # Log what reflect actually said, before acting on it. During the
    # 2026-07-30 incident one reflect call returned 25 ids and every one was
    # invalidated; because the reasoning was never recorded there is no way
    # to reconstruct why those ids were chosen. Never act on an LLM verdict
    # without first writing the verdict down.
    structured = reflect_resp.get("structured_output")
    reasoning = ""
    raw_id_count = 0
    if isinstance(structured, dict):
        if isinstance(structured.get("reasoning"), str):
            reasoning = structured["reasoning"]
        raw_ids = structured.get("superseded_fact_ids")
        if isinstance(raw_ids, list):
            raw_id_count = len(raw_ids)
    log.info(
        "reflect verdict: bank=%s unit=%s raw_ids=%d kept_ids=%d ids=%s reasoning=%r",
        effective_bank_id,
        unit_id,
        raw_id_count,
        len(superseded),
        superseded,
        reasoning[:1000],
    )

    if not superseded:
        return ReconcileResult(
            status="abandoned",
            reason="no superseded facts detected",
            bank_id=effective_bank_id,
            bank_source=effective_bank_source,
            new_fact_preview=content[:120],
        )

    return _curate_and_return(
        client=client,
        bank_id=effective_bank_id,
        content=content,
        superseded=superseded,
        reasoning=reasoning,
    )


def _curate_and_return(
    *,
    client: HindsightClient,
    bank_id: str,
    content: str,
    superseded: list[str],
    reasoning: str,
) -> ReconcileResult:
    """Run Hindsight-side curation, returning a top-level ok/abandoned result.

    The reason string passed to ``invalidate_memory`` is the reflect
    LLM's reasoning (truncated). The Hindsight side stores this in
    ``invalidated_memory_units.invalidation_reason`` for audit.
    """
    reason = reasoning[:200] if reasoning else f"Superseded by newly retained fact: {content[:200]}"
    report: CurateReport = curate_many(client, bank_id, superseded, reason=reason)
    return ReconcileResult(
        status="ok",
        superseded_count=len(superseded),
        bank_id=bank_id,
        bank_source="caller",
        new_fact_preview=content[:120],
        observations_cleared=report.observations_cleared_count,
        errors=report.error_count,
        results=[asdict(r) for r in report.results],
    )


__all__ = [
    "ConfigLoader",
    "ReconcileResult",
    "run_reconcile",
]
