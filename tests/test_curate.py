"""Unit tests for the curate module (PATCH invalidate + DELETE observations)."""
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
from hindsight_memorial.curate import curate_many, curate_memory


def _http_error(status: int, body: str = "{}"):
    return urllib.error.HTTPError(
        url="http://x", code=status, msg="err", hdrs={}, fp=io.BytesIO(body.encode())
    )


def _resp(body: dict):
    m = mock.Mock()
    m.read.return_value = json.dumps(body).encode()
    m.__enter__ = mock.Mock(return_value=m)
    m.__exit__ = mock.Mock(return_value=False)
    return m


class CurateMemoryTest(unittest.TestCase):
    def test_happy_path_calls_patch_then_delete(self):
        calls: list[tuple[str, str]] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            calls.append((req.method, req.full_url))
            if req.method == "PATCH":
                return _resp({"memory": {"state": "invalidated"}})
            if req.method == "DELETE":
                return _resp({"deleted_count": 3})
            raise AssertionError(f"unexpected {req.method}")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            r = curate_memory(
                HindsightClient(base_url="http://t"),
                "b1",
                "11111111-1111-1111-1111-111111111111",
                reason="r",
            )
        self.assertTrue(r.invalidated)
        self.assertTrue(r.observations_cleared)
        self.assertIsNone(r.error)
        self.assertEqual([c[0] for c in calls], ["PATCH", "DELETE"])

    def test_patch_failure_aborts_delete(self):
        calls: list[str] = []

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            calls.append(req.method)
            raise _http_error(404, "not found")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            r = curate_memory(
                HindsightClient(base_url="http://t"),
                "b1",
                "11111111-1111-1111-1111-111111111111",
                reason="r",
            )
        self.assertFalse(r.invalidated)
        self.assertFalse(r.observations_cleared)
        self.assertIsNotNone(r.error)
        self.assertEqual(calls, ["PATCH"])  # never tried DELETE

    def test_delete_failure_returns_partial(self):
        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            if req.method == "PATCH":
                return _resp({"memory": {"state": "invalidated"}})
            raise _http_error(500, "oops")

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            r = curate_memory(
                HindsightClient(base_url="http://t"),
                "b1",
                "11111111-1111-1111-1111-111111111111",
                reason="r",
            )
        self.assertTrue(r.invalidated)  # patch succeeded
        self.assertFalse(r.observations_cleared)
        self.assertIsNotNone(r.error)


class CurateManyTest(unittest.TestCase):
    def test_processes_each_id_and_reports_aggregate(self):
        responses = [
            _resp({"memory": {"state": "invalidated"}}),
            _resp({"deleted_count": 1}),
            _http_error(404, "x"),  # second id: patch fails
        ]

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            if not responses:
                raise AssertionError("too many calls")
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(client_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            report = curate_many(
                HindsightClient(base_url="http://t"),
                "b1",
                [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
                reason="r",
            )

        self.assertEqual(len(report.results), 2)
        self.assertEqual(report.invalidated_count, 1)
        self.assertEqual(report.observations_cleared_count, 1)
        self.assertEqual(report.error_count, 1)
        d = report.to_dict()
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["invalidated"], 1)


if __name__ == "__main__":
    unittest.main()