"""Build & parse the structured reflect call used to detect superseded facts.

The reflect LLM is given a freshly-retained fact and asked which existing facts in the same bank
the new one has superseded. We constrain the response with `response_schema` so we can pull a
clean `superseded_fact_ids` list out of the result.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

SUPERSEDED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "superseded_fact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "UUIDs of memory_units in the same bank that the new fact has rendered stale, "
                "contradicted, or otherwise superseded. Empty if none."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One short paragraph explaining why each id was chosen.",
        },
    },
    "required": ["superseded_fact_ids", "reasoning"],
    "additionalProperties": False,
}


def build_query(new_fact_text: str, *, bank_id: str) -> str:
    """Construct the natural-language prompt that goes into reflect().

    The prompt is intentionally narrow: only ask about *supersession*, not general cleanup. This
    keeps the reflect LLM focused and reduces the chance of it inventing unrelated edits.
    """
    return (
        f"A new fact was just retained into bank `{bank_id}`:\n\n"
        f"    >>> {new_fact_text.strip()} <<<\n\n"
        "Inspect the existing facts in this bank. Return the UUIDs of any facts that this new one\n"
        "has rendered **stale, contradicted, or directly superseded**. Examples:\n"
        "  - a previous fact named the same file/method/module by an old name\n"
        "  - a previous fact recorded a path that has since moved\n"
        "  - a previous fact said something this new one directly negates\n\n"
        "Do NOT return facts that are merely related or co-occurring. Only return facts whose\n"
        "truth value is materially affected by the new fact.\n"
    )


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def extract_superseded_ids(reflect_response: dict[str, Any]) -> list[str]:
    """Pull the UUIDs out of the reflect response.

    Prefers the structured `structured_output.superseded_fact_ids` field. If that field is
    missing or empty, falls back to scanning the response text for UUIDs — the reflect LLM often
    repeats the ids inside `based_on.memories` or its prose answer, and we don't want to lose
    them just because structured-output parsing failed.
    """
    found: list[str] = []

    structured = reflect_response.get("structured_output")
    if isinstance(structured, dict):
        raw_ids = structured.get("superseded_fact_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                if isinstance(item, str):
                    found.append(item)

    if not found:
        text_candidates: list[str] = []
        if isinstance(structured, dict) and isinstance(structured.get("reasoning"), str):
            text_candidates.append(structured["reasoning"])
        if isinstance(reflect_response.get("text"), str):
            text_candidates.append(reflect_response["text"])
        for blob in text_candidates:
            for match in _UUID_RE.findall(blob):
                found.append(match)

    # Deduplicate while preserving order, and validate UUID format.
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in found:
        try:
            normalised = str(uuid.UUID(candidate))
        except ValueError:
            continue
        if normalised not in seen:
            seen.add(normalised)
            deduped.append(normalised)
    return deduped