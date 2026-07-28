# hindsight-memorial

> Client-side pollution cleanup for [Hindsight](https://hindsight.vectorize.io) memories.
> Runs a reflect LLM call after every `retain` and soft-deletes the facts it has superseded.

## Why

Hindsight's server-side `consolidate` can correct some pollution, but it can't eliminate the root
cause: when a **world fact goes stale** (a method renamed, a file moved, a submodule restructured,
a port changed, a CLI rewritten in a new language), the observations and mental models synthesised
from it inherit the staleness, and a future recall returns contradictory facts that pollute the
agent's context.

`hindsight-memorial` is a small standalone toolkit — no third-party Python dependencies, no edits
to the Hindsight monorepo — that runs **after each `retain`** on the client side and asks a
reflect LLM call which existing facts the freshly-retained one has superseded, then soft-deletes
those facts and clears their derived observations.

```
retain new fact ──► hook intercepts ──► reflect("which old facts did this new one supersede?")
                                              │
                                              ▼
                                    superseded_fact_ids[]
                                              │
                  ┌───────────────────────────┼───────────────────────────┐
                  ▼                           ▼                           ▼
           PATCH memory {id}      DELETE /memories/{id}/observations    log JSON
           state=invalidated       (clear derived observations)
```

The reflect call is made while the *new* fact is fresh in context, so the LLM has a clean signal
to reason from. Once a fact is `state=invalidated` it disappears from `recall` results but remains
recoverable (`PATCH state=valid` restores it).

## Client support

| Client       | Status             | Hook file                  | Tool name the hook matches           |
|--------------|--------------------|----------------------------|---------------------------------------|
| Claude Code  | **End-to-end verified** | [`hooks/hooks.json`](hooks/hooks.json) | `mcp__plugin_hindsight-memory_hindsight__agent_knowledge_ingest` |
| Codex CLI    | Template only — not yet tested on a live Codex | [`hooks/codex.json`](hooks/codex.json) | `^hindsight_retain$` (regex; pending real-name confirmation) |
| Hermes Agent | Template only — not yet tested on a live Hermes | [`hooks/hermes.yaml`](hooks/hermes.yaml) | `^hindsight_retain$` (shell hook; Hermes also supports plugin distribution, not yet packaged) |

## Install

This project is distributed as a **Claude Code plugin on GitHub**. Anyone can install it without
cloning anything first.

**Prerequisite**: Claude Code CLI ≥ 1.0 with plugin support.

```bash
# 1. Register this repo as a marketplace (one-time, per machine)
/plugin marketplace add Fectivnfy112357/hindsight-memorial-plugin

# 2. Install the plugin
/plugin install hindsight-memorial@hindsight-memorial

# 3. Reload so the new matcher is picked up (or restart Claude Code)
/reload-plugins
```

Verify it loaded:

```bash
cat ~/.claude/settings.json | python -c "import sys,json; print(json.load(sys.stdin).get('enabledPlugins'))"
# should contain: {'hindsight-memorial@hindsight-memorial': True, ...}
```

### Local install (development)

If you're hacking on the plugin itself and want Claude Code to pick up your local edits:

```bash
claude plugin marketplace add "D:/programming/projects/hindsight-memorial" --scope user
claude plugin install hindsight-memorial@hindsight-memorial --scope user
/reload-plugins
```

Then edit files under the project directory and re-run `/reload-plugins` to see changes.

### Configure

Memorial reads connection settings from `~/.hindsight/claude-code.json` (the same file the official
Hindsight plugin writes), or from environment variables (which take precedence):

```bash
# Either: edit ~/.hindsight/claude-code.json
{
  "hindsightApiUrl": "http://your-hindsight-host:9600",
  "hindsightApiToken": "your-token",
  "directoryBankMap": {
    "D:/path/to/project-a": "project-a-bank",
    "D:/path/to/project-b": "project-b-bank"
  }
}

# Or: export env vars (override the file)
export HINDSIGHT_API_URL=http://your-hindsight-host:9600
export HINDSIGHT_API_KEY=your-token
export HINDSIGHT_BANK_ID=hindsight
```

**Bank resolution order** (strict, per directory):

1. `directoryBankMap[<cwd>]` — exact match after path normalisation
2. `basename(<cwd>)` — e.g. cwd `D:/code/foo` → bank `foo`
3. *(give up)* — never auto-create a bank on the server

The hook will silently no-op if the resolved bank does not exist on the server.

### Manual invocation (for testing without a hook)

```bash
python scripts/retain_reflect_curate.py \
  --cwd "D:/code/your-project" \
  --bank-id your-bank \
  --new-fact "The auth module moved from src/auth/ to src/security/auth/."
```

With `--dry-run`, it skips the PATCH/DELETE calls and only prints the query the reflect LLM would
have seen:

```bash
python scripts/retain_reflect_curate.py --cwd . --bank-id test --new-fact "..." --dry-run
```

### Hook into Codex / Hermes (when you're ready)

The hook configs in [`hooks/`](hooks/) are templates. They are **not** wired up automatically;
they're provided so you can copy the relevant one to the correct location for your client:

- Codex: drop into `~/.codex/hooks.json` (and ensure Codex CLI ≥ v0.124 for stable hook support)
- Hermes: append the `hooks:` block to `~/.hermes/config.yaml`

Tool names on those clients are different from Claude Code's MCP name — see the table at the top
of this README. Verify the exact tool name in your client's tool list before activating, and update
the `matcher` field accordingly.

## Tests

```bash
cd hindsight-memorial
python -m unittest discover -s tests
# expected: Ran 34 tests in 0.0Xs — OK
```

Tests mock the HTTP layer (`urllib.request.urlopen`), so no real Hindsight server is required.

## What's *not* in scope

- **Hard-deleting facts.** Memorial only soft-deletes (`state=invalidated`). Reversible.
- **Scheduled background scanning.** Memorial runs synchronously at retain time.
- **Cross-client deduplication.** Each client invokes its own hook; nothing coordinates across them.
- **Modifying the Hindsight monorepo.** Memorial is fully standalone and only uses the public
  HTTP API.

## Design notes

- The reflect LLM is asked about *supersession*, not general cleanup, to keep false positives low.
  Failures are isolated per-id in `curate_many`, so one bad id doesn't abort the rest.
- The hook is non-fatal by design: any failure (reflect / patch / delete / network) is logged and
  exit code is always 0, so memorial never blocks the calling agent.
- Reversibility: `PATCH /v1/default/banks/{bank_id}/memories/{id} {state: "valid"}` restores an
  invalidated fact. The `reason` field set by memorial (visible via `GET /memories/{id}`) makes it
  auditable why each fact was invalidated.
- Hook payload parsing accepts four shapes (top-level fields, nested `tool_input.*`, `memory.*`,
  and Codex-style `tool_input.command="hindsight retain '...'"`). Adding a new client shape is a
  3-line addition in `scripts/retain_reflect_curate.py:_extract_new_fact`.

## Project layout

```
hindsight-memorial/
├── .claude-plugin/
│   ├── plugin.json          ← Claude Code plugin manifest
│   └── marketplace.json     ← self-host marketplace (so `claude plugin marketplace add <path>` works)
├── hooks/
│   ├── hooks.json           ← ACTIVE: Claude Code PostToolUse hook
│   ├── codex.json           ← template: Codex CLI hook config
│   └── hermes.yaml          ← template: Hermes shell-hook config
├── scripts/
│   ├── lib/{client,config,curate,reflect_query}.py
│   └── retain_reflect_curate.py        ← main entry point (run by hooks)
├── skills/hindsight-memorial/SKILL.md  ← plugin skill (auto-loaded into Claude Code sessions)
├── tests/                              ← 34 unit tests (stdlib only)
├── SKILL.md, README.md, pyproject.toml, .gitignore
```

## License

MIT.