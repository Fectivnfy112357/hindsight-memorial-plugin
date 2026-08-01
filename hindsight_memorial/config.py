"""Read hindsight-memorial config from ~/.hindsight/claude-code.json (and env overrides).

The official Hindsight Claude Code plugin writes its config to ~/.hindsight/claude-code.json
with fields like `hindsightApiUrl`, `hindsightApiToken`, `bankId`, and a `directoryBankMap`
that maps absolute project paths to bank ids. Memorial reuses this file so users don't have
to configure the same connection twice.

For Hermes, a separate config file at ~/.hindsight/hermes.json is supported as a fallback.
Both files use the same format.

Field names follow the official plugin's convention (camelCase), not snake_case.

Resolution order:
  1. Environment variables (HINDSIGHT_API_URL / HINDSIGHT_API_KEY / HINDSIGHT_BANK_ID) win if set
  2. ~/.hindsight/claude-code.json (or explicit --config path) supplies the rest
  3. ~/.hindsight/hermes.json is tried as a fallback if the primary config is missing
  4. Bank id is resolved as: directoryBankMap[cwd] -> basename(cwd) -> None (give up)

If a bank id cannot be resolved, callers should give up silently — never auto-create.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".hindsight" / "claude-code.json"
DEFAULT_HERMES_CONFIG_PATH = Path.home() / ".hindsight" / "hermes.json"


@dataclass(frozen=True)
class DBConfig:
    """Connection parameters for the persistent reconciler backend.

    ``backend`` is either ``"mysql"`` (production) or ``"sqlite"`` (tests
    and the "ingest-only" local mode). The other fields are interpreted
    by :mod:`hindsight_memorial.db_mysql` for MySQL and ignored for SQLite.
    """

    backend: str = "sqlite"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "memorial"
    password: str = ""
    database: str = "hindsight_memorial"


@dataclass(frozen=True)
class PollerConfig:
    """Runtime knobs for the :class:`~hindsight_memorial.poller.ReconcilerPoller`."""

    enabled: bool = True
    interval_sec: float = 1.0


def load_db_config(env: dict[str, str] | None = None) -> DBConfig:
    """Read DB connection settings from the environment.

    If ``HINDSIGHT_MYSQL_HOST`` is set, the backend is MySQL. Otherwise
    the backend is SQLite (in-memory, useful for tests and the local
    "ingest-only" mode).
    """
    e = env if env is not None else os.environ
    host = e.get("HINDSIGHT_MYSQL_HOST", "").strip()
    if not host:
        return DBConfig(backend="sqlite")
    return DBConfig(
        backend="mysql",
        host=host,
        port=int(e.get("HINDSIGHT_MYSQL_PORT", "3306")),
        user=e.get("HINDSIGHT_MYSQL_USER", "memorial"),
        password=e.get("HINDSIGHT_MYSQL_PASSWORD", ""),
        database=e.get("HINDSIGHT_MYSQL_DATABASE", "hindsight_memorial"),
    )


def load_poller_config(env: dict[str, str] | None = None) -> PollerConfig:
    """Read poller settings from the environment."""
    e = env if env is not None else os.environ
    enabled = e.get("HINDSIGHT_POLLER_ENABLED", "1") != "0"
    try:
        interval = float(e.get("HINDSIGHT_POLLER_INTERVAL_SEC", "1.0"))
    except ValueError:
        interval = 1.0
    return PollerConfig(enabled=enabled, interval_sec=interval)


@dataclass(frozen=True)
class MemorialConfig:
    """Resolved config: API URL/key plus the bank id to operate on.

    `bank_id` may be None — callers must treat that as "skip this retain".
    """

    api_url: str
    api_key: str | None
    bank_id: str | None
    bank_source: str  # "env" | "directoryBankMap" | "basename" | "default" | "none"


def _load_json(path: Path) -> dict[str, Any]:
    """Read JSON from path, returning {} on any failure. Never raises."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, PermissionError):
        return {}


def _normalise_dir(path_str: str) -> str:
    """Normalise an absolute path so directoryBankMap lookups match across platforms.

    Windows paths are case-insensitive in practice; JSON keys may be either case. We lowercase
    and use forward slashes so 'D:\\Programming\\Projects\\Foo' and 'd:/programming/projects/foo'
    resolve identically.
    """
    return os.path.normpath(path_str).replace("\\", "/").rstrip("/").lower()


def resolve_bank_id(
    config: dict[str, Any],
    cwd: str | None,
) -> tuple[str | None, str]:
    """Resolve the bank id from config + cwd, strictly per user's stated order:

      1. directoryBankMap[cwd] (exact match after normalisation)
      2. os.path.basename(cwd) if cwd is provided
      3. None — caller must give up

    config.bankId is intentionally NOT used as a fallback. Users who want a static bank id for
    a project should add an entry to directoryBankMap; otherwise we anchor strictly to the cwd.
    """
    if cwd:
        directory_bank_map = config.get("directoryBankMap")
        if isinstance(directory_bank_map, dict):
            key = _normalise_dir(cwd)
            for map_key, map_val in directory_bank_map.items():
                if isinstance(map_key, str) and isinstance(map_val, str):
                    if _normalise_dir(map_key) == key:
                        return map_val, "directoryBankMap"
        basename = os.path.basename(cwd.rstrip("/\\"))
        if basename:
            return basename, "basename"
    return None, "none"


def load_config(
    config_path: Path | None = None,
    *,
    cwd: str | None = None,
) -> MemorialConfig:
    """Load memorial config, resolving env > file > defaults.

    `cwd` is the current working directory for bank id resolution. If None, we try
    os.getcwd() and fall back to no-bank-id resolution.

    Config file resolution order:
      1. Explicit --config path (if provided)
      2. ~/.hindsight/claude-code.json (Claude Code default)
      3. ~/.hindsight/hermes.json (Hermes fallback)
    """
    cfg = {}
    if config_path is not None:
        # Explicit path: only try that one file. Don't fall back to defaults
        # so callers can test with a deliberately missing file.
        cfg = _load_json(config_path)
    else:
        cfg = _load_json(DEFAULT_CONFIG_PATH)
        if not cfg:
            cfg = _load_json(DEFAULT_HERMES_CONFIG_PATH)

    api_url = (
        os.environ.get("HINDSIGHT_API_URL")
        or cfg.get("hindsightApiUrl")
        or cfg.get("hindsightApiUrlOverride")
        or ""
    )
    api_key = (
        os.environ.get("HINDSIGHT_API_KEY")
        or cfg.get("hindsightApiToken")
        or cfg.get("hindsightApiKey")
    )

    effective_cwd = cwd if cwd is not None else _safe_cwd()
    env_bank = os.environ.get("HINDSIGHT_BANK_ID")
    if env_bank:
        bank_id = env_bank
        bank_source = "env"
    else:
        bank_id, bank_source = resolve_bank_id(cfg, effective_cwd)

    return MemorialConfig(
        api_url=api_url.rstrip("/") if api_url else "",
        api_key=api_key,
        bank_id=bank_id,
        bank_source=bank_source,
    )


def _safe_cwd() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None