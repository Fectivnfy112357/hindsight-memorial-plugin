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
import logging.handlers
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config import MemorialConfig
from .reconcile import ConfigLoader, ReconcileResult, run_reconcile

log = logging.getLogger("hindsight_memorial.webhook_handlers")

# Header literal from hindsight-api-slim/hindsight_api/engine/memory_engine.py:2322-2328
SIGNATURE_HEADER = "X-Hindsight-Signature"
EVENT_HEADER = "X-Hindsight-Event"

# ── logging setup ────────────────────────────────────────────────────────


def configure_logging(
    log_file: str | None = None,
    level: str | None = None,
) -> None:
    """Configure the ``hindsight_memorial`` logger tree.

    Honours env vars when args are not given:

      HINDSIGHT_MEMORIAL_LOG_FILE  path to a rotating file; if unset, stderr only
      HINDSIGHT_MEMORIAL_LOG_LEVEL  one of DEBUG/INFO/WARNING/ERROR (default INFO)

    RotatingFileHandler: 10 MB per file, 5 backups. Idempotent — safe to call
    multiple times (e.g. from tests).
    """
    resolved_level = (level or os.environ.get("HINDSIGHT_MEMORIAL_LOG_LEVEL") or "INFO").upper()
    resolved_file = log_file or os.environ.get("HINDSIGHT_MEMORIAL_LOG_FILE") or None

    root = logging.getLogger("hindsight_memorial")
    root.setLevel(getattr(logging, resolved_level, logging.INFO))
    # Wipe handlers added by previous configure_logging calls.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if resolved_file:
        os.makedirs(os.path.dirname(resolved_file), exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            resolved_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
        root.info("logging to file: %s", resolved_file)


# ── payload model ────────────────────────────────────────────────────────


@dataclass
class RetainEvent:
    """Parsed retain.completed webhook event.

    ``document_id`` may be None: some Hindsight retain paths emit
    ``data={}`` because the client did not supply a document_id and the
    server's auto-generated id is not propagated into the webhook payload.
    Callers can fall back to ``list_recent_units`` in that case.
    """

    event: str
    bank_id: str
    operation_id: str | None
    document_id: str | None
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

    document_id may be missing/empty (Hindsight's auto-generated id is sometimes
    not propagated into the webhook payload — see
    hindsight_api/engine/retain/orchestrator.py:757). In that case the returned
    ``RetainEvent.document_id`` is None and the caller is responsible for
    recovery (e.g. via ``list_recent_units``).
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
    raw_doc_id = data.get("document_id")
    document_id: str | None = raw_doc_id if isinstance(raw_doc_id, str) and raw_doc_id else None
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


def _safe_json(raw_body: bytes) -> Any:
    """Parse JSON body for diagnostic logging. Never raises — returns None on any error."""
    try:
        return json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return None


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
    fetch_recent_doc: Callable[[str], str | None] | None = None,
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

    log.info(
        "webhook received: bytes=%d event_header=%r sig_present=%s",
        len(raw_body),
        event_name,
        bool(sig),
    )
    # TEMP DEBUG: dump the raw body so we can see why parse_event rejects it.
    log.info("webhook raw body: %r", raw_body.decode("utf-8", errors="replace"))

    if not verify_signature(raw_body, sig, secret):
        log.warning("signature verification failed (event=%r)", event_name)
        return WebhookOutcome(
            status="ignored",
            error="signature verification failed",
        )

    evt = parse_event(raw_body)
    if evt is None:
        # parse_event returns None for any of: malformed JSON, body.event !=
        # 'retain.completed', missing/non-string bank_id, missing/non-dict data,
        # or missing/empty data.document_id. The body dump above shows which
        # case actually fired; the previous message "not a retain.completed
        # event" was misleading (we now know data={} is the common mode).
        body_obj = _safe_json(raw_body)
        body_event = body_obj.get("event") if isinstance(body_obj, dict) else None
        data_obj = body_obj.get("data") if isinstance(body_obj, dict) else None
        has_doc_id = isinstance(data_obj, dict) and bool(data_obj.get("document_id"))
        log.warning(
            "payload rejected by parse_event "
            "(event_header=%r body_event=%r data=%r has_document_id=%s)",
            event_name,
            body_event,
            data_obj,
            has_doc_id,
        )
        return WebhookOutcome(
            status="ignored",
            error=(
                f"payload rejected by parse_event (X-Hindsight-Event={event_name!r})"
            ),
        )

    log.info(
        "event parsed: bank=%s document=%s memory_unit_count=%d operation=%s",
        evt.bank_id,
        evt.document_id,
        evt.memory_unit_count,
        evt.operation_id,
    )

    # Fallback: Hindsight's outbox sometimes sends ``data={}`` (no document_id)
    # because the client did not supply one and the server's auto-generated id
    # is not written back into the content dict that flows into the webhook
    # (see hindsight_api/engine/retain/orchestrator.py:757). When that happens
    # we cannot target /memories/list by document_id, so we ask for the most
    # recently mentioned unit in the bank and read its document_id. This is
    # best-effort: under concurrent retains on the same bank the "most recent"
    # unit may not belong to this retain, in which case reconcile will clean
    # the wrong document — but the race is rare and self-correcting on the
    # next retain.
    if not evt.document_id and fetch_recent_doc is not None:
        recovered = fetch_recent_doc(evt.bank_id)
        if recovered:
            log.info(
                "fallback: recovered document_id=%r from most recent unit "
                "(webhook data={} for bank=%s)",
                recovered,
                evt.bank_id,
            )
            evt = RetainEvent(
                event=evt.event,
                bank_id=evt.bank_id,
                operation_id=evt.operation_id,
                document_id=recovered,
                memory_unit_count=evt.memory_unit_count,
            )
        else:
            log.warning(
                "fallback: no recent units in bank=%s; cannot reconcile",
                evt.bank_id,
            )

    # NOTE: ``memory_unit_count`` is the server's pre-extraction hint, not an
    # authoritative truth. We've observed cases where the count is 0 but
    # /memories/list still returns units (the UI shows them), and cases where
    # it's >0 but no units are retrievable. Always do the actual query —
    # the list endpoint is the single source of truth.
    units = fetch_units(evt.bank_id, evt.document_id)
    log.info(
        "fetched units: bank=%s document=%s units=%d (server_hint=%d)",
        evt.bank_id,
        evt.document_id,
        len(units),

        evt.memory_unit_count,
    )
    if not units:
        log.info(
            "skipped: /memories/list returned 0 units bank=%s document=%s",
            evt.bank_id,
            evt.document_id,
        )
        return WebhookOutcome(
            status="skipped",
            bank_id=evt.bank_id,
            document_id=evt.document_id,
            memory_unit_count=evt.memory_unit_count,
            reason="no memory_units returned by /memories/list (count hint from server may be unreliable)",
        )

    loader = webhook_config_loader(evt.bank_id)
    per_unit: list[ReconcileResult] = []
    for idx, unit in enumerate(units, start=1):
        text = unit.get("text") if isinstance(unit, dict) else None
        unit_id = unit.get("id") if isinstance(unit, dict) else None
        if not isinstance(text, str) or not text.strip():
            log.info(
                "unit %d/%d skipped (no text) bank=%s document=%s unit_id=%s",
                idx,
                len(units),
                evt.bank_id,
                evt.document_id,
                unit_id,
            )
            per_unit.append(
                ReconcileResult(
                    status="skipped",
                    bank_id=evt.bank_id,
                    bank_source="event",
                    reason="memory_unit has no text field",
                )
            )
            continue
        log.info(
            "unit %d/%d reconciling bank=%s document=%s unit_id=%s text_preview=%r",
            idx,
            len(units),
            evt.bank_id,
            evt.document_id,
            unit_id,
            text.strip()[:120],
        )
        result = run_reconcile(text.strip(), load_cfg=loader)
        log.info(
            "unit %d/%d result=%s superseded=%d observations_cleared=%d error=%s",
            idx,
            len(units),
            result.status,
            result.superseded_count,
            result.observations_cleared,
            result.error,
        )
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