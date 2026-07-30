"""hindsight-memorial: webhook-driven Hindsight memory pollution cleanup."""

from .client import HindsightAPIError, HindsightClient
from .config import MemorialConfig, load_config, resolve_bank_id
from .curate import CurateReport, CurateResult, curate_many, curate_memory
from .reconcile import (
    ConfigLoader,
    ReconcileResult,
    run_reconcile,
)
from .reflect_query import (
    SUPERSEDED_SCHEMA,
    build_query,
    extract_superseded_ids,
)
from .webhook_handlers import (
    EVENT_HEADER,
    RetainEvent,
    SIGNATURE_HEADER,
    WebhookOutcome,
    handle_event,
    parse_event,
    verify_signature,
    webhook_config_loader,
)

__all__ = [
    "HindsightAPIError",
    "HindsightClient",
    "MemorialConfig",
    "load_config",
    "resolve_bank_id",
    "CurateReport",
    "CurateResult",
    "curate_many",
    "curate_memory",
    "ConfigLoader",
    "ReconcileResult",
    "run_reconcile",
    "SUPERSEDED_SCHEMA",
    "build_query",
    "extract_superseded_ids",
    # webhook
    "EVENT_HEADER",
    "RetainEvent",
    "SIGNATURE_HEADER",
    "WebhookOutcome",
    "handle_event",
    "parse_event",
    "verify_signature",
    "webhook_config_loader",
]