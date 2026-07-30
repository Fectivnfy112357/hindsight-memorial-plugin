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

    Note on temporal wording: a new fact may use past-tense markers ("曾", "previously lived in")
    while still expressing a corrective update on a current-state fact. The LLM should treat
    topic-level supersession as the trigger, not tense alone.
    """
    return (
        f"A new fact was just retained into bank `{bank_id}`:\n\n"
        f"    >>> {new_fact_text.strip()} <<<\n\n"
        "Inspect the existing facts in this bank. Return the UUIDs of any facts that this new one\n"
        "has rendered stale, contradicted, or directly superseded. Look for facts about the SAME\n"
        "topic (user location, preferences, project state, file paths, tool choices, etc.) — even if\n"
        "the new fact uses past tense (\"曾\", \"previously\", \"used to\") it can still supersede a\n"
        "current-state fact that contradicts it. Examples:\n"
        "  - a previous fact named the same file/method/module by an old name\n"
        "  - a previous fact recorded a path that has since moved\n"
        "  - a previous fact said something this new one directly negates\n"
        "  - a previous fact recorded \"currently in X\" but this new fact states the user\n"
        "    previously was in X (and the previous fact is therefore stale)\n\n"
        "Do NOT return facts that are merely related or co-occurring. Only return facts whose\n"
        "truth value is materially affected by the new fact."
    )


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def extract_superseded_ids(
    reflect_response: dict[str, Any],
    exclude_ids: list[str] | None = None,
) -> list[str]:
    """Pull the UUIDs out of the reflect response.

    Prefers the structured `structured_output.superseded_fact_ids` field. If that field is
    missing or empty, falls back to scanning the response text for UUIDs — the reflect LLM often
    repeats the ids inside `based_on.memories` or its prose answer, and we don't want to lose
    them just because structured-output parsing failed.

    ``exclude_ids`` lets the caller drop the id of the freshly retained fact itself:
    the reflect LLM sometimes lists the new fact alongside the ones it supersedes
    (because its prompt asked for "facts this new one has rendered stale" and the
    LLM reads "stale" loosely enough to include the new fact in its own enumeration).
    Without this filter memorial would PATCH-invalidate the very fact it just
    retained, losing the current truth.
    """
    exclude = {str(x).lower() for x in (exclude_ids or []) if x}
    found: list[str] = []

    structured = reflect_response.get("structured_output")
    if isinstance(structured, dict):
        raw_ids = structured.get("superseded_fact_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                if isinstance(item, str) and item.lower() not in exclude:
                    found.append(item)

    if not found:
        text_candidates: list[str] = []
        if isinstance(structured, dict) and isinstance(structured.get("reasoning"), str):
            text_candidates.append(structured["reasoning"])
        if isinstance(reflect_response.get("text"), str):
            text_candidates.append(reflect_response["text"])
        for blob in text_candidates:
            for match in _UUID_RE.findall(blob):
                if match.lower() not in exclude:
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