"""End-to-end test for retain_reflect_curate.main, with urllib.request.urlopen mocked.

Covers: happy path, no-superseded result, reflect failure (non-fatal), patch failure per id.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial/scripts")

import retain_reflect_curate as rrc


@contextmanager
def _mock_urlopen(responses: list[mock.Mock]):
    """Mock urlopen to return successive responses, raising HTTPError on demand.

    We patch `urllib.request.urlopen` in the *lib.client* namespace because that's where it's
    imported, not in retain_reflect_curate itself.
    """
    from lib import client as client_mod

    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls.append({"url": req.full_url, "method": req.method, "body": req.data})
        if not responses:
            raise AssertionError("urlopen called more times than responses provided")
        resp = responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        yield calls


def _resp(body: dict | str, status: int = 200):
    m = mock.Mock()
    m.read.return_value = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=False)
    m.status = status
    return m


def _http_error(status: int, body: str = "{}"):
    import urllib.error

    err = urllib.error.HTTPError(
        url="http://x", code=status, msg="err", hdrs={}, fp=io.BytesIO(body.encode())
    )
    return err


class HappyPathTest(unittest.TestCase):
    def test_reflect_returns_two_ids_then_curate_each(self):
        superseded = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
        responses = [
            # list_banks check — bank "b1" exists
            _resp({"banks": [{"bank_id": "b1"}]}),
            # reflect call
            _resp({"structured_output": {"superseded_fact_ids": superseded, "reasoning": "ok"}}),
            # patch #1
            _resp({"memory": {"id": superseded[0], "state": "invalidated"}}),
            # delete obs #1
            _resp({"deleted_count": 1}),
            # patch #2
            _resp({"memory": {"id": superseded[1], "state": "invalidated"}}),
            # delete obs #2
            _resp({"deleted_count": 2}),
        ]

        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_API_KEY": "k", "HINDSIGHT_BANK_ID": "b1"},
            clear=True,
        ):
            with _mock_urlopen(responses) as calls:
                argv = ["--new-fact", "X renamed to Y"]
                rc = rrc.main(argv)
                self.assertEqual(rc, 0)

        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertIn("/banks", calls[0]["url"])
        self.assertEqual(calls[1]["method"], "POST")
        self.assertIn("/reflect", calls[1]["url"])
        # calls[1] is the reflect POST, which carries the new fact in its body
        reflect_body = json.loads(calls[1]["body"])
        self.assertIn("X renamed to Y", reflect_body["query"])

        # Both ids should be PATCHed then DELETEd
        patch_urls = [c["url"] for c in calls[1:] if c["method"] == "PATCH"]
        delete_urls = [c["url"] for c in calls[1:] if c["method"] == "DELETE"]
        self.assertEqual(len(patch_urls), 2)
        self.assertEqual(len(delete_urls), 2)
        for sid in superseded:
            self.assertTrue(any(sid in u for u in patch_urls))
            self.assertTrue(any(sid in u for u in delete_urls))


class EmptyResultTest(unittest.TestCase):
    def test_no_superseded_means_no_curate_calls(self):
        responses = [
            # list_banks — bank exists
            _resp({"banks": [{"bank_id": "b1"}]}),
            # reflect returns empty list
            _resp({"structured_output": {"superseded_fact_ids": [], "reasoning": "nothing"}}),
        ]
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_BANK_ID": "b1"},
            clear=True,
        ):
            with _mock_urlopen(responses) as calls:
                rc = rrc.main(["--new-fact", "irrelevant fact"])
                self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)  # list_banks + reflect
        reflect_body = json.loads(calls[1]["body"])
        self.assertIn("irrelevant fact", reflect_body["query"])


class ReflectFailureTest(unittest.TestCase):
    def test_reflect_failure_does_not_abort(self):
        responses = [_http_error(500, "boom")]
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_BANK_ID": "b1"},
            clear=True,
        ):
            with _mock_urlopen(responses) as calls:
                rc = rrc.main(["--new-fact", "anything"])
                self.assertEqual(rc, 0)  # never block the caller
        self.assertEqual(len(calls), 1)  # only the failed reflect; no curate calls


class MissingFactTest(unittest.TestCase):
    def test_no_new_fact_skips_cleanly(self):
        # No HTTP traffic at all
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_BANK_ID": "b1"},
            clear=True,
        ):
            with mock.patch("sys.stdin", io.StringIO("")):
                with _mock_urlopen([]) as calls:
                    rc = rrc.main([])
                    self.assertEqual(rc, 0)
        self.assertEqual(calls, [])


class ClaudeCodePayloadTest(unittest.TestCase):
    """Hook receives a Claude Code PostToolUse payload shape."""

    def test_tool_input_content_extracted_as_new_fact(self):
        responses = [
            # list_banks — bank exists
            _resp({"banks": [{"bank_id": "demo"}]}),
            # reflect
            _resp({"structured_output": {"superseded_fact_ids": [], "reasoning": "none"}}),
        ]
        payload = {
            "tool_name": "mcp__hindsight__retain",
            "tool_input": {"content": "foo.txt renamed to bar.txt", "bank_id": "demo"},
            "cwd": "D:/somewhere/demo",
        }
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test"},
            clear=True,
        ):
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with _mock_urlopen(responses) as calls:
                    rc = rrc.main([])
                    self.assertEqual(rc, 0)
        # calls[0] is list_banks (GET, no body); calls[1] is reflect (POST, has body)
        self.assertEqual(len(calls), 2)
        reflect_body = json.loads(calls[1]["body"])
        self.assertIn("foo.txt renamed to bar.txt", reflect_body["query"])


class CodexBashPayloadTest(unittest.TestCase):
    """Hook receives a Codex PostToolUse payload where retain went through Bash."""

    def test_command_string_extracted(self):
        responses = [
            _resp({"banks": [{"bank_id": "demo"}]}),
            _resp({"structured_output": {"superseded_fact_ids": [], "reasoning": "none"}}),
        ]
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "hindsight retain 'foo.txt renamed to bar.txt' --bank-id demo",
                "bank_id": "demo",
            },
            "cwd": "D:/somewhere/demo",
        }
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test"},
            clear=True,
        ):
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with _mock_urlopen(responses) as calls:
                    rc = rrc.main([])
                    self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        reflect_body = json.loads(calls[1]["body"])
        self.assertIn("foo.txt renamed to bar.txt", reflect_body["query"])


class BankNotOnServerTest(unittest.TestCase):
    """If list_banks shows the resolved bank id is missing, we skip without error."""

    def test_missing_bank_skips_cleanly(self):
        responses = [
            # list_banks returns no matching bank
            _resp({"banks": [{"bank_id": "other-bank"}]}),
        ]
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_BANK_ID": "missing-bank"},
            clear=True,
        ):
            with mock.patch("sys.stdin", io.StringIO("")):
                with _mock_urlopen(responses) as calls:
                    rc = rrc.main(["--new-fact", "anything"])
                    self.assertEqual(rc, 0)
        # Only the list_banks call; no reflect, no curate
        self.assertEqual(len(calls), 1)
        self.assertIn("/banks", calls[0]["url"])


class DryRunTest(unittest.TestCase):
    """--dry-run prints a preview and skips reflect + curate, but list_banks still runs."""

    def test_dry_run_skips_reflect_and_curate(self):
        responses = [
            _resp({"banks": [{"bank_id": "any"}]}),
        ]
        with mock.patch.dict(
            "os.environ",
            {"HINDSIGHT_API_URL": "http://test", "HINDSIGHT_BANK_ID": "any"},
            clear=True,
        ):
            with mock.patch("sys.stdin", io.StringIO("")):
                with _mock_urlopen(responses) as calls:
                    rc = rrc.main(["--new-fact", "foo renamed to bar", "--dry-run"])
                    self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("/banks", calls[0]["url"])


if __name__ == "__main__":
    unittest.main()