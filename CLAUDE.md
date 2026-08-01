# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

`hindsight-memorial` is a standalone Python webhook receiver for Hindsight. It handles
`retain.completed` events: ingests the freshly retained `memory_unit` records into a local
MySQL/SQLite table (`memory_units`), and a background poller thread asynchronously
asks Hindsight's `reflect` endpoint which existing facts were superseded, soft-invalidates
those facts, and clears their derived observations.

This repository is separate from the Hindsight monorepo. It only calls Hindsight's public HTTP API;
it does not modify Hindsight's source or access its database, queue, or LLM provider directly.

The 2026-08-01 redesign replaces the previous "admission queue + single background worker"
architecture with a persistent state machine: the unit-level dedup that previously lived in
the dispatcher's in-memory body-hash table is now the database's `UNIQUE (bank_id, unit_id)`
key. See `doc/persistent-reconciler-design-2026-08-01.md` for the full design.

## Development commands

The HTTP layer is stdlib-only (urllib, http.server). The persistence layer adds one
runtime dependency, `PyMySQL`, for production MySQL; SQLite is used in tests and the
local "ingest-only" mode. Python 3.10 or newer is required; Docker uses Python 3.13.

### Install locally

```bash
python -m pip install .
```

This installs the package and the `hindsight-memorial-webhook` console-script entry point. For a
checkout-only test run, installation is not required because the package is at the repository root.

### Run tests

Tests use `unittest` and `unittest.mock`; they do not require a live Hindsight server or a
live MySQL — the in-process DB layer is in-memory SQLite. The documented pytest command
requires `pytest` to be installed separately because it is not a project dependency:

```bash
python -m pytest tests/
python -m pytest tests/test_poller.py
python -m pytest tests/test_poller.py::LifecycleTest::test_restart_drains_pending_rows_left_by_previous_run
```

The same tests can be run without pytest using the standard library:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Use the pytest selector above when you need one test method; the `tests/` directory is not a Python
package, so dotted `unittest` selectors such as `tests.test_poller...` are not supported here.

### Run the webhook receiver directly

Set `HINDSIGHT_API_URL`, `HINDSIGHT_API_KEY` (when required by the Hindsight deployment),
`HINDSIGHT_WEBHOOK_SECRET`, and optionally `HINDSIGHT_MYSQL_HOST` (omit to use in-memory
SQLite), then run:

```bash
python -m hindsight_memorial.webhook_server --host 0.0.0.0 --port 9602
```

The secret can alternatively be supplied with `--secret`. The installed console-script equivalent
is:

```bash
hindsight-memorial-webhook --port 9602 --secret <hex-encoded-shared-secret>
```

### Run with Docker

The Hindsight server must already have created the external `hindsight_default` Docker network.
Configure `.env` from `.env.example`, including the shared API key, webhook secret, and MySQL
password, then run:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f memorial
```

The compose file does not publish a host port. Hindsight reaches the receiver at
`http://memorial:9602/webhook/hindsight`. For local debugging, the documented command is:

```bash
docker compose run --service-ports memorial
```

The container healthcheck calls `/healthz` and expects JSON with `status: "ok"`. Logs are persisted
through `./data/logs`.

### Replay the incident regression

This script starts a real local `ThreadingHTTPServer`, replays the recorded incident payload 5
times (the d1b21d2e delivery that Hindsight sent 5 times over 8 hours), and asserts that the
new architecture collapses 5 deliveries to 1 row in the local table and 1 reflect call, with
HTTP responses under 50 ms:

```bash
python scripts/replay_incident.py
```

There is no configured Makefile, CI workflow, pre-commit configuration, or lint/format/type-check
configuration. Do not assume commands such as `ruff`, `black`, `mypy`, or `flake8` are available;
no project-specific lint command currently exists.

## Architecture

The runtime is a single container with no application-level database (other than the local
`memory_units` table managed by `hindsight_memorial.db` / `db_mysql.py`), broker, or scheduler.
The only persistent outputs are the local `memory_units` table and optional rotating log files.
The process entry point is `hindsight_memorial.webhook_server` (also exposed as the console script).

The request path has two halves that are now decoupled by the database, not by a shared queue:

