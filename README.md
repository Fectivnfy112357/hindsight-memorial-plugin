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

## Deploy

The receiver runs as a standalone Docker container alongside the Hindsight
server. It does not modify the Hindsight deployment in any way — they are
two independent projects that share one docker network.

### Project layout

```
hindsight-memorial/
├── Dockerfile                       ← image build (python:3.13-slim + stdlib only)
├── docker-compose.yml               ← service definition, joins hindsight_default network
├── .env.example                     ← copy to .env on the host
├── app/                             ← Python source tree, bind-mounted
│   └── hindsight_memorial/
├── data/logs/                       ← bind-mounted log directory
│   └── hindsight-memorial.log       ← RotatingFileHandler, 10 MB × 5 backups
├── tests/
├── README.md
└── SKILL.md
```

The Hindsight project lives in a separate sibling directory (typically
`/www/dk_project/dk_app/hindsight/`) and is **never touched by this repo**.

### 1. Place the source on the deployment host

```bash
# From your dev machine:
tar czf /tmp/hindsight-memorial.tar.gz \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='.idea' .
scp /tmp/hindsight-memorial.tar.gz user@host:/www/dk_project/dk_app/

ssh user@host
cd /www/dk_project/dk_app
mkdir -p hindsight-memorial/app hindsight-memorial/data/logs
tar xzf hindsight-memorial.tar.gz -C hindsight-memorial/app --strip-components=0
```

### 2. Configure secrets

```bash
cd /www/dk_project/dk_app/hindsight-memorial
cp .env.example .env
# Edit .env — set HINDSIGHT_API_KEY to the same value the Hindsight container uses
# (see /www/dk_project/dk_app/hindsight/.env) and set HINDSIGHT_WEBHOOK_SECRET to
# the output of: openssl rand -hex 32
```

### 3. Build and start

```bash
docker compose up -d --build
docker compose logs -f memorial
# → "hindsight-memorial webhook server listening on 0.0.0.0:9602"
```

### 4. Verify

```bash
docker compose ps              # memorial should be (healthy)
docker compose logs --tail 50  # check for "listening on" + no error lines
```

### 5. Wire up the webhook in Hindsight

In the Hindsight webhooks UI:

- **URL**: `http://memorial:9602/webhook/hindsight` (the docker network hostname,
  NOT `localhost` or the host IP — the Hindsight container reaches the memorial
  container over the shared `hindsight_default` network)
- **Method**: POST
- **Signing secret**: paste the same value you put in `HINDSIGHT_WEBHOOK_SECRET`
- **Event type**: `retain.completed`
- **Enabled**: true

Trigger a retain and watch the log:

```bash
tail -f data/logs/hindsight-memorial.log
# Expect lines like:
#   webhook received: bytes=412 event_header='retain.completed' sig_present=True
#   event parsed: bank=... document=... memory_unit_count=1 operation=...
#   unit 1/1 reconciling bank=... document=... text_preview='...'
#   unit 1/1 result=ok superseded=2 observations_cleared=4 error=None
#   webhook processed: status=ok bank=... document=... units=1 superseded=2
```

### Network configuration

The compose file declares an **external** network:

```yaml
networks:
  hindsight_net:
    external: true
    name: hindsight_default
```

This joins the existing network that the Hindsight project created (docker
compose names networks `<project>_default`). The memorial service can resolve
`hindsight` because both containers share this network. **No host port is
exposed** — the receiver is only reachable from inside the docker network.

If the Hindsight project was deployed under a different compose project name,
update `name:` above to match `<that-name>_default`.

### Upgrading

```bash
cd /www/dk_project/dk_app/hindsight-memorial
# Re-upload the new source to ./app/ (via tar+scp as in step 1)
docker compose restart memorial   # no rebuild needed (source is bind-mounted)
```

`docker compose restart` re-execs the process without rebuilding the image.
Image rebuilds are only needed when `Dockerfile` itself changes.

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
├── Dockerfile                       ← image build (python:3.13-slim)
├── docker-compose.yml               ← joins hindsight_default docker network
├── .env.example                     ← deployment secret template (copy → .env)
├── .gitignore
├── app/                             ← Python source tree (bind-mounted into container)
│   └── hindsight_memorial/
│       ├── __init__.py              ← public API surface
│       ├── client.py                ← stdlib HTTP client (Hindsight API)
│       ├── config.py                ← MemorialConfig dataclass + env loading
│       ├── curate.py                ← soft-delete + observation-clear
│       ├── reconcile.py             ← single-attempt reflect + curate pipeline
│       ├── reflect_query.py         ← structured supersession query builder
│       ├── webhook_handlers.py      ← verify_signature + parse_event + handle_event
│       └── webhook_server.py        ← `python -m` HTTP entrypoint
├── data/                            ← bind-mounted log directory (gitignored)
│   └── logs/
│       └── hindsight-memorial.log   ← RotatingFileHandler output
├── tests/                           ← 57 unit tests (stdlib only)
│   ├── test_client.py
│   ├── test_config.py
│   ├── test_curate.py
│   ├── test_reconcile.py
│   ├── test_reflect_query.py
│   └── test_webhook_handlers.py
└── SKILL.md, README.md, pyproject.toml, conftest.py
```

## License

MIT.