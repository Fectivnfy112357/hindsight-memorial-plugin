"""Hermes plugin entry point for hindsight-memorial.

This package is the only Hermes-specific surface in the project; everything
else lives in ``hindsight_memorial/``. This file does three things and no more:

  1. Detect whether a tool call is a Hindsight retain.
  2. Pull the new fact out of the call's args.
  3. Hand it off to ``hindsight_memorial.reconcile.run_reconcile`` with a
     Hermes-shaped config loader from ``plugins.hermes.config``.

Bank-id resolution, retry policy, query building, and curate all live in the
shared library.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from hindsight_memorial.reconcile import run_reconcile

from .config import build_loader, read_config

log = logging.getLogger("hindsight_memorial.hermes")

# Tool names that should trigger the reconcile pipeline. Hermes' MemoryManager
# tool is registered as `hindsight_retain`; we also accept the Claude Code MCP
# alias so the same plugin can be ported trivially.
_RETAIN_PATTERNS = (
    "hindsight_retain",
    "agent_knowledge_ingest",
)


def _is_retain_tool(tool_name: str) -> bool:
    return any(pattern in tool_name for pattern in _RETAIN_PATTERNS)


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


def _on_post_tool_call(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    task_id: str,
    duration_ms: int,
    **kwargs: Any,
) -> None:
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
    loader = build_loader(read_config())

    outcome = run_reconcile(new_fact, load_cfg=loader, cwd=cwd)

    if outcome.status == "ok" and outcome.superseded_count > 0:
        log.info(
            "memorial: cleaned %d/%d facts after %d attempt(s) (%.0fs elapsed, "
            "bank=%s, source=%s)",
            outcome.superseded_count,
            outcome.superseded_count,
            outcome.attempts,
            outcome.elapsed_seconds,
            outcome.bank_id,
            outcome.bank_source,
        )
    elif outcome.status == "abandoned":
        log.debug(
            "memorial: abandoned after %d attempt(s), %.0fs elapsed, "
            "reason=%s, fact=%r",
            outcome.attempts,
            outcome.elapsed_seconds,
            outcome.reason,
            new_fact[:120],
        )
    elif outcome.status in ("reflect_failed", "list_banks_failed", "error"):
        log.warning(
            "memorial: pipeline failed status=%s error=%s bank=%s",
            outcome.status,
            outcome.error,
            outcome.bank_id,
        )


def register(ctx: Any) -> None:
    """Called by Hermes at plugin load time."""
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    log.info("hindsight-memorial plugin registered (post_tool_call hook)")


__all__ = ["register"]
