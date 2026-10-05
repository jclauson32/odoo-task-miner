# odoo-miner

[![CI](https://github.com/jclauson32/odoo-task-miner/actions/workflows/ci.yml/badge.svg)](https://github.com/jclauson32/odoo-task-miner/actions/workflows/ci.yml)

Records how a person actually works in Odoo, replays it to capture the backend
call behind every click, and runs a pipeline of agents over the result: group
the clicks into tasks, explain each task against the Odoo source code that ran,
score where the friction is, and decide - with a person approving - whether the
workflow is worth changing.

The agents follow one rule throughout: **compute what can be computed, and let
the model judge only what cannot.** Replay, call classification, difficulty
signals and citation checks are code; naming tasks, explaining code and
weighing a change are the model's job. Every model output is validated, and
nothing leaves the machine without a person approving it.

New here? [`docs/RUNBOOK.md`](docs/RUNBOOK.md) walks through the reference
scenario end to end. Working on the agents? Start with [`AGENTS.md`](AGENTS.md),
then [`docs/AGENT_BUILD_GUIDE.md`](docs/AGENT_BUILD_GUIDE.md).

## What it found on the reference recording

`tests/fixtures/rfq_to_payment.json` is a buyer creating an RFQ for two
catheter components, fixing two product prices, confirming, receiving, billing
and paying - and hitting Odoo's "bill date required" error on the way.

| Stage | Built with | Result | Cost |
|---|---|---|---|
| replay | Puppeteer | 54 steps, 57 backend calls in about 50 s; a rerun reproduced every `(kind, model, method)` on every step | none |
| segment | LangChain `create_agent` | 11-12 tasks in business language; boundary F1 0.78 and 0.75 against hand labels in two runs; found the failed confirm and the recovery | ~$0.09 |
| trace | resolver in code + agent | every code reference exists (27/27, 23/23); confirm → `purchase_order.py:538`, post → `account_move.py:5558`, override chains included | ~$0.25 |
| assess | 9 signals in code + agent | total effort 95; identical scores across runs; the bill error and its dialog flagged; model moves a score at most ±1 | ~$0.08 |
| plan | Deep Agent | **Refused to default the bill date in all five runs** - a deliberate audit control the obvious fix would weaken. On the price detour, a borderline call, the runs split: no change ×3 (the price is inline-editable in Purchase › Products), fix the catalog data ×1, a small view change ×1. Citations are checked mechanically; one run had one misfiled, flagged before approval | $0.31-$6.65 |

Costs are measured on Claude Sonnet 5 with LangSmith; the tracer figure is
with prompt caching (82% of its input served from cache). The planner decides
how much of Odoo's source to read, so its cost varies most: 11 model calls in
one run, 69 in another. A whole analysis has cost between $0.66 and about $7. Because the planner's
decision on borderline friction varies between runs, it stops for a person.

## When a change is worth building

`recordings/bill-exception-review.json` is a buyer resolving two seeded
three-way-match exceptions: a bill priced above its purchase order (4.35
against 4.10) and a bill for 1,000 units when 600 were received. For each,
they open the purchase order and its receipts just to read numbers, come
back, correct the line and post.

The planner chose to **customize** - nothing in Odoo Community shows the PO's
quantity, received quantity or price on a bill line (the "Purchase Matching"
button hides once a line is matched; 3-way matching is an Enterprise
upgrade) - and the builder delivered `purchase_bill_match_columns`, three
read-only columns on the bill lines:

| Check | Result |
|---|---|
| Module tests (Odoo's own result line) | 3 of 3 pass |
| Workflow replayed with the module installed | both bills posted with the same corrected values |
| Steps / effort, scored the same way before and after | 20 → 14 steps, 34 → 22 effort (**−35%**) |
| Review | the first push was rejected (the columns leaked onto customer invoices); the builder fixed it, reran its tests, and asked again |
| Delivery | branch `feat/purchase_bill_match_columns`, [pull request #1](https://github.com/jclauson32/odoo-task-miner/pull/1), report email with before/after screenshots - each approved at its gate, each in `out/audit.jsonl` |

## When a written rule decides the answer

`recordings/bill-exception-resolution.json` is a scripted AP clerk clearing
all four seeded exceptions: a price variance, a partial receipt, rejected
goods, and a freight charge on no PO. For each bill they open the PO and its
receipts, come back, type a note quoting the numbers, correct the line and
confirm - 47 steps, and the four notes they type are the company's payables
policy, applied by hand.

The planner first proposed showing the PO's numbers on the bill. A reviewer
sent the plan back with the policy - *record a bill at PO terms, remove a
charge with no PO line and raise it with the vendor, explain every deviation
in the chatter* - and asked for it as one action the clerk takes, with
Confirm left to them. The revised plan worked out how:
`purchase_bill_apply_po_terms`, an **Apply PO Terms** button on draft vendor
bills, with the edge cases and risks named. It read Odoo's source to get the
quantity right: a draft bill counts itself in `qty_to_invoice`, so copying
that field would bill −400 of the 600 connectors received.

| Check | Result |
|---|---|
| Plan | sent back once with the policy; then approved with conditions the builder followed (refuse POs in another currency, ask before removing lines, convert units, escape the chatter note) |
| Module tests | 10 of 10 pass: one per acceptance criterion, plus the refusals |
| Review | the first push was rejected: a PO for 2 dozen billed as 20 units came out 23.96 instead of 24, and the builder had changed the test rather than the code. Fixed in the code, test restored, approved on the second attempt |
| Workflow replayed with the module installed | all four bills posted at PO terms (4.10, 600, 180, freight removed), each with one note listing old → new values - checked in the database |
| Steps / effort, scored the same way before and after | 47 → 24 steps, 77 → 32 effort (**−58%**) |
| Delivery | branch `feat/purchase_bill_apply_po_terms`, [pull request #2](https://github.com/jclauson32/odoo-task-miner/pull/2), report email with before/after screenshots - every decision in `out/audit.jsonl` |

With the module in `addons/` (it is in pull request #2), see it in Odoo:

```bash
./scripts/restore_db.sh && ./scripts/install_module.sh purchase_bill_apply_po_terms
./scripts/test_module.sh purchase_bill_apply_po_terms     # its 10 tests, about 15 s
```

then open a draft bill at http://localhost:8069/odoo/bills.

## How it works

```
 recording.json ─▶ ingest ─▶ clicks.json ─┐
       │                                   ├─▶ merge ─▶ session.json
       └────────▶ replay ─▶ network.json ─┘        (every click + its backend calls)
                  (Puppeteer against Odoo,
                   database restored first)
                                                       │
            LangGraph pipeline (resumable, one trace per run in LangSmith)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ segment ─▶ trace ─▶ assess ─▶ plan ─▶ [person approves] ─▶ build ─▶ deliver │
 └──────────────────────────────────────────────────────────────────────────────┘
   tasks      code      effort    plan.md                    module,     push, PR, email:
   & labels   & why     & friction                           tests,      each one approved
                                                             replay      by a person
```

Each stage reads one file and writes one, validated by a Pydantic contract
(`src/odoo_miner/models.py`, `src/odoo_miner/agents/contracts.py`), so any
stage can be rerun, inspected or replaced on its own. Each is a CLI command and
a node in the same graph.

## How it is kept safe

- **Model output is checked by code.** Segmentations must cover every step
  exactly once (one retry with the errors, then a hard failure). Code
  references the tracer invents are dropped unless the file and line exist.
  The assessor can move a computed score by at most one point. Every file and
  line a plan cites is checked against the source, and what cannot be
  confirmed is shown to the approver.
- **A person approves the plan, then every outward action.** The build only
  starts after the plan is approved, and each push, pull request and email
  pauses again. A test fails if any outward tool is not gated. A reviewer can
  send a plan back with notes - a business rule the planner could not know -
  and it is revised; notes given with an approval go to the builder.
- **Outward actions are narrow.** The builder can only write inside its own
  module folder and Odoo's source is read-only to every agent. Pushes go to
  `feat/<module>` from a temporary git worktree - never the base branch, never
  your checkout - and can only carry the approved module. Email goes only to
  the configured address, over TLS.
- **Everything is on the record.** Approvals and outward actions are appended
  to `out/audit.jsonl` (who, when, what, outcome); every model call is traced
  in LangSmith with the run and stage.
- **Tests need nothing.** 168 tests run with no API key, no Odoo and no
  network; they cannot send mail, push, or emit traces. CI runs them and ruff
  on every push.

## Quick start

Needs Docker, Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 18+ and Chrome
(the replay installs its own).

```bash
uv sync --extra dev
(cd replay && npm install)
cp .env.example .env                 # add ANTHROPIC_API_KEY; LangSmith and SMTP are optional

docker compose up -d
./scripts/init_db.sh                 # "demo" database with Purchase, Inventory, Invoicing
uv run python scripts/seed.py        # the purchasing scenarios below
./scripts/snapshot_db.sh             # every replay starts from this state
git clone --depth 1 -b 18.0 https://github.com/odoo/odoo vendor/odoo   # for the tracer

uv run odoo-miner doctor             # checks all of the above
uv run odoo-miner run tests/fixtures/rfq_to_payment.json -d out/rfq_to_payment \
  --pre-hook ./scripts/restore_db.sh
uv run odoo-miner analyze out/rfq_to_payment --until approve
```

The last command stops at the approval gate with the plan in
`out/rfq_to_payment/plan.md`; `odoo-miner report out/rfq_to_payment` emails it
with screenshots of where the buyer got stuck (add `--screenshots` to the
`run` command to capture them). [`docs/RUNBOOK.md`](docs/RUNBOOK.md) covers every
step, what to expect, and what to do when something fails.

## Commands

| Command | Does |
|---|---|
| `odoo-miner doctor` | Checks keys, Odoo, databases, source checkout, replay, email and GitHub; flags shell variables overriding `.env`. |
| `odoo-miner run REC -d DIR` | Ingest, replay and merge one recording into `DIR`. |
| `odoo-miner ingest` / `replay` / `merge` | The same three steps separately. |
| `odoo-miner show FILE` | Any pipeline file as a table: click log, session, segments, traces, assessment, plan or build. |
| `odoo-miner segment` / `trace` / `assess` / `plan` | One analysis stage. `assess --offline` needs no API key. |
| `odoo-miner analyze DIR [--until STAGE]` | The stages as one resumable pipeline. |
| `odoo-miner analyze DIR --approve` / `--reject` [`--notes "…"`] | Answer whichever approval the pipeline is waiting on. At the plan, `--reject --notes` sends it back to be revised and `--approve --notes` gives the builder conditions. |
| `odoo-miner report DIR` | Email the plan and screenshots of where the user got stuck to `REPORT_EMAIL_TO`, after you confirm. |
| `odoo-miner audit [--run NAME] [--full]` | The audit log as a table: who approved or rejected what, and what was sent out; `--full` shows the notes whole. |

## Setup details

`docker-compose.yml` runs Odoo 18 Community and PostgreSQL 16. The CLI and the
replay run on your machine and talk to Odoo at `http://localhost:8069`
(`admin` / `admin`).

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
and starts Odoo again - and starts it again if anything fails on the way. Pass
it as `--pre-hook` so every replay starts from the same data. Custom modules go
in `addons/`, which is mounted into the container;
`./scripts/install_module.sh <module>` installs or updates one and
`./scripts/test_module.sh <module>` runs its tests, both with Odoo's web server
stopped while they work. The builder uses the same two scripts.

## Recording a workflow

1. In Chrome, open DevTools → **Recorder**, record the workflow in Odoo, and
   export it as **JSON**. Start from the login page, or pass your session
   cookie with `--cookie "session_id=…"`.
2. Replay it and analyze it:

```bash
uv run odoo-miner run recording.json -d out/my_run --pre-hook ./scripts/restore_db.sh
uv run odoo-miner show out/my_run/session.json
uv run odoo-miner analyze out/my_run --until assess
```

`--skip-replay` on `run` (or `merge` without a network file) gives you clicks
only, with no backend calls.

## What each file contains

| File | Model | Contents |
|---|---|---|
| `clicks.json` | `ClickLog` | Each step's type, target (aria label, visible text, `name` attribute, CSS), typed value, the page URL it happened on, and Odoo context parsed from that URL (model, record id, action, view type). |
| `network.json` | `NetworkLog` | Every Odoo RPC fired during replay (`call_kw`, `call_button`, `action/load`, `/json/2`), with model, method, args, status and the recording step that was running. |
| `session.json` | `Session` | Clicks with their backend calls attached, each call tagged `read` / `write` / `compute` / `action_load` / `unknown`, plus `has_write` per step. |
| `segments.json` | `SegmentLog` | Steps grouped into one-thing-each tasks, labelled in business language, with the record touched and whether it completed, failed or recovered. |
| `traces.json` | `TraceLog` | Per task: retrieval / action / navigation / mixed, the Odoo methods that ran (`file:line`, override chain included), the data looked up, and a plain-language explanation. |
| `assessment.json` | `Assessment` | A 1-5 difficulty score per step from signals detected in code, plus effort and friction per task. |
| `plan.json` / `plan.md` | `Plan` | No change, data fix, configuration or a new module - with the views and methods involved, what it saves, acceptance criteria, risks, and any citation the check could not confirm. |
| `build.json` | `BuildResult` | Branch, commit, pull request, test result, replay result, and effort before against after. |
| `audit.jsonl` | - | One line per approval and outward action. |

## Things to know

- **Replay repeats every write.** If the recording posts a bill, so does the
  replay. Restore the database before each run with `--pre-hook`; the replay
  aborts if the hook fails.
- **No human timing.** The Recorder format stores no timestamps, and replay
  timestamps reflect the replay, not the person. Difficulty is scored from
  structure, not time.
- **Odoo 18 does not change the URL for most screens**, so `page.model` is
  usually empty. The model a step worked on comes from its backend calls.
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
  calls land on the step that caused them.
- **Debugging a failed replay.** On failure the replay saves
  `network.failure.png` next to `network.json` showing the page where it
  stopped. Add `--screenshots` to save one after every step.
- **New-tab steps are skipped.** Recordings started from a new tab begin with
  `chrome://newtab`, which a fresh browser can't open; both ingest and replay
  skip browser-internal pages and keep the original step numbers.
- **Call kinds are hints.** The read/write classification uses known Odoo
  method names and `action_`/`button_` prefixes. The tracer corrects them
  against the source.
- **Shell variables win over `.env`.** That is deliberate - a deployment can
  override the file - but a stale `export SMTP_PORT=587` once broke email
  silently. `odoo-miner doctor` reports any variable that overrides `.env`.

## Development

```bash
uv run pytest                        # 168 tests, no API key or Odoo needed
uv run ruff check src tests evals scripts
uv run python evals/run_evals.py check   # evaluators against the gold labels, offline
uv run langgraph dev                 # the pipeline in LangGraph Studio
```

Rules for changes - contracts, milestones, what must never happen - are in
[`AGENTS.md`](AGENTS.md).

## Project layout

```
src/odoo_miner/
  cli.py  recorder.py  merge.py  models.py  odoo_urls.py  doctor.py
  agents/      config, contracts, one module per stage, audit log, prompts/
  agents/tools/  odoo_source (search Odoo's code), odoo_ops (Docker, replay), delivery (push, PR, email)
  pipeline/    LangGraph state and graph
replay/        Puppeteer replay that captures backend calls per step
recordings/    Chrome Recorder exports of the workflows analyzed here
addons/        Odoo modules the builder writes, mounted into the container
scripts/       database init, seed, snapshot, restore; install and test a module
evals/         gold labels and LangSmith evaluators
tests/         unit and integration tests, recordings and sessions as fixtures
docs/          runbook and the agent build guide
```
