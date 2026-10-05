# odoo-miner

Turns a Chrome DevTools Recorder session of someone working in Odoo into
structured data the analysis agents can reason about: every step, the element
it touched, the screen and record it happened on, and the exact backend calls
it triggered.

Building the analysis agents on top of this? Start with `AGENTS.md`, then
`docs/AGENT_BUILD_GUIDE.md`.

```
recording.json ──ingest──▶ clicks.json ─┐
       │                                ├─merge──▶ session.json ──▶ segmenter_agent → tracer_agent → …
       └───────replay────▶ network.json ┘
```

## Setup

### 1. Odoo 18 in Docker

`docker-compose.yml` runs Odoo 18 Community and PostgreSQL 16. The CLI and the
replay run on your machine and talk to Odoo at `http://localhost:8069`.

```bash
./scripts/init_db.sh          # creates "demo" with Purchase, Inventory, Invoicing (admin / admin)
python3 scripts/seed.py        # adds the purchasing scenarios below
./scripts/snapshot_db.sh      # saves "demo_snapshot"; replays reset to this
```

The seed creates five confirmed purchase orders, receives the goods, and
leaves a draft vendor bill on each for a buyer to work through:

| Scenario | Ordered | Received | Billed | PO price | Bill price |
|---|---|---|---|---|---|
| clean | 1000 | 1000 | 1000 | 2.00 | 2.00 |
| price_variance | 500 | 500 | 500 | 4.10 | 4.35 |
| partial_receipt | 1000 | 600 (rest on backorder) | 1000 | 0.85 | 0.85 |
| rejected_goods | 200 | 180 (20 failed inspection) | 200 | 12.50 | 12.50 |
| extra_charges | 300 | 300 | 300 + $145 expedite freight | 3.20 | 3.20 |

Re-running the seed skips scenarios that already exist.

`./scripts/restore_db.sh` resets `demo` to the snapshot: it stops Odoo,
recreates the database from the snapshot, copies the attachments folder back
and starts Odoo again. Pass it as `--pre-hook` so every replay starts from the
same data. Custom modules go in `addons/`, which is mounted into the container.

For the agents that read Odoo's code, clone the matching source next to the
project: `git clone --depth 1 -b 18.0 https://github.com/odoo/odoo`.

### 2. The CLI and replay

```bash
uv sync --extra dev          # or: python -m venv .venv && pip install -e '.[dev]'

# Replay needs Node 18+ and downloads Chrome on first install
cd replay && npm install && cd ..
```

Python 3.11 or newer. Run commands with `uv run odoo-miner ...`, or activate
the environment with `source .venv/bin/activate`.

## Usage

1. In Chrome, open DevTools → **Recorder**, record the workflow in Odoo, and
   export it as **JSON**.
2. Run the pipeline:

```bash
odoo-miner run recording.json -d out/ \
  --cookie "session_id=<your Odoo session cookie>" \
  --pre-hook "./scripts/restore_db.sh"
odoo-miner show out/session.json
```

Or run the stages separately:

```bash
odoo-miner ingest recording.json -o clicks.json
odoo-miner replay recording.json -o network.json --cookie "session_id=…"
odoo-miner merge clicks.json network.json -o session.json
```

`--skip-replay` on `run` (or `merge` without a network file) gives you clicks
only, with no backend calls.

## What each file contains

| File | Model | Contents |
|---|---|---|
| `clicks.json` | `ClickLog` | Each step's type, target (aria label, visible text, `name` attribute, CSS), typed value, the page URL it happened on, and Odoo context parsed from that URL (model, record id, action, view type). |
| `network.json` | `NetworkLog` | Every Odoo RPC fired during replay (`call_kw`, `call_button`, `action/load`, `/json/2`), with model, method, args, status and the recording step that was running. |
| `session.json` | `Session` | Clicks with their backend calls attached, each call tagged `read` / `write` / `compute` / `action_load` / `unknown`, plus `has_write` per step. |

Schemas live in `src/odoo_miner/models.py`; every stage reads and writes these,
so a stage can be inspected or re-run on its own.

## Analysis stages

On top of `session.json`, a pipeline of agents finds the friction and plans a
fix. Each stage reads one file and writes one file, has a CLI subcommand, and
is a node in the same LangGraph pipeline. See `docs/AGENT_BUILD_GUIDE.md`.

