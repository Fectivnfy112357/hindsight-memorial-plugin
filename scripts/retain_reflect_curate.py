"""retain_reflect_curate: the core entry point.

Flow:
    1. read the freshly-retained fact from stdin (JSON hook payload) or --new-fact arg
    2. call Hindsight reflect() with a structured supersession query
    3. parse superseded_fact_ids out of the response
    4. curate each (PATCH invalidate + DELETE observations)
    5. emit a JSON summary on stdout

Designed to be called by a hook after a retain tool completes. Exit code is always 0 — memorial
must never block the calling LLM because of a cleanup hiccup. All errors are reported in the JSON.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# The shared library lives one level up from scripts/, at the project root.
# Add the project root to sys.path so both the Claude Code hook and Hermes
# plugin can import from the same hindsight_memorial package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hindsight_memorial.client import HindsightAPIError, HindsightClient
from hindsight_memorial.config import load_config
from hindsight_memorial.curate import curate_many
from hindsight_memorial.reflect_query import SUPERSEDED_SCHEMA, build_query, extract_superseded_ids

log = logging.getLogger("hindsight_memorial")


def _read_hook_payload() -> dict[str, Any]:
    """Read a hook payload from stdin if available.

    Each client sends a different payload shape, so we accept anything JSON-shaped and let the
    caller pass --new-fact to override.
    """
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
    # Claude Code PostToolUse nests the tool args under "tool_input".
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("content", "text", "fact", "memory_text", "new_fact"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                return v
    # Generic fallback: try "memory" wrapper (some clients nest deeper).
    nested = payload.get("memory")
    if isinstance(nested, dict):
        for key in ("content", "text", "fact"):
            v = nested.get(key)
            if isinstance(v, str) and v.strip():
                return v
    # Codex-like shape: tool_input.command = "hindsight retain '...'"
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        cmd = tool_input["command"]
        # Best-effort: extract single-quoted argument
        import re
        m = re.search(r"hindsight\s+retain\s+['\"](.+?)['\"]", cmd)
        if m:
            return m.group(1)
    return None


def _extract_cwd(payload: dict[str, Any]) -> str | None:
    """Pull the working directory out of a hook payload, if present.

    Claude Code's PostToolUse payload includes a top-level `cwd`. Falls back to None if the
    caller didn't pass --cwd.
    """
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def run(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_hook_payload()
    new_fact = _extract_new_fact(args, payload)
    if not new_fact:
        return {
            "status": "skipped",
            "reason": "no new_fact supplied (use --new-fact, HINDSIGHT_MEMORIAL_NEW_FACT, or stdin)",
        }

    # Config: env overrides file. cwd from --cwd flag > stdin payload > os.getcwd().
    effective_cwd = args.cwd or _extract_cwd(payload)
    cfg_path = Path(args.config) if args.config else None
    cfg = load_config(cfg_path, cwd=effective_cwd)

    # Resolve bank id: explicit --bank-id wins over everything, else use cfg.
    bank_id = args.bank_id or cfg.bank_id
    if not bank_id:
        return {
            "status": "skipped",
            "reason": (
                "no bank_id resolved "
                f"(config bank_source={cfg.bank_source}; "
                "set --bank-id or HINDSIGHT_BANK_ID or add bankId/directoryBankMap to "
                "~/.hindsight/claude-code.json or ~/.hindsight/hermes.json)"
            ),
        }
    if not cfg.api_url:
        return {
            "status": "skipped",
            "reason": (
                "no api_url configured "
                "(set HINDSIGHT_API_URL or add hindsightApiUrl to "
                "~/.hindsight/claude-code.json or ~/.hindsight/hermes.json)"
            ),
        }

    client = HindsightClient.from_memorial_config(cfg)

    # Verify bank exists on the server. Per user requirement: silently give up if missing.
    try:
        bank_ids = client.list_banks()
    except HindsightAPIError as e:
        log.warning("list_banks failed: %s", e)
        return {"status": "list_banks_failed", "error": str(e)}
    if bank_id not in bank_ids:
        return {
            "status": "skipped",
            "reason": f"bank '{bank_id}' not present on server (resolved via {cfg.bank_source})",
            "available_bank_count": len(bank_ids),
        }

    query = build_query(new_fact, bank_id=bank_id)

    if args.dry_run:
        return {
            "status": "dry_run",
            "bank_id": bank_id,
            "bank_source": cfg.bank_source,
            "new_fact_preview": new_fact[:120],
            "query_preview": query[:200],
        }

    try:
        reflect_resp = client.reflect(
            bank_id,
            query,
            structured_output=SUPERSEDED_SCHEMA,
            include_based_on=False,
        )
    except HindsightAPIError as e:
        # Reflect failure is non-fatal: log it, let the retain stand, and let a future retain retry.
        log.warning("reflect failed: %s", e)
        return {"status": "reflect_failed", "error": str(e)}

    superseded = extract_superseded_ids(reflect_resp)
    if not superseded:
        return {"status": "ok", "superseded_count": 0, "results": []}

    reason = f"Superseded by newly retained fact: {new_fact[:200]}"
    report = curate_many(client, bank_id, superseded, reason=reason)
    return {
        "status": "ok",
        "superseded_count": len(superseded),
        "bank_source": cfg.bank_source,
        **report.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        prog="hindsight-memorial-retain-reflect",
        description=(
            "After a retain, reflect on the bank to detect superseded facts, then soft-delete them "
            "and clear their derived observations. Never raises; always exits 0."
        ),
    )
    p.add_argument("--new-fact", help="The newly retained fact text (overrides stdin/env).")
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

    try:
        result = run(args)
    except Exception as e:  # last-resort: hooks must never bubble
        log.exception("unexpected error")
        result = {"status": "error", "error": repr(e)}

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())