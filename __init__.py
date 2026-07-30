"""Hermes plugin for hindsight-memorial — post-retain pollution cleanup.

Registers a post_tool_call hook that fires after every hindsight_retain
invocation. The hook runs a reflect LLM query against the Hindsight bank
to detect facts superseded by the newly-retained one, then soft-deletes
those stale facts and clears their derived observations.

Config is read from Hermes-specific paths (not Claude Code):
  - API URL / Key  → $HERMES_HOME/.env (loaded by Hermes into os.environ)
  - Bank ID / Map  → $HERMES_HOME/hindsight/config.json

All failures are non-fatal — the hook never blocks the agent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── path setup ──────────────────────────────────────────────────────────
# The shared library is at hindsight_memorial/ in the same directory as this
# file. Hermes adds the plugin directory to sys.path automatically.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hindsight_memorial.client import HindsightAPIError, HindsightClient
from hindsight_memorial.config import MemorialConfig, resolve_bank_id
from hindsight_memorial.curate import curate_many
from hindsight_memorial.reflect_query import (
    SUPERSEDED_SCHEMA,
    build_query,
    extract_superseded_ids,
)

log = logging.getLogger("hindsight_memorial.hermes")

# ── tool name matching ──────────────────────────────────────────────────
_RETAIN_PATTERNS = [
    "hindsight_retain",
    "agent_knowledge_ingest",  # Claude Code MCP plugin name
]


def _is_retain_tool(tool_name: str) -> bool:
    """Return True if *tool_name* matches a known Hindsight retain pattern."""
    for pattern in _RETAIN_PATTERNS:
        if pattern in tool_name:
            return True
    return False


# ── fact extraction ─────────────────────────────────────────────────────

def _extract_new_fact(args: dict[str, Any]) -> str | None:
    """Pull the fact text out of the Hindsight retain tool arguments."""
    for key in ("content", "text", "fact", "memory_text", "new_fact", "memory"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
    memory = args.get("memory")
    if isinstance(memory, dict):
        for key in ("content", "text", "fact"):
            v = memory.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return None


# ── Hermes config loading ───────────────────────────────────────────────

def _hermes_home() -> Path:
    """Return the Hermes home directory, respecting $HERMES_HOME."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _load_hermes_config(cwd: str | None = None) -> MemorialConfig | None:
    """Load memorial config from Hermes-specific paths.

    API URL and key come from environment variables (set by Hermes from
    $HERMES_HOME/.env). Bank id comes from $HERMES_HOME/hindsight/config.json.
    """
    api_url = os.environ.get("HINDSIGHT_API_URL", "").strip()
    if not api_url:
        log.debug("memorial: HINDSIGHT_API_URL not set — skipping")
        return None

    api_key = os.environ.get("HINDSIGHT_API_KEY")

    # Read bank config from Hermes' own Hindsight config file
    config_path = _hermes_home() / "hindsight" / "config.json"
    cfg: dict[str, Any] = {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        log.debug("memorial: no config at %s", config_path)

    # Bank resolution must mirror Hermes' Hindsight provider. Its config has a
    # session-wide ``bank_id``; only an explicit directoryBankMap match should
    # override it. Falling back to basename(cwd) here would make retain write to
    # one bank while memorial reflects/curates a different one.
    env_bank = os.environ.get("HINDSIGHT_BANK_ID")
    if env_bank:
        bank_id: str | None = env_bank
        bank_source = "env"
    else:
        mapped_bank: str | None = None
        if cwd:
            resolved_bank, resolved_source = resolve_bank_id(cfg, cwd)
            if resolved_source == "directoryBankMap":
                mapped_bank = resolved_bank
        if mapped_bank:
            bank_id = mapped_bank
            bank_source = "directoryBankMap"
        else:
            bank_id = cfg.get("bank_id")
            bank_source = "hermes_config" if bank_id else "none"

    return MemorialConfig(
        api_url=api_url.rstrip("/"),
        api_key=api_key,
        bank_id=bank_id,
        bank_source=bank_source,
    )


# ── hook callback ───────────────────────────────────────────────────────

def _on_post_tool_call(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    task_id: str,
    duration_ms: int,
    **kwargs: Any,
) -> None:
    """Fires after every tool call. If it was a Hindsight retain, run cleanup."""
    if not _is_retain_tool(tool_name):
        return

    new_fact = _extract_new_fact(args)
    if not new_fact:
        log.debug(
            "memorial: retain tool '%s' fired but no fact text found in args keys=%s",
            tool_name,
            list(args.keys()),
        )
        return

    cwd = args.get("cwd") or os.getcwd()

    cfg = _load_hermes_config(cwd=cwd)
    if cfg is None:
        return

    if not cfg.bank_id:
        log.debug("memorial: no bank_id resolved (source=%s)", cfg.bank_source)
        return

    try:
        client = HindsightClient.from_memorial_config(cfg)
    except Exception as e:
        log.warning("memorial: client init failed: %s", e)
        return

    # Verify the bank exists on the server — silently skip if not.
    try:
        bank_ids = client.list_banks()
    except HindsightAPIError as e:
        log.warning("memorial: list_banks failed: %s", e)
        return
    if cfg.bank_id not in bank_ids:
        log.debug(
            "memorial: bank '%s' not found on server (available: %d)",
            cfg.bank_id,
            len(bank_ids),
        )
        return

    query = build_query(new_fact, bank_id=cfg.bank_id)

    try:
        reflect_resp = client.reflect(
            cfg.bank_id,
            query,
            structured_output=SUPERSEDED_SCHEMA,
            include_based_on=False,
        )
    except HindsightAPIError as e:
        log.warning("memorial: reflect failed: %s", e)
        return

    superseded = extract_superseded_ids(reflect_resp)
    if not superseded:
        log.debug("memorial: no superseded facts detected")
        return

    reason = f"Superseded by newly retained fact: {new_fact[:200]}"
    try:
        report = curate_many(client, cfg.bank_id, superseded, reason=reason)
    except Exception as e:
        log.warning("memorial: curate failed: %s", e)
        return

    log.info(
        "memorial: cleaned %d/%d facts (bank=%s, source=%s)",
        report.invalidated_count,
        len(superseded),
        cfg.bank_id,
        cfg.bank_source,
    )


# ── plugin entry point ──────────────────────────────────────────────────

def register(ctx: Any) -> None:
    """Called by Hermes at plugin load time."""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    log.info("hindsight-memorial plugin registered (post_tool_call hook)")