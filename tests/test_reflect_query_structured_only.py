"""Unit tests for the reflect_query module — covering the structured_only
parameter added in the 2026-08-01 design (issue #2 from
doc/webhook-runtime-findings-2026-07-31.md: structured verdict with empty
ids must not fall through to scanning reasoning prose for UUIDs)."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial.reflect_query import extract_superseded_ids


# ── structured_only=True (the new default) ──────────────────────────────


class StructuredOnlyTrueTest(unittest.TestCase):
    def test_uses_structured_ids_when_present_and_non_empty(self):
        resp = {
            "structured_output": {
                "superseded_fact_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
                "reasoning": "the new fact contradicts this old one",
            }
        }
        ids = extract_superseded_ids(resp, structured_only=True)
        self.assertEqual(ids, ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])

    def test_empty_structured_list_returns_empty_no_fallback(self):
        """Issue #2: structured list is empty → there is no superseded id.
        Reasoning text may contain UUIDs (e.g. proof quotes, or 'no match
        found' phrasings), but those must NOT be acted on."""
        resp = {
            "structured_output": {
                "superseded_fact_ids": [],
                "reasoning": (
                    "Inspected candidate uuid bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb "
                    "but the new fact does not materially negate it."
                ),
            }
        }
        ids = extract_superseded_ids(resp, structured_only=True)
        self.assertEqual(ids, [])

    def test_missing_structured_key_returns_empty(self):
        """When the structured_output key itself is missing, structured_only
        mode returns nothing — there is no structured list to read."""
        resp = {"text": "found uuid cccccccc-cccc-cccc-cccc-cccccccccccc"}
        ids = extract_superseded_ids(resp, structured_only=True)
        self.assertEqual(ids, [])

    def test_exclude_ids_applies_in_structured_only_mode(self):
        resp = {
            "structured_output": {
                "superseded_fact_ids": [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
            }
        }
        ids = extract_superseded_ids(
            resp,
            exclude_ids=["11111111-1111-1111-1111-111111111111"],
            structured_only=True,
        )
        self.assertEqual(ids, ["22222222-2222-2222-2222-222222222222"])


# ── structured_only=False (legacy fallback path) ───────────────────────


class StructuredOnlyFalseTest(unittest.TestCase):
    def test_falls_back_to_reasoning_uuids_when_structured_empty(self):
        """The legacy fallback is the pre-fix behavior. It is preserved
        only for callers that opt in via structured_only=False — the
        default must be True."""
        resp = {
            "structured_output": {
                "superseded_fact_ids": [],
                "reasoning": "matches dddddddd-dddd-dddd-dddd-dddddddddddd",
            },
            "text": "no UUIDs here",
        }
        ids = extract_superseded_ids(resp, structured_only=False)
        self.assertEqual(ids, ["dddddddd-dddd-dddd-dddd-dddddddddddd"])

    def test_falls_back_to_text_field_uuids(self):
        resp = {
            "text": "I see eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee in the bank",
        }
        ids = extract_superseded_ids(resp, structured_only=False)
        self.assertEqual(ids, ["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"])

    def test_legacy_fallback_still_applies_exclude_ids(self):
        resp = {
            "text": "11111111-1111-1111-1111-111111111111 22222222-2222-2222-2222-222222222222",
        }
        ids = extract_superseded_ids(
            resp,
            exclude_ids=["11111111-1111-1111-1111-111111111111"],
            structured_only=False,
        )
        self.assertEqual(ids, ["22222222-2222-2222-2222-222222222222"])


# ── default value ───────────────────────────────────────────────────────


class DefaultStructuredOnlyTest(unittest.TestCase):
    def test_default_is_true(self):
        """Not passing structured_only must behave as structured_only=True."""
        resp = {
            "structured_output": {
                "superseded_fact_ids": [],
                "reasoning": "ffffffff-ffff-ffff-ffff-ffffffffffff in candidate list",
            }
        }
        ids = extract_superseded_ids(resp)
        self.assertEqual(ids, [])

    def test_invalid_uuid_strings_in_structured_are_dropped(self):
        resp = {
            "structured_output": {
                "superseded_fact_ids": [
                    "not-a-uuid",
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                ],
            }
        }
        ids = extract_superseded_ids(resp, structured_only=True)
        self.assertEqual(ids, ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"])


if __name__ == "__main__":
    unittest.main()
