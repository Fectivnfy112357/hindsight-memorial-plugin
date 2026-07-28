---
name: hindsight-memorial
description: This skill should be used when the user asks to "clean up stale memories", "purge pollution from hindsight", "forget an outdated fact", "invalidate superseded memory", "audit my recall context", "what facts are stale?", or mentions Hindsight memory cleanup, world-fact expiry, or contamination from renamed files/moved modules. Provides a hands-off PostToolUse hook workflow for cleaning stale Hindsight memories after each retain.
version: 0.1.0
---

# Hindsight Memorial — Client-side Pollution Cleanup

When the user retains a new fact into Hindsight and that new fact contradicts, supersedes, or
renames something already in the bank, the old fact becomes pollution. Every future recall will
pull both the old and the new fact, contaminating the agent's context. Hindsight's server-side
consolidation cannot fully prevent this because the old fact is not obviously wrong in isolation;
it only becomes wrong in light of the *new* fact the user just told the agent about.

Memorial solves this by hooking the `retain` tool. After each retain, a Python script:

1. Calls `POST /v1/default/banks/{bank_id}/reflect` with a structured query asking "which existing
   facts did this new one supersede?"
2. Parses the returned `superseded_fact_ids` list.
3. For each id, sends `PATCH .../memories/{id}` with `state=invalidated` (soft delete — reversible).
4. Sends `DELETE .../memories/{id}/observations` to clear derived observations.

The reflect LLM sees the freshly retained fact as a clean signal, so it can decide what is stale
without itself being misled by older stale facts.

## When to use this skill

Trigger this skill when the user expresses intent to:

- Clean up, purge, or audit Hindsight memories
- Forget or invalidate a specific outdated fact
- Diagnose why recall keeps returning contradictory information
- React to a code rename, file move, or refactor that has stale facts in memory

Do **not** use this skill for:

- Reading memory (use the regular `recall` tool)
- Storing memory (use `retain` — memorial is *post-retain* cleanup, not retain itself)
- Hard-deleting entire documents or banks (memorial only soft-deletes by id)

## Practical guidance

Memorial runs as a hook on every retain. The LLM does not need to invoke it manually. If the user
asks "what did memorial do after I retained X?", surface the JSON summary that the hook prints to
stdout.

If the user reports that memorial is over-aggressive (it is invalidating facts the user wanted to
keep), the fix is `PATCH .../memories/{id}` with `state=valid` to restore, plus turning the hook
off in the client config until the user can diagnose why the reflect prompt is too broad.

If the user reports memorial is missing obvious pollution, suggest tightening the new fact text in
their `retain` call — the reflect LLM only sees what was retained, so a vague retain produces a
vague supersession decision.

## Available scripts

- `scripts/retain_reflect_curate.py` — main entry point; called by every hook
- `scripts/lib/client.py` — stdlib HTTP wrapper for the four endpoints memorial uses
- `scripts/lib/reflect_query.py` — builds & parses the structured supersession query
- `scripts/lib/curate.py` — soft-delete + observation-clear pair

All scripts are stdlib-only — no `pip install` is required to run them.

## Configuration

Memorial reads from environment:

- `HINDSIGHT_API_URL` (required) — base URL of the Hindsight server
- `HINDSIGHT_API_KEY` (required for cloud; unset for local)
- `HINDSIGHT_BANK_ID` (required unless provided in stdin payload)

## Limitations

- Memorial cannot detect pollution that is *not* surfaced by a new retain. A fact that became stale
  long ago with no superseding event will sit in the bank forever.
- Reflect failures are non-fatal: memorial logs and exits 0 so a transient reflect outage cannot
  block the calling agent.
- Memorial does not currently support `dry_run` from a hook context — pass `--dry-run` manually to
  preview what would be invalidated.