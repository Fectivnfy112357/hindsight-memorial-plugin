#!/usr/bin/env python3
"""Compatibility shim for ``scripts/retain_reflect_curate.py``.

The actual implementation lives in :mod:`plugins.claude_code.cli`. This shim
exists so existing PostToolUse hook commands (which call this exact path)
keep working without any configuration changes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``plugins.claude_code`` resolves.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.claude_code.cli import main  # noqa: E402

if __name__ == "__main__":
    # Hook payloads arrive via stdin; sys.stdin.isatty() will be False. We pass
    # through argv unchanged so existing flags (--new-fact, --dry-run, …) work.
    sys.exit(main())