1. **Fast admission — `webhook_server.py` + `webhook_handlers.py`**
   `ThreadingHTTPServer` receives `POST /webhook/hindsight`, verifies the HMAC-SHA256 signature,
   parses the event, recovers a missing `document_id` when possible (fallback path, see below),
   fetches the event's memory units, and upserts each into the local `memory_units` table. It
   returns 200 immediately; the request thread never calls `reflect` or `curate`.

2. **Slow processing — `poller.py`**
   A single serial background worker (a daemon thread owned by `ReconcilerPoller`) polls the
   local table for `status='pending'` rows, picks the most-recent by `created_at`, calls
   `run_reconcile` against it, and updates the row to a terminal state
   (`processed` / `superseded` / `failed`).

The two halves communicate only through the `memory_units` table; there is no shared
in-memory state between the request thread and the poller thread.

### Per-unit pipeline

- `client.py`: stdlib `urllib` client for Hindsight's banks, memory listing, reflect,
  memory update, and observation-clear endpoints. Every urlopen failure path
  (`HTTPError`, `URLError`, `TimeoutError`, `OSError`) is wrapped as `HindsightAPIError`
  with `status=0` so callers see a single typed exception.
- `reconcile.py`: a single `run_reconcile(bank_id, unit_id, content, *, load_cfg, dry_run=False)`
  attempt. The freshly retained unit's id is always passed to `extract_superseded_ids` as
  `exclude_ids` to defend against the LLM listing the new fact itself as a supersede target
  (defence in depth on top of `exclude_unit_ids`, see commit `a4ac52d`).
