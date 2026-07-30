"""Pytest config: ensure the project root is on sys.path so that
``plugins.hermes`` / ``plugins.claude_code`` resolve during test collection.
"""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
