"""Webhook entry point for hindsight-memorial.

The Hindsight server POSTs ``retain.completed`` events here after each
ingest. For each event we:

  1. Verify the HMAC-SHA256 signature in ``X-Hindsight-Signature`` against
     the raw request body using the shared secret.
  2. Look up the freshly retained memory_units for ``data.document_id``
     via ``GET /v1/default/banks/{bank_id}/memories/list?document_id=...``.
  3. Run ``reconcile.run_reconcile`` **once per memory_unit** — facts in the
     same document are not necessarily mutually consistent, so we deliberately
     avoid fusing them into a single reflect query.

The handler is a plain function over ``(raw_body, headers, secret)`` so it
has no HTTP framework dependency and can be exercised end-to-end with
``FakeClient`` in tests.

Reference: Hindsight source ``hindsight_api/webhooks/manager.py`` for the
signature scheme (``sha256=<hex>`` HMAC over the raw POST body).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import MemorialConfig
from .reconcile import ConfigLoader, ReconcileResult, run_reconcile

log = logging.getLogger("hindsight_memorial.webhook_handlers")

# Header literal from hindsight-api-slim/hindsight_api/engine/memory_engine.py:2322-2328
SIGNATURE_HEADER = "X-Hindsight-Signature"
EVENT_HEADER = "X-Hindsight-Event"


# ── payload model ────────────────────────────────────────────────────────


@dataclass
class RetainEvent:
    """Parsed retain.completed webhook event."""

    event: str
    bank_id: str
    operation_id: str | None
    document_id: str
    memory_unit_count: int


# ── result aggregation ───────────────────────────────────────────────────


@dataclass
class WebhookOutcome:
    """Aggregate outcome of processing one retain.completed event."""

    status: str  # "ok" | "abandoned" | "skipped" | "reflect_failed" | "error" | "ignored"
    bank_id: str | None = None
    document_id: str | None = None
    memory_unit_count: int = 0
    units_processed: int = 0
    units_skipped: int = 0
    total_superseded: int = 0
    total_observations_cleared: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── signature verification ───────────────────────────────────────────────


def verify_signature(raw_body: bytes, header_value: str | None, secret: bytes) -> bool:
    """Return True iff ``header_value`` matches ``sha256=<hmac(secret, raw_body)>``.

    The header literal must be exactly ``sha256=<lowercase hex>`` (per the
    hindsight source). Any deviation — wrong prefix, uppercase hex, trailing
    whitespace already stripped by the HTTP layer — is rejected.
    """
    if not header_value:
        return False
    expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.strip())


# ── payload parsing ──────────────────────────────────────────────────────


def parse_event(raw_body: bytes) -> RetainEvent | None:
    """Parse + minimally validate the webhook body. Returns None on any error.

    Schema (from hindsight-api-slim/hindsight_api/webhooks/models.py):

        {
          "event":      "retain.completed",
          "bank_id":    "...",
          "operation_id": "...",
          "status":     "...",
          "timestamp":  "...",
          "data":       {"document_id": "...", "tags": [...], "memory_unit_count": N}
        }
    """
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("event") != "retain.completed":
        return None
    bank_id = body.get("bank_id")
    data = body.get("data")
    if not isinstance(bank_id, str) or not isinstance(data, dict):
        return None
    document_id = data.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        return None
    count = data.get("memory_unit_count")
    if not isinstance(count, int):
        count = 0
    operation_id = body.get("operation_id")
    if not isinstance(operation_id, str):
        operation_id = None
    return RetainEvent(
        event="retain.completed",
        bank_id=bank_id,
        operation_id=operation_id,
        document_id=document_id,
        memory_unit_count=count,
    )


# ── config loader for the webhook path ───────────────────────────────────


def webhook_config_loader(bank_id: str) -> ConfigLoader:
    """Build a ConfigLoader that resolves api_url/key from env and ignores cwd.

    The bank_id is fixed from the event payload — there is nothing to discover
    per request. The loader returns None if HINDSIGHT_API_URL is missing so the
    pipeline cleanly skips the event (consistent with the skipped-path behaviour
    elsewhere in the codebase).
    """

    def _loader(cwd: str | None) -> MemorialConfig | None:
        api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
        if not api_url:
            return None
        api_key = os.environ.get("HINDSIGHT_API_KEY")
        return MemorialConfig(
            api_url=api_url.rstrip("/"),
            api_key=api_key,
            bank_id=bank_id,
            bank_source="event",
        )

    return _loader


# ── core handler ─────────────────────────────────────────────────────────


def handle_event(
    raw_body: bytes,
    headers: dict[str, str],
    *,
    secret: bytes,
    fetch_units: Callable[[str, str], list[dict[str, Any]]],
) -> WebhookOutcome:
    """Process one webhook request end-to-end.

    ``fetch_units(bank_id, document_id)`` must return the list of memory_unit
    dicts the server associated with the document. The default handler wires
    this to ``HindsightClient.list_memory_units``; tests can substitute a stub.

    Returns a ``WebhookOutcome`` regardless of validation failure — the HTTP
    layer translates it into the response status (200 always, since the
    request was well-formed enough to log; 401 only if signature failed).
    """
    # Header lookup is case-insensitive per RFC 7230; headers arrives lowercased
    # by the stdlib server already, but be defensive.
    sig = None
    event_name = None
    for k, v in headers.items():
        lk = k.lower()
        if lk == SIGNATURE_HEADER.lower():
            sig = v
        elif lk == EVENT_HEADER.lower():
            event_name = v

    if not verify_signature(raw_body, sig, secret):
        return WebhookOutcome(
            status="ignored",
            error="signature verification failed",
        )

    evt = parse_event(raw_body)
    if evt is None:
        return WebhookOutcome(
            status="ignored",
            error=(
                f"payload not a retain.completed event (X-Hindsight-Event={event_name!r})"
            ),
        )

    if evt.memory_unit_count == 0:
        return WebhookOutcome(
            status="skipped",
            bank_id=evt.bank_id,
            document_id=evt.document_id,
            memory_unit_count=0,
            reason="memory_unit_count == 0; nothing to reconcile",
        )

    units = fetch_units(evt.bank_id, evt.document_id)
    if not units:
        return WebhookOutcome(
            status="skipped",
            bank_id=evt.bank_id,
            document_id=evt.document_id,
            memory_unit_count=evt.memory_unit_count,
            units_skipped=evt.memory_unit_count,
            reason="no memory_units returned by /memories/list",
        )

    loader = webhook_config_loader(evt.bank_id)
    per_unit: list[ReconcileResult] = []
    for unit in units:
        text = unit.get("text") if isinstance(unit, dict) else None
        if not isinstance(text, str) or not text.strip():
            per_unit.append(
                ReconcileResult(
                    status="skipped",
                    bank_id=evt.bank_id,
                    bank_source="event",
                    reason="memory_unit has no text field",
                )
            )
            continue
        result = run_reconcile(text.strip(), load_cfg=loader)
        per_unit.append(result)

    return _aggregate(evt, per_unit)


def _aggregate(evt: RetainEvent, results: list[ReconcileResult]) -> WebhookOutcome:
    """Fold a list of per-unit reconcile results into one WebhookOutcome."""
    worst = "abandoned"
    total_superseded = 0
    total_obs = 0
    units_processed = 0
    units_skipped = 0
    error_message: str | None = None

    for r in results:
        # Promote "worst" status using a fixed ladder.
        if r.status == "ok":
            worst = "ok"
            total_superseded += r.superseded_count
            total_obs += r.observations_cleared
            units_processed += 1
        elif r.status in ("reflect_failed", "error"):
            if worst != "ok":
                worst = r.status
            error_message = error_message or r.error
            units_processed += 1
        elif r.status == "skipped":
            units_skipped += 1
        else:  # "abandoned" / "list_banks_failed" / "dry_run"
            if worst not in ("ok", "reflect_failed", "error"):
                worst = r.status
            units_processed += 1

    return WebhookOutcome(
        status=worst,
        bank_id=evt.bank_id,
        document_id=evt.document_id,
        memory_unit_count=evt.memory_unit_count,
        units_processed=units_processed,
        units_skipped=units_skipped,
        total_superseded=total_superseded,
        total_observations_cleared=total_obs,
        results=[r.to_dict() for r in results],
        error=error_message,
    )


__all__ = [
    "EVENT_HEADER",
    "RetainEvent",
    "SIGNATURE_HEADER",
    "WebhookOutcome",
    "handle_event",
    "parse_event",
    "verify_signature",
    "webhook_config_loader",
]