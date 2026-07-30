"""Claude Code / Codex CLI entry point: retain → reflect → curate.

This module is intentionally thin: it only knows how to pull the new fact
and ``cwd`` out of the adapter-specific hook payload (JSON via stdin or
command-line flags), and then hands off to
``hindsight_memorial.reconcile.run_reconcile``. The reconcile logic — retry
policy, query building, curate, error classification — lives in the shared
library so the Hermes plugin and this CLI share a single source of truth.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from hindsight_memorial.config import MemorialConfig, load_config
from hindsight_memorial.reconcile import run_reconcile

log = logging.getLogger("hindsight_memorial.cli")


# ── stdin payload parsing (Claude Code / Codex specific) ────────────────


def _read_hook_payload() -> dict[str, Any]:
    """Read a JSON hook payload from stdin, returning ``{}`` on absence or parse failure."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _extract_new_fact(args: argparse.Namespace, payload: dict[str, Any]) -> str | None:
    """Resolve the new fact text from CLI arg, env, or stdin payload — in that order."""
    if args.new_fact:
        return args.new_fact
    env = os.environ.get("HINDSIGHT_MEMORIAL_NEW_FACT")
    if env:
        return env
    # Heuristics: try a few common payload shapes from different clients.
    for key in ("new_fact", "content", "text", "memory_text", "fact"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    # Claude Code PostToolUse nests the tool args under ``tool_input``.
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("content", "text", "fact", "memory_text", "new_fact"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                return v
    # Generic fallback: try ``memory`` wrapper (some clients nest deeper).
    nested = payload.get("memory")
    if isinstance(nested, dict):
        for key in ("content", "text", "fact"):
            v = nested.get(key)
            if isinstance(v, str) and v.strip():
                return v
    # Codex-like shape: tool_input.command = "hindsight retain '...'"
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        m = re.search(r"hindsight\s+retain\s+['\"](.+?)['\"]", tool_input["command"])
        if m:
            return m.group(1)
    return None


def _extract_cwd(payload: dict[str, Any]) -> str | None:
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


# ── adapter: how CLI surfaces its MemorialConfig ─────────────────────────


def build_cli_loader(args: argparse.Namespace):
    """Return a per-call ConfigLoader that respects CLI ``--bank-id`` and ``--config``."""
    cfg_path = Path(args.config) if args.config else None

    def _loader(cwd: str | None) -> MemorialConfig | None:
        effective_cwd = args.cwd or cwd
        cfg = load_config(cfg_path, cwd=effective_cwd)
        if args.bank_id:
            # Caller pinned a bank id, take precedence over file/env.
            cfg = replace(cfg, bank_id=args.bank_id, bank_source="cli_flag")
        return cfg

    return _loader


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by Claude Code / Codex PostToolUse hooks."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(
        prog="hindsight-memorial-retain-reflect",
        description=(
            "After a retain, reflect on the bank to detect superseded facts, "
            "then soft-delete them and clear their derived observations. "
            "Never raises; always exits 0."
        ),
    )
    p.add_argument(
        "--new-fact",
        help="The newly retained fact text (overrides stdin/env).",
    )
    p.add_argument(
        "--cwd",
        help="Working directory used for bank id resolution (overrides stdin payload cwd).",
    )
    p.add_argument(
        "--bank-id",
        help="Explicit bank id (overrides HINDSIGHT_BANK_ID and ~/.hindsight/claude-code.json).",
    )
    p.add_argument(
        "--config",
        help="Path to a JSON config file (defaults to ~/.hindsight/claude-code.json).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run reflect but skip the PATCH/DELETE calls; print what would have been curated.",
    )
    args = p.parse_args(argv)

    payload = _read_hook_payload()
    new_fact = _extract_new_fact(args, payload)
    cwd = args.cwd or _extract_cwd(payload)
    loader = build_cli_loader(args)

    try:
        outcome = run_reconcile(
            new_fact or "",
            load_cfg=loader,
            cwd=cwd,
            dry_run=args.dry_run,
        )
        result = outcome.to_dict()
    except Exception as e:  # last-resort: hooks must never bubble
        log.exception("unexpected error")
        result = {"status": "error", "error": repr(e)}

    print(json.dumps(result, indent=2))
    return 0


__all__ = [
    "main",
    "build_cli_loader",
    "_extract_new_fact",
    "_read_hook_payload",
]


if __name__ == "__main__":
    sys.exit(main())
