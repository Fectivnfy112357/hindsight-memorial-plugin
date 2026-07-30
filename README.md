# hindsight-memorial

> Webhook-driven pollution cleanup for [Hindsight](https://hindsight.vectorize.io) memories.
> Receives `retain.completed` events from the Hindsight server and runs a reflect LLM call per
> retained memory_unit, then soft-deletes any facts it has superseded.

## Why

Hindsight's server-side `consolidate` can correct some pollution, but it can't eliminate the root
cause: when a **world fact goes stale** (a method renamed, a file moved, a submodule restructured,
a port changed, a CLI rewritten in a new language), the observations and mental models synthesised
from it inherit the staleness, and a future recall returns contradictory facts that pollute the
agent's context.

`hindsight-memorial` is a small standalone toolkit — no third-party Python dependencies, no edits
to the Hindsight monorepo — that runs as a **webhook receiver** the Hindsight server POSTs to
after every retain. For each retained memory_unit it asks a reflect LLM call which existing
facts the freshly-retained one has superseded, then soft-deletes those facts and clears their
derived observations.

```
retain new fact
       │
       ▼
Hindsight server commits and indexes
       │
       ▼
POST /webhook/hindsight  (HMAC-SHA256 signed)
       │
       ▼
GET /memories/list?document_id=...     ── pull each memory_unit for this event
       │
       ▼
for each unit:
   reflect("which old facts did this new unit supersede?")
       │
       ▼
   superseded_fact_ids[]
       │
       ├─► PATCH memory {id}     state=invalidated
       └─► DELETE /memories/{id}/observations
```

The reflect call is made while the *new* fact is fresh in context, so the LLM has a clean signal
to reason from. Once a fact is `state=invalidated` it disappears from `recall` results but remains
recoverable (`PATCH state=valid` restores it).

Each memory_unit is reconciled independently — facts within the same document are not necessarily
mutually consistent, so we deliberately avoid fusing them into a single reflect query.

## Why webhook, not hooks

Previous versions of this project shipped as Claude Code / Hermes / Codex hooks that intercepted
retain tool calls. Two unavoidable gaps made that approach unreliable:

1. **Tool-name matchers don't cover all retain paths.** Hindsight's automatic retain (HTTP direct
   POST + the Stop hook that forces a final write at session end) bypasses any PostToolUse hook.
   Only actively-invoked `agent_knowledge_ingest` MCP calls were intercepted.
2. **Hooks fire before the index catches up.** A 30-second initial wait plus two 30-second
   retries were needed before reflect could see the new fact — and even then, the race was not
   fully closed.

The webhook path fixes both: the event fires only after Hindsight commits the new fact, and every
retain path (manual MCP, HTTP direct, Stop-hook forced write) goes through the same
`retain.completed` event.

## Install

### 1. Deploy the receiver alongside your Hindsight server

This project is a Python package with stdlib-only dependencies. Run it as a long-lived process:

```bash
git clone https://github.com/Fectivnfy112357/hindsight-memorial-plugin.git
cd hindsight-memorial-plugin
python -m hindsight_memorial.webhook_server \
    --host 0.0.0.0 \
    --port 9601 \
    --secret '<same-secret-as-configured-in-hindsight-webhooks-ui>'
```

The receiver listens on `:9601/webhook/hindsight` and `:9601/healthz`.

### 2. Configure the webhook in Hindsight

In the Hindsight UI, add a webhook pointing at the receiver:

- **URL**: `http://<your-host>:9601/webhook/hindsight`
- **Method**: POST
- **Signing secret**: any value; paste the same value into `--secret` above
- **Event type**: `retain.completed`
- **Enabled**: true

Verify it works:

```bash
curl http://localhost:9601/healthz
# → ok
```

### 3. Configure the receiver

Memorial reads connection settings from environment:

```bash
export HINDSIGHT_API_URL=http://your-hindsight-host:9600
export HINDSIGHT_API_KEY=your-token        # optional for local
export HINDSIGHT_WEBHOOK_SECRET=the-secret # must match what Hindsight signed with
```

The bank id is taken from each webhook event's `bank_id` field — there is no project/cwd mapping
in this mode. A single receiver instance handles every bank on the server.

### Running in the same container as Hindsight

If Hindsight and this receiver share a container (recommended), point `HINDSIGHT_API_URL` at
`http://localhost:9600` (or whatever Hindsight binds internally) and start the receiver as a
second process inside the same container — e.g. as a second command in your Dockerfile /
compose file, or supervised by the same init system.

## Tests

```bash
python -m pytest tests/
# expected: 57 passed in 0.1Xs
```

Tests mock the HTTP layer, so no real Hindsight server is required.

## What's *not* in scope

- **Hard-deleting facts.** Memorial only soft-deletes (`state=invalidated`). Reversible.
- **Scheduled background scanning.** Memorial reacts to retain events only.
- **Modifying the Hindsight monorepo.** Memorial is fully standalone and only uses the public
  HTTP API.

## Design notes

- The reflect LLM is asked about *supersession*, not general cleanup, to keep false positives low.
  Failures are isolated per-id in `curate_many`, so one bad id doesn't abort the rest.
- Reflect failures are surfaced as `status="reflect_failed"` and acknowledged with HTTP 200.
  Hindsight's outbox has a 5s/5min/30min/2h/5h retry policy; a webhook receiver that returns 5xx
  would trigger that storm. Returning 200 with a structured failure in the body keeps the outbox
  quiescent.
- Reversibility: `PATCH /v1/default/banks/{bank_id}/memories/{id} {state: "valid"}` restores an
  invalidated fact. The `reason` field set by memorial (visible via `GET /memories/{id}`) makes
  it auditable why each fact was invalidated.
- Per-unit reconcile (rather than fusing all units of a document into one reflect query) is
  deliberate: facts in the same document can be mutually contradictory, and the reflect LLM
  performs better when asked about one new fact at a time.

## Project layout

```
hindsight-memorial/
├── hindsight_memorial/                 ← Python package (stdlib-only)
│   ├── __init__.py                     ← public API surface
│   ├── client.py                       ← stdlib HTTP client (Hindsight API)
│   ├── config.py                       ← MemorialConfig dataclass + env loading
│   ├── curate.py                       ← soft-delete + observation-clear
│   ├── reconcile.py                    ← single-attempt reflect + curate pipeline
│   ├── reflect_query.py                ← structured supersession query builder
│   ├── webhook_handlers.py             ← verify_signature + parse_event + handle_event
│   └── webhook_server.py               ← `python -m` HTTP entrypoint
├── tests/                              ← 57 unit tests (stdlib only)
│   ├── test_client.py
│   ├── test_config.py
│   ├── test_curate.py
│   ├── test_reconcile.py
│   ├── test_reflect_query.py
│   └── test_webhook_handlers.py
├── SKILL.md, README.md, pyproject.toml, conftest.py
```

## License

MIT.