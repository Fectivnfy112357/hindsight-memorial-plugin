"""Regression tests for the Hermes plugin entry point."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_hermes_plugin():
    module_name = "hindsight_memorial_hermes_plugin_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TestHermesConfig(unittest.TestCase):
    def _write_config(self, home: Path, data: dict) -> None:
        config_dir = home / "hindsight"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_static_hermes_bank_id_wins_when_directory_map_has_no_match(self):
        plugin = _load_hermes_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_config(
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
                cfg = plugin._load_hermes_config(
                    cwd="D:/programming/projects/hindsight-memorial"
                )

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bank_id, "hermes-agent")
        self.assertEqual(cfg.bank_source, "hermes_config")

    def test_exact_directory_map_still_overrides_static_hermes_bank_id(self):
        plugin = _load_hermes_plugin()
        cwd = "D:/programming/projects/hindsight-memorial"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_config(
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
                cfg = plugin._load_hermes_config(cwd=cwd)

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.bank_id, "project-specific-bank")
        self.assertEqual(cfg.bank_source, "directoryBankMap")


if __name__ == "__main__":
    unittest.main()
