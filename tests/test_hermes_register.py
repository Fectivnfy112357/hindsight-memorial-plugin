"""Regression tests for the Hermes plugin adapter.

These tests cover (a) bank-id resolution rules under ``HERMES_HOME`` and
(b) that :func:`register` (the project root's plugin entry) registers a
``post_tool_call`` hook pointing at the right callback.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hindsight_memorial.config import MemorialConfig

# The Hermes plugin entrypoint lives at the project root so that
# `~/.hermes/plugins/hindsight-memorial/__init__.py` resolves as a
# standalone spec when Hermes loads plugins.
import hermes_config
from hermes_config import (
    build_loader,
    load_hermes_config,
    read_config,
)

import importlib.util

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "hermes_plugin_entry", _PROJECT_ROOT / "__init__.py"
)
_hermes_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hermes_plugin)
register = _hermes_plugin.register


def _write_hermes_config(home: Path, data: dict) -> None:
    config_dir = home / "hindsight"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


class TestHermesBankResolution(unittest.TestCase):
    def test_static_hermes_bank_id_wins_when_directory_map_has_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_hermes_config(
                home,
                {
                    "bank_id": "hermes-agent",
                    "directoryBankMap": {
                        "D:/some/other/project": "other-bank",
                    },
                },
            )
            env = {
                "HERMES_HOME": str(home),
                "HINDSIGHT_API_URL": "http://hindsight.test",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                cfg: MemorialConfig | None = load_hermes_config(
                    cwd="D:/programming/projects/hindsight-memorial"
                )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bank_id, "hermes-agent")
        self.assertEqual(cfg.bank_source, "hermes_config")

    def test_exact_directory_map_still_overrides_static_hermes_bank_id(self):
        cwd = "D:/programming/projects/hindsight-memorial"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_hermes_config(
                home,
                {
                    "bank_id": "hermes-agent",
                    "directoryBankMap": {cwd: "project-specific-bank"},
                },
            )
            env = {
                "HERMES_HOME": str(home),
                "HINDSIGHT_API_URL": "http://hindsight.test",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_hermes_config(cwd=cwd)

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bank_id, "project-specific-bank")
        self.assertEqual(cfg.bank_source, "directoryBankMap")

    def test_env_bank_id_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_hermes_config(
                home,
                {
                    "bank_id": "hermes-agent",
                    "directoryBankMap": {
                        "D:/programming/projects/hindsight-memorial": "mapped-bank",
                    },
                },
            )
            env = {
                "HERMES_HOME": str(home),
                "HINDSIGHT_API_URL": "http://hindsight.test",
                "HINDSIGHT_BANK_ID": "env-bank",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                cfg = load_hermes_config(
                    cwd="D:/programming/projects/hindsight-memorial"
                )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bank_id, "env-bank")
        self.assertEqual(cfg.bank_source, "env")


class TestHermesRegister(unittest.TestCase):
    def test_register_wires_post_tool_call_hook(self):
        ctx = mock.Mock()
        register(ctx)
        ctx.register_hook.assert_called_once()
        event, callback = ctx.register_hook.call_args[0]
        self.assertEqual(event, "post_tool_call")
        self.assertTrue(callable(callback))

    def test_register_logs_expected_message(self):
        ctx = mock.Mock()
        with self.assertLogs("hindsight_memorial.hermes", level="INFO") as cm:
            register(ctx)
        self.assertTrue(
            any(
                "post_tool_call hook" in line
                for line in cm.output
            ),
            msg=f"unexpected log lines: {cm.output!r}",
        )


class TestReadHermesCfg(unittest.TestCase):
    def test_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}, clear=True):
                self.assertEqual(read_config(), {})

    def test_returns_empty_when_file_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "hindsight").mkdir()
            (home / "hindsight" / "config.json").write_text(
                "not-json-{", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=True):
                self.assertEqual(read_config(), {})


if __name__ == "__main__":
    unittest.main()
