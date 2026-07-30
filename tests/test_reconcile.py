"""Unit tests for the shared ``hindsight_memorial.reconcile`` pipeline.

These tests do not hit a live Hindsight server; they patch the
``HindsightClient`` instance built inside ``run_reconcile`` and assert that
the pipeline drives it correctly across retry, abandonment, and curate
paths.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from hindsight_memorial import reconcile
from hindsight_memorial.config import MemorialConfig


@dataclass
class FakeResponse:
    body: dict[str, Any]

    def read(self):
        return json.dumps(self.body).encode()


@dataclass
class FakeHTTPError:
    code: int
    msg: str = "err"

    def read(self):
        return b"{}"

    @property
    def reason(self):  # mimic urllib HTTPError.reason
        return self.msg


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


class _RecordingSleep:
    """Records every sleep call without blocking, so retry-path tests stay fast."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, delay: float) -> None:
        self.calls.append(delay)


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


class CleanOnFirstAttemptTest(unittest.TestCase):
    def test_clean_on_first_attempt_no_retry(self):
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": ["11111111-1111-1111-1111-111111111111"], "reasoning": "x"}},
            ],
            patch_responses=[{"memory": {"id": "11111111-1111-1111-1111-111111111111", "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        sleeper = _RecordingSleep()
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "x renamed to y",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=sleeper,
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.superseded_count, 1)
        # One attempt, only the initial wait — no retries fired.
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(sleeper.calls, [reconcile.RECONCILE_INITIAL_DELAY])

    def test_first_attempt_empty_retry_succeeds(self):
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": [], "reasoning": "no"}},
                {"structured_output": {"superseded_fact_ids": ["22222222-2222-2222-2222-222222222222"], "reasoning": "yes"}},
            ],
            patch_responses=[{"memory": {"id": "22222222-2222-2222-2222-222222222222", "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        sleeper = _RecordingSleep()
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "x renamed to y",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=sleeper,
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.superseded_count, 1)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(
            sleeper.calls,
            [reconcile.RECONCILE_INITIAL_DELAY, reconcile.RECONCILE_RETRY_DELAYS[0]],
        )

    def test_abandons_after_initial_and_all_retries_empty(self):
        # Initial reflect empty + 2 retry reflects empty -> 3 attempts total.
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": [], "reasoning": "no"}},
                {"structured_output": {"superseded_fact_ids": [], "reasoning": "no"}},
                {"structured_output": {"superseded_fact_ids": [], "reasoning": "no"}},
            ],
        )
        sleeper = _RecordingSleep()
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "anything",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=sleeper,
            )
        self.assertEqual(outcome.status, "abandoned")
        # attempts = 1 initial + len(RECONCILE_RETRY_DELAYS) retries
        self.assertEqual(
            outcome.attempts,
            1 + len(reconcile.RECONCILE_RETRY_DELAYS),
        )
        self.assertEqual(
            sleeper.calls,
            [reconcile.RECONCILE_INITIAL_DELAY, *reconcile.RECONCILE_RETRY_DELAYS],
        )
        self.assertIn("no superseded facts", outcome.reason or "")

    def test_reflect_error_initial_then_retry_succeeds(self):
        # Initial reflect fails, retry succeeds.
        client = FakeClient(
            banks=["b1"],
            reflect_seq=[
                {"structured_output": {"superseded_fact_ids": ["33333333-3333-3333-3333-333333333333"], "reasoning": "yes"}},
            ],
            patch_responses=[{"memory": {"id": "33333333-3333-3333-3333-333333333333", "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        sleeper = _RecordingSleep()
        err = reconcile.HindsightAPIError(500, "boom", "http://test/reflect")
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            # First call: raise; subsequent calls: succeed from reflect_seq.
            original = client.reflect
            call_state = {"n": 0}

            def reflect(bank_id, query, **kw):
                call_state["n"] += 1
                if call_state["n"] == 1:
                    raise err
                return original(bank_id, query, **kw)

            client.reflect = reflect
            outcome = reconcile.run_reconcile(
                "x renamed to y",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=sleeper,
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.superseded_count, 1)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(
            sleeper.calls,
            [reconcile.RECONCILE_INITIAL_DELAY, reconcile.RECONCILE_RETRY_DELAYS[0]],
        )

    def test_all_attempts_error_returns_reflect_failed(self):
        err = reconcile.HindsightAPIError(500, "boom", "http://test/reflect")
        client = FakeClient(
            banks=["b1"],
            reflect_errors=[err, err, err],
        )
        sleeper = _RecordingSleep()
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "anything",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=sleeper,
            )
        self.assertEqual(outcome.status, "reflect_failed")
        self.assertEqual(
            outcome.attempts,
            1 + len(reconcile.RECONCILE_RETRY_DELAYS),
        )
        self.assertEqual(
            sleeper.calls,
            [reconcile.RECONCILE_INITIAL_DELAY, *reconcile.RECONCILE_RETRY_DELAYS],
        )


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
                "x renamed to y",
                load_cfg=_loader(_mem_cfg()),
                sleep_fn=lambda *_a, **_k: None,
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
        sleeps: list[float] = []
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            outcome = reconcile.run_reconcile(
                "foo renamed to bar",
                load_cfg=_loader(_mem_cfg()),
                dry_run=True,
                sleep_fn=sleeps.append,
            )
        self.assertEqual(outcome.status, "dry_run")
        self.assertEqual(client.reflect_calls, [])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.clear_calls, [])
        self.assertIn("foo renamed to bar", outcome.new_fact_preview)


if __name__ == "__main__":
    unittest.main()