- `reflect_query.py`: structured supersession schema/query plus extraction and validation of
  returned memory IDs. Default `structured_only=True` (issue #2 fix): an empty structured
  list means *no* supersede; reasoning text is never scanned for UUIDs unless the caller
  explicitly opts in with `structured_only=False`.
- `curate.py`: independently patches each superseded memory to `state="invalidated"` and
  deletes its observations on the Hindsight side. The new `curate_superseded_in_db`
  function mirrors that onto the local table — superseded local rows are soft-marked
  (`status='superseded'`, `superseded_reason` recorded), not deleted.
- `db.py` / `db_mysql.py`: SQLite (default, in-memory) or MySQL (production, when
  `HINDSIGHT_MYSQL_HOST` is set) backend. Same SQL contract on both. `get_connection()` is
  the seam; production code never imports `sqlite3` or `pymysql` directly.
- `config.py`: local/CLI configuration resolution from environment. The webhook path uses
  `webhook_config_loader` in `webhook_handlers.py` instead: the bank ID comes from the
  event, and the webhook process does not resolve a bank from its working directory.
- `poller.py`: `ReconcilerPoller(conn, run_reconcile, *, poll_interval_sec=1.0)`. The
  daemon thread loop calls `run_once()` until `stop()` is set.

### Load-bearing behavior

- Return HTTP 200 for invalid signatures and admission failures as well as successful
  admission. Returning a non-2xx here causes Hindsight's webhook retry ladder to resend the
  event; replays are now deduplicated at the unit level via the `UNIQUE (bank_id, unit_id)`
  key on the `memory_units` table.
- Mark a unit `processing` before reflect, `processed` (or `superseded` / `failed`) after.
  A second delivery of the same `(bank_id, unit_id)` finds the row already in a terminal
  state and the upsert is a no-op (`skipped`), so reflect runs exactly once per unit.
- Keep the poller single-threaded. Concurrent reflect calls against the same bank change
  the ordering, deduplication, logging, and rate-limiting assumptions. The `processing`
  state plus the `FOR UPDATE`-friendly `LIMIT 1` query are designed for one consumer.
- Call `run_reconcile` with `exclude_ids=[unit_id]` for every newly retained unit (handled
  inside `reconcile.run_reconcile`; callers do not pass it).
- Reconcile units independently rather than combining all units from one document.
- Reflect failures are acknowledged and logged; the row is marked `status='failed'` with
  the short reason in `failure_reason` and the full traceback in the log. There is no
  client-side reflect retry — Hindsight's own webhook delivery is the retry mechanism, and
  the unit-level dedup makes replays safe.
- Curation is soft invalidation (`state="invalidated"` via the existing
  `client.invalidate_memory(unit_id, reason=...)` API) plus observation cleanup, not hard
  deletion of the memory record. The `reason` string is the reflect LLM's reasoning
  (truncated to 200 chars), which Hindsight persists in
  `invalidated_memory_units.invalidation_reason` and we mirror in the local
  `superseded_reason` column for independent audit.

### Event fallback and safety boundary

Some Hindsight events may omit `data.document_id`. `parse_event` preserves this as a recoverable
condition; the handler lists recent units and accepts a recovered document only when its
timestamp falls within the configured 60-second event window. The event's `memory_unit_count`
is only a hint: the handler still fetches the actual unit list.

The local `Dispatcher` (the previous architecture's in-memory queue) is gone. The DB row
table is the only state that survives across requests. Process restarts lose no queued
work because every event has already been durably written to the table by the time the
HTTP request returns 200. A job left `processing` by a process crash will be picked up
by the next poller start (the `pending` → `processing` → terminal cycle is restarted
on the next iteration; if you need explicit timeout-based recovery, add a
`processing_started_at` column — that's a future task, not a current requirement).
`/healthz` reports per-status row counts plus `poller_running`, not the success of the
last reconcile.

## Tests and change boundaries

Tests are organized around the modules and mock both HTTP I/O and the database. When
changing webhook behavior, inspect `tests/test_webhook_handlers_ingest.py` and
`tests/test_webhook_server_new.py` together. `tests/test_repro_mass_invalidate.py` is
the regression coverage for the 2026-07-30 mass-invalidation incident: it asserts that
`handle_event` does not call reflect (the architectural property that makes the
incident unreachable from the webhook path) and that the poller correctly mirrors
supersede verdicts onto the local table.

When changing configuration, distinguish the local/CLI loader from the webhook loader.
When changing reflect or curation, preserve the excluded newly retained ID,
single-attempt reflect behavior, structured-only extraction (the issue #2 fix),
and reversible invalidation semantics. When changing the persistence layer, all SQL
must remain valid on both SQLite (tests) and MySQL (production) — the
`UNIQUE (bank_id, unit_id)`, `KEY (status, created_at DESC)`, and `KEY (status, ingested_at DESC)`
indexes on the `memory_units` table are what make the poller's
"most-recent pending row" query and the upsert's "no-op on content-unchanged" path work.

The old dispatcher code (`hindsight_memorial/dispatch.py`) and its tests
(`tests/test_dispatch.py`) have been removed; the unit-level dedup that used to live
in the dispatcher's body-hash table is now the database's `UNIQUE (bank_id, unit_id)`
key. The legacy `test_webhook_handlers.py`, `test_webhook_server.py`, `test_reconcile.py`
files were also removed; the new test files are
`test_webhook_handlers_ingest.py`, `test_webhook_server_new.py`,
`test_reconcile_new_signature.py`, `test_db.py`, `test_poller.py`, and
`test_reflect_query_structured_only.py`.

There is no Makefile, CI workflow, pre-commit, or lint configuration. The `RuntimeError`
that the 2026-07-31 incident log showed leaking from `client._request` (a raw
`TimeoutError` not wrapped as `HindsightAPIError`) is fixed; the four-branch `except`
chain is pinned by `tests/test_client.py::ErrorPathTest::test_timeout_error_is_wrapped_as_typed_exception`
and `test_connection_reset_error_is_wrapped_as_typed_exception`.

The README contains older project-layout examples showing an `app/` source directory and an
older expected test count. The checked-in source is actually rooted at `hindsight_memorial/`,
and `pyproject.toml` discovers that package from the repository root; treat the current
files and configuration as authoritative.

## Reference

- `doc/persistent-reconciler-design-2026-08-01.md` — design document for the local-table
  architecture, including the schema, state machine, upsert rules, and the rollout plan.
- `doc/webhook-runtime-findings-2026-07-31.md` — the 2026-07-31 incident log that motivated
  the redesign. Issues #1 (cross-webhook duplicate reflect), #2 (empty structured verdict
  falling through to UUID scan), and #3 (single reflect timeout breaking the whole batch)
  are all closed by the new architecture; #4 (large document serialization) and #5
  (`include_based_on` API drift) are addressed by structured concurrency and by removing
  the unsupported field respectively.
