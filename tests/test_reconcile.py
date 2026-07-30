"""Unit tests for the shared ``hindsight_memorial.reconcile`` pipeline.

Webhook-driven mode: ``run_reconcile`` performs a single reflect attempt.
There is no client-side retry policy — if reflect fails, the result is
``reflect_failed`` and the caller is expected to re-drive from the webhook
later. These tests do not hit a live Hindsight server; they patch the
``HindsightClient`` instance built inside ``run_reconcile``.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Any
from unittest import mock

from hindsight_memorial import reconcile
from hindsight_memorial.config import MemorialConfig


@dataclass
class FakeResponse:
    body: dict[str, Any]

    def read(self):
        return json.dumps(self.body).encode()


class FakeClient:
    """Drop-in replacement for :class:`HindsightClient` used inside run_reconcile."""

    def __init__(
        self,
        *,
        banks: list[str] | None = None,
        reflect_seq: list[dict[str, Any]] | None = None,
        reflect_errors: list[Exception] | None = None,
        patch_responses: list[dict[str, Any]] | None = None,
        delete_responses: list[dict[str, Any]] | None = None,
    ):
        self.banks = banks or []
        self._reflect_seq = list(reflect_seq or [])
        self._reflect_errors = list(reflect_errors or [])
        self._patch_responses = list(patch_responses or [])
        self._delete_responses = list(delete_responses or [])
        self.reflect_calls: list[str] = []
        self.list_banks_calls = 0
        self.update_calls: list[dict[str, Any]] = []
        self.clear_calls: list[str] = []

    def list_banks(self):
        self.list_banks_calls += 1
        return self.banks

    def reflect(self, bank_id, query, *, structured_output=None, include_based_on=True):
        self.reflect_calls.append(query)
        if self._reflect_errors:
            err = self._reflect_errors.pop(0)
            raise err
        if not self._reflect_seq:
            raise AssertionError("reflect called more times than responses configured")
        return self._reflect_seq.pop(0)

    def update_memory(self, bank_id, memory_id, *, state=None, reason=None):
        self.update_calls.append(
            {"bank_id": bank_id, "memory_id": memory_id, "state": state, "reason": reason}
        )
        if not self._patch_responses:
            return {"memory": {"id": memory_id, "state": state}}
        return self._patch_responses.pop(0)

    def clear_memory_observations(self, bank_id, memory_id):
        self.clear_calls.append(memory_id)
        if not self._delete_responses:
            return {"deleted_count": 1}
        return self._delete_responses.pop(0)


def _mem_cfg(bank_id: str = "b1") -> MemorialConfig:
    return MemorialConfig(
        api_url="http://test",
        api_key="k",
        bank_id=bank_id,
        bank_source="test",
    )


def _loader(cfg: MemorialConfig | None):
    def _l(cwd):
        return cfg

    return _l


class SkippedPathsTest(unittest.TestCase):
    def test_no_new_fact_returns_skipped(self):
        result = reconcile.run_reconcile("", load_cfg=_loader(_mem_cfg()))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.superseded_count, 0)

    def test_no_config_returns_skipped(self):
        result = reconcile.run_reconcile("hello", load_cfg=_loader(None))
        self.assertEqual(result.status, "skipped")

    def test_no_bank_id_returns_skipped(self):
        cfg = MemorialConfig(
            api_url="http://test", api_key=None, bank_id=None, bank_source="none"
        )
        result = reconcile.run_reconcile("hello", load_cfg=_loader(cfg))
        self.assertEqual(result.status, "skipped")

    def test_no_api_url_returns_skipped(self):
        cfg = MemorialConfig(
            api_url="", api_key=None, bank_id="b1", bank_source="test"
        )
        result = reconcile.run_reconcile("hello", load_cfg=_loader(cfg))
        self.assertEqual(result.status, "skipped")


class BankPresenceTest(unittest.TestCase):
    def test_missing_bank_skipped(self):
        client = FakeClient(banks=["other"])
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                "hello", load_cfg=_loader(_mem_cfg("missing"))
            )
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.available_bank_count, 1)


class SingleReflectAttemptTest(unittest.TestCase):
    def test_clean_on_first_attempt(self):
        mem_id = "11111111-1111-1111-1111-111111111111"
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": [mem_id], "reasoning": "x"}},
            ],
            patch_responses=[{"memory": {"id": mem_id, "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "x renamed to y", load_cfg=_loader(_mem_cfg())
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.superseded_count, 1)
        self.assertEqual(len(client.reflect_calls), 1)

    def test_no_supersession_returns_abandoned(self):
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": [], "reasoning": "no"}},
            ],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "anything", load_cfg=_loader(_mem_cfg())
            )
        self.assertEqual(outcome.status, "abandoned")
        self.assertIn("no superseded facts", outcome.reason or "")
        self.assertEqual(len(client.reflect_calls), 1)

    def test_reflect_error_returns_reflect_failed(self):
        err = reconcile.HindsightAPIError(500, "boom", "http://test/reflect")
        client = FakeClient(
            banks=["b1"],
            reflect_errors=[err],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "anything", load_cfg=_loader(_mem_cfg())
            )
        self.assertEqual(outcome.status, "reflect_failed")
        self.assertEqual(outcome.error, str(err))
        self.assertEqual(len(client.reflect_calls), 1)


class CurateResultsTest(unittest.TestCase):
    def test_results_to_dict_format(self):
        mem_id = "33333333-3333-3333-3333-333333333333"
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": [mem_id], "reasoning": "ok"}},
            ],
            patch_responses=[{"memory": {"id": mem_id, "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "x renamed to y", load_cfg=_loader(_mem_cfg())
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(outcome.results), 1)
        item = outcome.results[0]
        self.assertEqual(item["memory_id"], mem_id)
        self.assertTrue(item["invalidated"])
        self.assertTrue(item["observations_cleared"])
        self.assertIsNone(item["error"])


class DryRunTest(unittest.TestCase):
    def test_dry_run_skips_reflect_and_curate(self):
        client = FakeClient(banks=["b1"])
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "foo renamed to bar",
                load_cfg=_loader(_mem_cfg()),
                dry_run=True,
            )
        self.assertEqual(outcome.status, "dry_run")
        self.assertEqual(client.reflect_calls, [])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.clear_calls, [])
        self.assertIn("foo renamed to bar", outcome.new_fact_preview)


if __name__ == "__main__":
    unittest.main()