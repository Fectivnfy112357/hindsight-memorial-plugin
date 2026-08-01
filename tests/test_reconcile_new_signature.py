"""Unit tests for the new ``run_reconcile`` signature.

The 2026-08-01 design changes the signature from
``run_reconcile(new_fact, *, load_cfg, cwd, dry_run, exclude_unit_ids)``
to ``run_reconcile(bank_id, unit_id, content, *, load_cfg, dry_run)`` so the
poller can drive the pipeline one row at a time and ``exclude_unit_ids``
is computed from the row itself (defence against the LLM listing the
just-retained fact in its supersede list — see commit a4ac52d).

These tests pin the new contract. The legacy signature tests still live
in ``test_reconcile.py`` and are kept working with a thin adapter on
top of the new core path; once #10 lands they will be migrated.
"""
from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass
from typing import Any
from unittest import mock

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial import reconcile
from hindsight_memorial.config import MemorialConfig


# ── doubles ─────────────────────────────────────────────────────────────


@dataclass
class _FakeResponse:
    body: dict[str, Any]

    def read(self):
        return json.dumps(self.body).encode()


class _FakeClient:
    """Mirror of the FakeClient in test_reconcile.py, kept private here
    so the two test files do not accidentally share state via globals."""

    def __init__(
        self,
        *,
        banks: list[str] | None = None,
        reflect_seq: list[dict[str, Any]] | None = None,
        reflect_errors: list[Exception] | None = None,
        patch_responses: list[dict[str, Any]] | None = None,
        delete_responses: list[dict[str, Any]] | None = None,
    ):
        self.banks = list(banks or [])
        self._reflect_seq = list(reflect_seq or [])
        self._reflect_errors = list(reflect_errors or [])
        self._patch_responses = list(patch_responses or [])
        self._delete_responses = list(delete_responses or [])
        self.reflect_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.clear_calls: list[str] = []

    def list_banks(self):
        return self.banks

    def reflect(self, bank_id, query, *, structured_output=None):
        self.reflect_calls.append(
            {
                "bank_id": bank_id,
                "query": query,
                "structured_output": structured_output,
            }
        )
        if self._reflect_errors:
            raise self._reflect_errors.pop(0)
        if not self._reflect_seq:
            raise AssertionError("reflect called more than responses configured")
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


def _cfg(bank_id: str = "b1") -> MemorialConfig:
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


# ── new signature surface ───────────────────────────────────────────────


