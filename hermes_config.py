"""Hermes-specific config discovery.

Locates Hermes' own hindsight config at ``$HERMES_HOME/hindsight/config.json``
and exposes a ``ConfigLoader`` callable that the shared
``hindsight_memorial.reconcile.run_reconcile`` pipeline can use to resolve
bank ids.

Bank-id resolution order (Hermes-style, distinct from Claude Code):

  1. ``HINDSIGHT_BANK_ID`` env var
  2. ``directoryBankMap[cwd]`` (only an explicit match counts;
     ``basename(cwd)`` is *not* a fallback because Hermes agents
     routinely work across projects in the same session)
  3. ``bank_id`` field at the top of ``config.json``
  4. None  — caller gives up cleanly

Nothing in this module makes HTTP calls.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from hindsight_memorial.config import MemorialConfig, resolve_bank_id

log = logging.getLogger("hindsight_memorial.hermes.config")

_CONFIG_REL_PATH = Path("hindsight") / "config.json"


def hermes_home() -> Path:
    """Return the Hermes home directory, respecting ``$HERMES_HOME``."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def read_config() -> dict[str, Any]:
    """Read ``$HERMES_HOME/hindsight/config.json`` as a dict, or ``{}`` on any failure."""
    path = hermes_home() / _CONFIG_REL_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def build_loader(cfg_raw: dict[str, Any]):
    """Build a per-call ``ConfigLoader`` mirroring Hermes' bank-id rules."""

    def _loader(cwd: str | None) -> MemorialConfig | None:
        api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
        if not api_url:
            log.debug("memorial: HINDSIGHT_API_URL not set — skipping")
            return None
        api_key = os.environ.get("HINDSIGHT_API_KEY")

        env_bank = os.environ.get("HINDSIGHT_BANK_ID")
        if env_bank:
            bank_id: str | None = env_bank
            bank_source = "env"
        else:
            mapped_bank: str | None = None
            if cwd:
                resolved, source = resolve_bank_id(cfg_raw, cwd)
                if source == "directoryBankMap":
                    mapped_bank = resolved
            if mapped_bank:
                bank_id = mapped_bank
                bank_source = "directoryBankMap"
            else:
                bank_id = cfg_raw.get("bank_id")
                bank_source = "hermes_config" if bank_id else "none"

        return MemorialConfig(
            api_url=api_url.rstrip("/"),
            api_key=api_key,
            bank_id=bank_id,
            bank_source=bank_source,
        )

    return _loader


def load_hermes_config(cwd: str | None = None) -> MemorialConfig | None:
    """Convenience wrapper used by tests and one-shot callers."""
    return build_loader(read_config())(cwd)


__all__ = [
    "hermes_home",
    "read_config",
    "build_loader",
    "load_hermes_config",
]
