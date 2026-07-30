"""Claude Code / Codex adapter package for hindsight-memorial.

The CLI lives in :mod:`plugins.claude_code.cli`. This file just re-exports
``main`` so callers can do ``python -m plugins.claude_code ...`` if they want.
"""
from __future__ import annotations

from .cli import main

__all__ = ["main"]
