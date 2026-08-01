"""Unit tests for extract_superseded_ids."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "D:/programming/projects/hindsight-memorial")

from hindsight_memorial.reflect_query import extract_superseded_ids


UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


class StructuredPathTest(unittest.TestCase):
    def test_reads_structured_field(self):
        resp = {"structured_output": {"superseded_fact_ids": [UUID_A, UUID_B], "reasoning": "x"}}
        self.assertEqual(extract_superseded_ids(resp), [UUID_A, UUID_B])

    def test_normalises_case(self):
        resp = {"structured_output": {"superseded_fact_ids": [UUID_A.upper()], "reasoning": "x"}}
        self.assertEqual(extract_superseded_ids(resp), [UUID_A])


class FallbackTest(unittest.TestCase):
    """Legacy fallback behaviour — only enabled when the caller explicitly
    passes ``structured_only=False``. These tests pin the behaviour of
    that opt-in path. The default (and production) path is
    ``structured_only=True`` and is covered in
    ``test_reflect_query_structured_only.py``.
    """

    def test_falls_back_to_text_when_structured_missing(self):
        resp = {"text": f"Superseded by new fact: {UUID_A}, also {UUID_B}"}
        self.assertEqual(
            extract_superseded_ids(resp, structured_only=False), [UUID_A, UUID_B]
        )

    def test_falls_back_to_reasoning_when_both_missing(self):
        resp = {"structured_output": {"reasoning": f"id={UUID_C} is old"}}
        self.assertEqual(
            extract_superseded_ids(resp, structured_only=False), [UUID_C]
        )

    def test_dedupes(self):
        resp = {
            "structured_output": {"superseded_fact_ids": [UUID_A, UUID_A]},
            "text": f"and {UUID_A}",
        }
        # structured_only=True: just the structured list (one entry, deduped).
        self.assertEqual(extract_superseded_ids(resp, structured_only=True), [UUID_A])
        # structured_only=False: same answer, but proven across both paths.
        self.assertEqual(extract_superseded_ids(resp, structured_only=False), [UUID_A])

    def test_ignores_garbage(self):
        resp = {"structured_output": {"superseded_fact_ids": ["not-a-uuid", UUID_A]}}
        self.assertEqual(extract_superseded_ids(resp), [UUID_A])


class ExcludeIdsTest(unittest.TestCase):
    """``exclude_ids`` filters out the freshly retained fact itself so the
    reflect LLM cannot trick memorial into PATCH-invalidating the current
    truth (it sometimes lists the new fact alongside the ones it supersedes).
    """

    NEW_FACT = "44444444-4444-4444-4444-444444444444"

    def test_excludes_id_in_structured_field(self):
        resp = {
            "structured_output": {
                "superseded_fact_ids": [self.NEW_FACT, UUID_A, UUID_B]
            }
        }
        # Without filter: 3 ids.
        self.assertEqual(len(extract_superseded_ids(resp)), 3)
        # With filter: 2 ids (NEW_FACT dropped).
        self.assertEqual(
            extract_superseded_ids(resp, exclude_ids=[self.NEW_FACT]),
            [UUID_A, UUID_B],
        )

    def test_excludes_case_insensitive(self):
        upper = self.NEW_FACT.upper()
        resp = {
            "structured_output": {"superseded_fact_ids": [upper, UUID_A]}
        }
        self.assertEqual(
            extract_superseded_ids(resp, exclude_ids=[self.NEW_FACT]),
            [UUID_A],
        )

    def test_excludes_id_in_text_fallback(self):
        # When structured_output is empty AND the caller opts in to the
        # legacy fallback, we scan text. exclude_ids must still apply.
        resp = {
            "structured_output": {},
            "text": f"the new fact {self.NEW_FACT} supersedes {UUID_A}",
        }
        self.assertEqual(
            extract_superseded_ids(
                resp, exclude_ids=[self.NEW_FACT], structured_only=False
            ),
            [UUID_A],
        )

    def test_exclude_ids_none_is_noop(self):
        resp = {
            "structured_output": {"superseded_fact_ids": [self.NEW_FACT, UUID_A]}
        }
        self.assertEqual(
            extract_superseded_ids(resp, exclude_ids=None),
            [self.NEW_FACT, UUID_A],
        )


if __name__ == "__main__":
    unittest.main()