"""Unit tests for ~/.hindsight/claude-code.json config loader + bank resolver."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial/scripts")

from lib.config import (
    DEFAULT_CONFIG_PATH,
    MemorialConfig,
    _normalise_dir,
    load_config,
    resolve_bank_id,
)


class TestNormaliseDir(unittest.TestCase):
    def test_windows_path_lowercased(self):
        self.assertEqual(_normalise_dir("D:\\Programming\\Projects\\Foo"), "d:/programming/projects/foo")

    def test_trailing_slash_removed(self):
        self.assertEqual(_normalise_dir("/tmp/foo/"), "/tmp/foo")


class TestResolveBankId(unittest.TestCase):
    def test_directory_bank_map_exact_match(self):
        cfg = {
            "directoryBankMap": {
                "D:\\programming\\projects\\my project\\camofox": "camofox-opencli",
            }
        }
        bid, source = resolve_bank_id(cfg, "D:\\programming\\projects\\my project\\camofox")
        self.assertEqual(bid, "camofox-opencli")
        self.assertEqual(source, "directoryBankMap")

    def test_directory_bank_map_case_insensitive(self):
        cfg = {"directoryBankMap": {"D:\\Programming\\Projects\\Foo": "foo"}}
        bid, source = resolve_bank_id(cfg, "d:\\programming\\projects\\foo")
        self.assertEqual(bid, "foo")
        self.assertEqual(source, "directoryBankMap")

    def test_falls_back_to_basename(self):
        cfg = {"directoryBankMap": {"/somewhere/else": "x"}}
        bid, source = resolve_bank_id(cfg, "/home/user/my-project")
        self.assertEqual(bid, "my-project")
        self.assertEqual(source, "basename")

    def test_no_cwd_returns_none(self):
        cfg = {"directoryBankMap": {}}
        bid, source = resolve_bank_id(cfg, None)
        self.assertIsNone(bid)
        self.assertEqual(source, "none")


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        self._tmppath = Path(self._tmpdir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _cfg_path(self, name: str = "claude-code.json") -> Path:
        return self._tmppath / name

    def _write_cfg(self, tmp: Path, data: dict) -> None:
        tmp.write_text(json.dumps(data), encoding="utf-8")

    def test_loads_from_path(self):
        cfg_path = self._cfg_path()
        self._write_cfg(cfg_path, {
            "hindsightApiUrl": "http://api",
            "hindsightApiToken": "tok",
            "bankId": "default-bank",
            "directoryBankMap": {},
        })
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = load_config(cfg_path, cwd="/tmp/anywhere")
        self.assertEqual(cfg.api_url, "http://api")
        self.assertEqual(cfg.api_key, "tok")
        # No env override, no directoryBankMap match, no cwd basename override path.
        # Bank id falls back to basename of cwd; config.bankId is intentionally NOT used.
        self.assertEqual(cfg.bank_id, "anywhere")
        self.assertEqual(cfg.bank_source, "basename")

    def test_env_overrides_file(self):
        cfg_path = self._cfg_path()
        self._write_cfg(cfg_path, {
            "hindsightApiUrl": "http://from-file",
            "hindsightApiToken": "file-tok",
            "bankId": "file-bank",
        })
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://from-env", "HINDSIGHT_API_KEY": "env-tok", "HINDSIGHT_BANK_ID": "env-bank"},
            clear=True,
        ):
            cfg = load_config(cfg_path, cwd="/tmp/anywhere")
        self.assertEqual(cfg.api_url, "http://from-env")
        self.assertEqual(cfg.api_key, "env-tok")
        self.assertEqual(cfg.bank_id, "env-bank")
        self.assertEqual(cfg.bank_source, "env")

    def test_missing_file_returns_empty(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = load_config(self._cfg_path("nonexistent.json"), cwd="/some/empty/cwd")
        self.assertEqual(cfg.api_url, "")
        # No env var, no directoryBankMap, basename of "/some/empty/cwd" = "cwd"
        # so bank_id resolves to "cwd", source = "basename"
        self.assertEqual(cfg.bank_id, "cwd")
        self.assertEqual(cfg.bank_source, "basename")

    def test_directory_map_wins_over_default_bank_id(self):
        cfg_path = self._cfg_path()
        self._write_cfg(cfg_path, {
            "hindsightApiUrl": "http://api",
            "bankId": "fallback-bank",
            "directoryBankMap": {"/home/user/proj-a": "proj-a-bank"},
        })
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = load_config(cfg_path, cwd="/home/user/proj-a")
        self.assertEqual(cfg.bank_id, "proj-a-bank")
        self.assertEqual(cfg.bank_source, "directoryBankMap")


if __name__ == "__main__":
    unittest.main()