class NewSignatureTest(unittest.TestCase):
    def test_takes_bank_unit_and_content_positionally(self):
        """The new signature is ``(bank_id, unit_id, content, *, load_cfg)``.
        The legacy ``new_fact`` keyword is gone."""
        mem_id = "11111111-1111-1111-1111-111111111111"
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [],
                        "reasoning": "no",
                    }
                }
            ],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id=mem_id,
                content="some fact",
                load_cfg=_loader(_cfg()),
            )
        # No superseded → abandoned; the point is the call worked.
        self.assertEqual(result.status, "abandoned")

    def test_excludes_self_via_unit_id(self):
        """Even if the LLM lists the new fact itself in superseded_fact_ids,
        run_reconcile must filter it out (defence against the
        'self-invalidation' class of bug fixed in commit a4ac52d)."""
        self_id = "11111111-1111-1111-1111-111111111111"
        other_id = "22222222-2222-2222-2222-222222222222"
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [self_id, other_id],
                        "reasoning": "the LLM listed the new fact too",
                    }
                }
            ],
            patch_responses=[{"memory": {"id": other_id, "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id=self_id,
                content="some fact",
                load_cfg=_loader(_cfg()),
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.superseded_count, 1)
        # Only the OTHER id was curated; the self id never reached PATCH.
        self.assertEqual(len(client.update_calls), 1)
        self.assertEqual(client.update_calls[0]["memory_id"], other_id)

    def test_uses_structured_only_by_default(self):
        """Empty structured list must NOT fall through to scanning reasoning
        text for UUIDs. This pins the 2026-08-01 fix for issue #2."""
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [],
                        "reasoning": (
                            "uuid 33333333-3333-3333-3333-333333333333 "
                            "is in the candidate set but does not conflict"
                        ),
                    }
                }
            ],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact",
                load_cfg=_loader(_cfg()),
            )
        # Empty structured list → no supersede, no curate. The reasoning
        # UUID must NOT have been picked up.
        self.assertEqual(result.status, "abandoned")
        self.assertEqual(len(client.update_calls), 0)

    def test_curate_passes_reasoning_as_reason(self):
        """The 'reason' string passed to invalidate_memory is the reflect
        LLM's reasoning, truncated to a sane length — not the new fact
        text. This gives the Hindsight side a useful audit trail
        (superseded_memory_units.invalidation_reason)."""
        other_id = "22222222-2222-2222-2222-222222222222"
        reasoning_text = "this old fact is contradicted by the new one"
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [other_id],
                        "reasoning": reasoning_text,
                    }
                }
            ],
            patch_responses=[{"memory": {"id": other_id, "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact body",
                load_cfg=_loader(_cfg()),
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(client.update_calls), 1)
        reason = client.update_calls[0]["reason"]
        self.assertIn(reasoning_text, reason)

    def test_result_carries_reasoning_for_the_local_mirror(self):
        """ReconcileResult.reason must hold the same reasoning string sent
        to Hindsight. The poller reads it to fill the local
        superseded_reason column; when it was left None the poller silently
        fell back to a placeholder and the two audit trails diverged."""
        other_id = "22222222-2222-2222-2222-222222222222"
        reasoning_text = "this old fact is contradicted by the new one"
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [other_id],
                        "reasoning": reasoning_text,
                    }
                }
            ],
            patch_responses=[{"memory": {"id": other_id, "state": "invalidated"}}],
            delete_responses=[{"deleted_count": 1}],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact body",
                load_cfg=_loader(_cfg()),
            )
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.reason)
        self.assertIn(reasoning_text, result.reason)
        # Same string on both sides of the audit trail.
        self.assertEqual(result.reason, client.update_calls[0]["reason"])

    def test_reflect_failure_returns_reflect_failed(self):
        err = reconcile.HindsightAPIError(500, "boom", "http://test/reflect")
        client = _FakeClient(banks=["b1"], reflect_errors=[err])
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact",
                load_cfg=_loader(_cfg()),
            )
        self.assertEqual(result.status, "reflect_failed")
        self.assertEqual(result.error, str(err))
        # Curate must NOT have been called when reflect failed.
        self.assertEqual(len(client.update_calls), 0)

    def test_reflect_called_with_structured_schema(self):
        """run_reconcile must pass SUPERSEDED_SCHEMA as the response schema
        so the LLM returns the structured_output field we expect."""
        client = _FakeClient(
            banks=["b1"],
            reflect_seq=[
                {
                    "structured_output": {
                        "superseded_fact_ids": [],
                        "reasoning": "no",
                    }
                }
            ],
        )
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact",
                load_cfg=_loader(_cfg()),
            )
        self.assertEqual(len(client.reflect_calls), 1)
        self.assertEqual(
            client.reflect_calls[0]["structured_output"],
            reconcile.SUPERSEDED_SCHEMA,
        )
        # The HTTP-level ``include_based_on`` field was a casualty of
        # API drift — Hindsight never accepted it (the field only
        # existed on the MCP tool surface, not on the HTTP API). The
        # reflect request body must therefore NOT carry the
        # ``include_based_on`` key at all.
        self.assertNotIn("include_based_on", client.reflect_calls[0])

    def test_missing_bank_skipped(self):
        client = _FakeClient(banks=["other"])
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="missing",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact",
                load_cfg=_loader(_cfg("missing")),
            )
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.available_bank_count, 1)

    def test_no_api_url_returns_skipped(self):
        cfg = MemorialConfig(
            api_url="", api_key=None, bank_id="b1", bank_source="test"
        )
        result = reconcile.run_reconcile(
            bank_id="b1",
            unit_id="11111111-1111-1111-1111-111111111111",
            content="new fact",
            load_cfg=_loader(cfg),
        )
        self.assertEqual(result.status, "skipped")
        self.assertIn("api_url", (result.reason or "").lower())

    def test_dry_run_does_not_call_reflect(self):
        client = _FakeClient(banks=["b1"])
        with mock.patch.object(reconcile, "HindsightClient") as HC:
            HC.from_memorial_config.return_value = client
            result = reconcile.run_reconcile(
                bank_id="b1",
                unit_id="11111111-1111-1111-1111-111111111111",
                content="new fact",
                load_cfg=_loader(_cfg()),
                dry_run=True,
            )
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(len(client.reflect_calls), 0)
        self.assertEqual(len(client.update_calls), 0)


if __name__ == "__main__":
    unittest.main()
