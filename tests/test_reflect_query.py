"""Unit tests for extract_superseded_ids."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "D:/programming/projects/hindsight-memorial/scripts")

from lib.reflect_query import extract_superseded_ids


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
    def test_falls_back_to_text_when_structured_missing(self):
        resp = {"text": f"Superseded by new fact: {UUID_A}, also {UUID_B}"}
        self.assertEqual(extract_superseded_ids(resp), [UUID_A, UUID_B])

    def test_falls_back_to_reasoning_when_both_missing(self):
        resp = {"structured_output": {"reasoning": f"id={UUID_C} is old"}}
        self.assertEqual(extract_superseded_ids(resp), [UUID_C])

    def test_dedupes(self):
        resp = {
            "structured_output": {"superseded_fact_ids": [UUID_A, UUID_A]},
            "text": f"and {UUID_A}",
        }
        self.assertEqual(extract_superseded_ids(resp), [UUID_A])

    def test_ignores_garbage(self):
        resp = {"structured_output": {"superseded_fact_ids": ["not-a-uuid", UUID_A]}}
        self.assertEqual(extract_superseded_ids(resp), [UUID_A])


if __name__ == "__main__":
    unittest.main()