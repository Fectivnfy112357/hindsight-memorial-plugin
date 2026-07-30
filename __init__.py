"""Hermes plugin entry point for hindsight-memorial.

This file is the Hermes entrypoint. It defines ``register(ctx)`` which is
called by ``hermes_plugins`` when the plugin is discovered at
``~/.hermes/plugins/hindsight-memorial/__init__.py``.

Layout:

* ``__init__.py``              ← this file. Hermes entrypoint.
* ``hermes_config.py``         ← Hermes-specific bank-id resolution.
* ``hindsight_memorial/``      ← shared backend (client, config, reconcile, …)
* ``plugins/claude_code/cli.py`` ← CLI entry used by ``scripts/retain_reflect_curate.py``
* ``scripts/``                 ← compatibility shim for Claude Code hooks
* ``tests/``                   ← pytest suite

The two adapters (this file for Hermes, ``plugins/claude_code/cli.py`` for
Claude Code) each only know their own config-discovery rules and call into
the shared ``hindsight_memorial.reconcile.run_reconcile`` pipeline so the
retry policy, query building, and curate all live in exactly one place.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# When this plugin is loaded by Hermes, the surrounding package is
# ``hermes_plugins.hindsight_memorial`` and its ``__path__`` includes this
# directory. ``hindsight_memorial`` (the shared backend) sits as a sibling
# subpackage, and ``hermes_config.py`` as a sibling module. Both ``..`` and
# relative imports work because ``module.__package__`` is set by
# ``hermes_cli.plugins._load_directory_module``.
#
# However, when the same file is imported through other code paths (legacy
# direct import, tools that place this directory on ``sys.path`` for
# inspection), the parent package context is missing. To stay importable from
# any of those paths we resolve the absolute references directly here. The
# cost is one ``sys.path.insert`` per process — harmless.
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from hindsight_memorial.reconcile import run_reconcile  # noqa: E402
import hermes_config  # noqa: E402

build_loader = hermes_config.build_loader
read_config = hermes_config.read_config

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
    log.info(
        "memorial: post_tool_call fired tool=%r args_keys=%s",
        tool_name,
        sorted(args.keys()) if isinstance(args, dict) else type(args).__name__,
    )
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