```bash
odoo-miner segment out/run/session.json -o out/run/segments.json
odoo-miner trace   out/run/segments.json --session out/run/session.json -o out/run/traces.json
odoo-miner assess  out/run/segments.json --session out/run/session.json -o out/run/assessment.json
odoo-miner plan    out/run
```

Or run them as one resumable, traced pipeline:

```bash
odoo-miner analyze out/run                      # the whole thing
odoo-miner analyze out/run --until assess       # stop before planning
odoo-miner analyze out/run --approve --thread run   # answer the approval gate
```

| File | Model | Contents |
|---|---|---|
| `segments.json` | `SegmentLog` | Steps grouped into one-thing-each segments, each labelled in business language, with the record it touched and whether it completed, failed or recovered. |
| `traces.json` | `TraceLog` | Per segment: retrieval / action / navigation / mixed, the Odoo methods that ran (`file:line`, override chain included), the data looked up, and a plain-language explanation. |
| `assessment.json` | `Assessment` | A 1-5 difficulty score per step from signals detected in code (typing, lookup, tab switch, screen change, modal, error, backtrack, hidden field, wasted click), plus effort and friction per segment. |
| `plan.json` / `plan.md` | `Plan` | Whether to change anything - no change, data fix, configuration or a new module - with the views and methods to extend, what it saves, acceptance criteria and risks. |
| `build.json` | `BuildResult` | The branch and commit, whether tests passed, whether the replay completed, and effort before against after. |

These stages call a model, so they need `ANTHROPIC_API_KEY` in `.env` (copy
`.env.example`). Two exceptions run offline: `assess --offline` produces the
deterministic scores with no model call, and `python evals/run_evals.py check`
scores the evaluators themselves. `pytest` never needs a key.

The agents read Odoo's source to explain what the backend did, so clone it
next to the project:

```bash
git clone --depth 1 -b 18.0 https://github.com/odoo/odoo vendor/odoo
```

Without it the stages still run; they just cannot resolve code references.

## Things to know

- **Replay repeats every write.** If the recording posts a bill, so does the
  replay. Restore the database before each run with `--pre-hook` (for Odoo in
  Docker, a `pg_restore` or Odoo's database duplicate/restore). The replay
  aborts if the hook fails.
- **Login.** Replay starts a fresh browser. Pass your session cookie with
  `--cookie`, or include the login steps in the recording.
- **No human timing.** The Recorder format stores no timestamps, and replay
  timestamps reflect the replay, not the person. Difficulty is scored from
  structure, not time.
- **Which element gets clicked.** The Recorder saves several alternative
  selectors per step and the replay library normally clicks whichever matches
  first. On Odoo that can pick the wrong element (for example `a.focus`, the
  menu item that happened to be highlighted while recording). The replay
  instead waits for an alternative that matches exactly one visible element,
  preferring ids, then accessible names, then visible text, then CSS, then
  XPath, and ignores selectors based on momentary state (`.focus`, `.active`,
  `.show`). `network.json` lists the selector used for each step under
  `selectors_used`.
- **Late requests.** After each step the replay waits until no Odoo backend
  call has been in flight for `--settle` ms (default 500), so onchange/autosave
  calls land on the step that caused them. Only backend calls count, because
  Odoo keeps other connections open permanently.
- **Debugging a failed replay.** On failure the replay saves
  `network.failure.png` next to `network.json` showing the page where it
  stopped. Add `--screenshots` to `run` to save a screenshot after every step.
- **New-tab steps are skipped.** Recordings started from a new tab begin with
  `chrome://newtab`, which a fresh browser can't open; both ingest and replay
  skip browser-internal pages and keep the original step numbers.
- **Call kinds are hints.** The read/write classification uses known Odoo
  method names and `action_`/`button_` prefixes. Custom methods come out as
  `unknown` for `tracer_agent` to resolve against the backend code.
- **URL parsing.** Both the legacy `/web#model=…&id=…` style and the Odoo 18
  `/odoo/…` path style are handled. Odoo 18 paths encode the breadcrumb trail
  (`/odoo/action-245/1042/action-388/77`); the parser follows Odoo's own router
  rules and reports the last screen, with the full trail kept in `path_slugs`.

## Tests

```bash
pytest
```

The fixture in `tests/fixtures/` is a hand-written vendor-bill workflow
(open bill → check PO → check receipt → fix price → confirm). Replace it with
real recordings as you capture them.
