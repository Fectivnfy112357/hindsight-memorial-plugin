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

    def test_timeout_error_is_wrapped_as_typed_exception(self):
        """Python 3.10+ raises a built-in ``TimeoutError`` (an OSError
        subclass) for ``urlopen(timeout=...)``. The 2026-07-31 incident
        log showed this path was unhandled, leaking a raw
        ``TimeoutError`` that the reconciler had to catch ad-hoc.
        This test pins the contract: every urlopen failure must surface
        as a ``HindsightAPIError``, regardless of the underlying
        exception class."""

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            raise TimeoutError("timed out")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HindsightAPIError) as ctx:
                HindsightClient(base_url="http://t").reflect("b1", "q")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("timed out", ctx.exception.body)
        # The cause chain is preserved for debugging.
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    def test_connection_reset_error_is_wrapped_as_typed_exception(self):
        """ConnectionResetError is an OSError subclass but not an
        URLError — it used to slip through the old two-exception
        catch. Pin the contract here too."""

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            raise ConnectionResetError("connection reset by peer")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HindsightAPIError) as ctx:
                HindsightClient(base_url="http://t").reflect("b1", "q")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("connection reset", ctx.exception.body)


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


class ListMemoryUnitsTest(unittest.TestCase):
    """Hindsight /memories/list returns items under the ``items`` key (not
    ``memory_units``). The client must read it correctly even when the page
    is full (paginate) or partial (terminate).
    """

    def _resp_body(self, units: list[dict], total: int):
        return {
            "items": units,
            "total": total,
            "limit": 100,
            "offset": 0,
        }

    def test_parses_items_field(self):
        unit = {
            "id": "5a0e493d-9581-4b45-acd7-abfc3a288cc1",
            "text": "用户当前居住在日本 | Involving: user",
            "fact_type": "world",
            "document_id": "用户居住地",
            "state": "valid",
        }
        with mock.patch.object(
            client_mod.urllib.request, "urlopen", return_value=_resp(self._resp_body([unit], 1))
        ):
            units = HindsightClient(base_url="http://t").list_memory_units("bank-1", "用户居住地")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["id"], unit["id"])
        self.assertEqual(units[0]["text"], unit["text"])

    def test_paginates_until_short_page(self):
        page1 = [{"id": f"u-{i}", "text": f"t-{i}"} for i in range(3)]
        page2 = [{"id": "u-99", "text": "final"}]
        seen_offset: list[int] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(req.full_url).query)
            seen_offset.append(int(q["offset"][0]))
            offset = seen_offset[-1]
            if offset == 0:
                return _resp(self._resp_body(page1, 4))
            return _resp(self._resp_body(page2, 4))

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            units = HindsightClient(base_url="http://t", timeout=5).list_memory_units(
                "bank-1", "doc-1", limit=3
            )
        self.assertEqual(len(units), 4)
        self.assertEqual([u["id"] for u in units], ["u-0", "u-1", "u-2", "u-99"])

    def test_handles_unicode_document_id(self):
        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            seen_urls.append(req.full_url)
            return _resp(self._resp_body([], 0))

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            HindsightClient(base_url="http://t").list_memory_units("bank-1", "用户居住地")
        # document_id must be percent-encoded, not raw UTF-8 in the URL.
        self.assertIn("document_id=%E7%94%A8%E6%88%B7%E5%B1%85%E4%BD%8F%E5%9C%B0", seen_urls[0])


if __name__ == "__main__":
    unittest.main()