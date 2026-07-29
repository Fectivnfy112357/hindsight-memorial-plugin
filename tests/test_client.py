"""Unit tests for the HTTP client wrapper."""
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import client as client_mod
from hindsight_memorial.client import HindsightAPIError, HindsightClient


def _http_error(status: int, body: str = "{}"):
    return urllib.error.HTTPError(
        url="http://x", code=status, msg="err", hdrs={}, fp=io.BytesIO(body.encode())
    )


def _resp(body: dict | str = ""):
    m = mock.Mock()
    raw = json.dumps(body) if isinstance(body, dict) else body
    m.read.return_value = raw.encode() if raw else b""
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=False)
    return m


class AuthHeaderTest(unittest.TestCase):
    def test_authorization_set_when_key_present(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            captured["auth"] = req.headers.get("Authorization")
            captured["ct"] = req.headers.get("Content-type")
            return _resp({"ok": True})

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            HindsightClient(base_url="http://t", api_key="secret").reflect("b1", "q")

        self.assertEqual(captured["auth"], "Bearer secret")
        self.assertEqual(captured["ct"], "application/json")

    def test_no_authorization_when_key_absent(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            captured["auth"] = req.headers.get("Authorization")
            return _resp({})

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            HindsightClient(base_url="http://t").reflect("b1", "q")

        self.assertIsNone(captured["auth"])


class ErrorPathTest(unittest.TestCase):
    def test_http_error_raises_typed_exception(self):
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            raise _http_error(503, "service unavailable")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HindsightAPIError) as ctx:
                HindsightClient(base_url="http://t").reflect("b1", "q")
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("service unavailable", ctx.exception.body)

    def test_url_error_is_wrapped_as_typed_exception(self):
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HindsightAPIError) as ctx:
                HindsightClient(base_url="http://t").reflect("b1", "q")
        # status 0 means network-level (not HTTP-level) failure
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("connection refused", ctx.exception.body)


class ResponseParseTest(unittest.TestCase):
    def test_empty_body_returns_empty_dict(self):
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            return _resp("")  # empty body

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = HindsightClient(base_url="http://t").update_memory(
                "b1", "11111111-1111-1111-1111-111111111111", state="invalidated", reason="x"
            )
        self.assertEqual(out, {})


class UrlEncodingTest(unittest.TestCase):
    def test_bank_id_with_special_chars_is_quoted(self):
        seen: list[str] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            seen.append(req.full_url)
            return _resp({})

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            HindsightClient(base_url="http://t").get_memory("bank/one", "abc")
        self.assertIn("/banks/bank%2Fone/memories/abc", seen[0])


if __name__ == "__main__":
    unittest.main()