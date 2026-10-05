# AGENTS.md

Instructions for coding agents (and people) working in this repository.

## What this project is

`odoo-miner` records how someone works in Odoo 18, replays it to capture the
backend calls behind each click, and (in progress) uses a pipeline of agents
to find friction and build customizations that remove it.

- Pipeline and data formats: `README.md`
- **How to build the agents, step by step: `docs/AGENT_BUILD_GUIDE.md`**

## Before you change anything

```bash
source .venv/bin/activate          # or prefix commands with `uv run`
docker compose up -d               # Odoo 18 + Postgres 16
odoo-miner doctor                  # must end with "Ready."
pytest
odoo-miner run tests/fixtures/rfq_to_payment.json -d out/rfq_to_payment \
  --pre-hook ./scripts/restore_db.sh
```

All must succeed. If they don't, fixing that is the task. `docs/RUNBOOK.md`
walks through the whole scenario.

## Rules

- Never edit `vendor/odoo` (Odoo's source, read-only reference) or Odoo's code
  in the container. Customizations go in `addons/<module>/`.
- Every stage reads one file and writes one file, validated by the Pydantic
  models in `src/odoo_miner/models.py` and `src/odoo_miner/agents/contracts.py`.
  Changing a contract means updating producer, consumers, tests and the guide together.
- Model names come from config (`ODOO_MINER_MODEL`), never hard-coded.
- Tests must run without API keys; mock models or test the deterministic parts.
  `tests/conftest.py` also stops any test from sending email, pushing, writing
  the real audit log, or sending LangSmith traces - keep it that way.
- Every tool that sends something out of the machine is listed in
  `builder.GATED_TOOLS`; a test fails if one is missing.
- Never push to `main`; feature branches are `feat/<name>`.
- Don't send email anywhere except the configured address.

## Milestone status

Update this table and the log below whenever you stop working.

| ID | Milestone | Status |
|---|---|---|
| M0 | Setup (deps, .env, LangSmith tracing) | **done** - runs trace to LangSmith |
| M1 | Contracts + gold data | **done** - 12 hand-labelled segments; `evals/run_evals.py push` sends them to LangSmith |
| M2 | segmenter_agent | **done** - boundary F1 0.78 / 0.75 against gold in two runs; full coverage |
| M3 | tracer_agent | **done** - expected methods found; every CodeRef exists (27/27, 23/23); prompt-cached |
| M4 | assessor_agent | **done** - deterministic scores stable; bill error and dialog flagged |
| M5 | LangGraph pipeline | **done** - one command, one trace; resumes after a crash |
| M6 | planner_agent + approval | **done** - valid plan, readable plan.md, citations checked, graph pauses; a reviewer can send a plan back with notes (revised up to twice) or approve it with conditions the builder follows |
| M7 | builder_agent | **done** - two end-to-end runs: `bill-exception-review.json` → `purchase_bill_match_columns` (3/3 tests, effort 34 → 22, PR #1) and `bill-exception-resolution.json` → `purchase_bill_apply_po_terms` (10/10 tests, 47 → 24 steps, effort 77 → 32, PR #2); each had one push rejected on review and fixed |
| M8 | Demo polish | **mostly done** - `doctor`, `report`, runbook, Studio via `langgraph dev`, traces from a fresh terminal; no recorded backup video yet |

## What needs an API key

Nothing in `pytest` does. Every model-calling stage does: `segment`, `trace`,
`assess` (unless `--offline`), `plan`, `analyze`, and `evals/run_evals.py
segmenter`. Without a key this still works and is the useful offline loop:

```bash
cp evals/datasets/rfq_to_payment.segments.json out/rfq_to_payment/segments.json
odoo-miner assess out/rfq_to_payment/segments.json \
  --session out/rfq_to_payment/session.json \
  -o out/rfq_to_payment/assessment.json --offline
python evals/run_evals.py check
```

## Handoff log

Newest first. One entry per working session: date, who, what changed, what's
next, anything surprising.

- 2026-10-05 (later): Automated a process that a written policy decides, end
  to end. Recorded `recordings/bill-exception-resolution.json` (an AP clerk on
  all four seeded exceptions; `capture.mjs` now records chatter posts). The
  planner's first plan showed the PO's numbers on the bill; sent back with the
  payables policy, it became `purchase_bill_apply_po_terms`, an Apply PO
  Terms button, approved with conditions. The builder's first push was
  rejected: a PO in dozens billed in units came out 23.96 instead of 24, and
  it had changed the test rather than the code. Fixed; 10/10 tests; the
  after-replay posts all four bills at PO terms (checked in the database),
  47 → 24 steps, effort 77 → 32; PR #2 and the email went out through the
  gates (the email was sent back once for promising screenshots it lacked).
  New: `--reject --notes` sends a plan back to be revised (`plan.rejected-N.*`
  kept); approval notes now reach the builder; `show` renders `build.json`;
  `scripts/test_module.sh`. Fixed: the citation check skipped every path under
  the planner's `/odoo/` mount and still reported "all found"; module installs
  and tests ran beside a running Odoo and collided with its start-up ("could
  not serialize access due to concurrent update") - both scripts now stop the
  web server, and the builder's tools call them. `feat/exception-automation`
  is merged into `main`. Next: merge PRs #1/#2 after review; add the four-bill
  session to the eval datasets; keep policies in a file the planner reads up front instead of
  learning them from a send-back.
  Surprise: the builder will weaken a test to get to green. The push gate
  caught it because the reviewer read the test diff, not just the result.
- 2026-10-05 (morning): First end-to-end build. Recorded (from the live UI)
  `recordings/bill-exception-review.json`: a buyer resolving two three-way-match
  exceptions by hopping to the PO and receipts. The planner chose `customize`
  (`purchase_bill_match_columns`: PO qty / received qty / PO price on bill
  lines); the builder wrote it, installed it, passed 3/3 tests, replayed the
  workflow without the hops (20 → 14 steps, effort 34 → 22), and delivered
  branch, PR #1 and email through the gates. The first push was rejected - the
  columns leaked onto customer invoices - and the builder fixed it and asked again.
  Fixed on the way: replay restored the DB *after* installing the module (the
  after-run never had it); the builder could not see the recording; module
  tests could "pass" with none run; before/after effort was scored differently;
  `action_view_*` smart buttons counted as writes; a tool exception ended the
  whole run (tools now report errors to the model, but never swallow an
  approval pause); the builder could not resubmit after fixing review notes;
  runs from a fresh terminal were never traced (.env loaded too late); email
  attachments with the same name. All on `feat/production-hardening`.
  Next: merge the branch and PR #1; record more scenarios from the seed
  (rejected goods, extra charges) and add them to the eval datasets.
- 2026-10-05: Ran every LLM stage for the first time and fixed what that
  exposed; hardened delivery; production pass over the repo. On branch
  `feat/production-hardening`.
  - First live runs: segmenter F1 0.78/0.75; tracer crashed on Anthropic's
    strict schema (`QueryRef.domain` is an untyped list) - the model now
    returns `TracedSegmentDraft` and code attaches retrievals; planner
    recommends **no change** (price edit is a training gap; the bill date is
    a deliberate audit control) with 4 of 5 citations exact.
  - Three difficulty signals were dead or wrong: `find_view_fields` matched
    nothing (views declare their model as element text), `tab_switch` never
    fired (Odoo 18 tabs are positional selectors - matched by page label
    now), `hidden_field` never matched (`Sales\xa0Price` has a non-breaking
    space). `wasted_click` ignored half its definition.
  - The approval gates were broken: `--approve` answered tool gates in the
    plan shape (`KeyError: 'decisions'`). Fixed and tested end to end.
  - Delivery: push from a temp worktree (never your checkout), new pull
    request tool, email over TLS (465 or STARTTLS), audit log, builder tools
    bound to the run and the approved module.
  - New: `odoo-miner doctor`, `odoo-miner report`, citation check on plans,
    prompt caching for the tracer (82% cache reads), resume after a crash,
    `show` numbered by recording step, ruff, CI, `docs/RUNBOOK.md`.
  - The reference fixture was a different recording from the gold labels;
    fixed, and the reference session is now committed as a fixture.
  - Surprises: a stale `export SMTP_PORT=587` in the shell overrides `.env`
    (python-dotenv never overrides) and broke email as "connection reset" -
    `doctor` now flags shadowed variables. The planner costs ~$6.65 a run,
    almost all reading Odoo source; it is already 95% cached.
  - Next: merge the branch; run the builder end to end on a recording whose
    plan is `customize` (the seed's `price_variance` bill is a candidate);
    record a backup demo video.
- 2026-10-04 (later): Built M0-M7 of the agent guide. New:
  `agents/{config,contracts,segmenter,tracer,assessor,planner,builder}.py`,
  `agents/tools/{odoo_source,odoo_ops,delivery}.py`, six prompts in
  `agents/prompts/`, `pipeline/{state,graph}.py`, `evals/run_evals.py`,
  `evals/datasets/rfq_to_payment.segments.json` (12 hand-labelled segments),
  `langgraph.json`, and CLI subcommands `segment`/`trace`/`assess`/`plan`/`analyze`.
  66 tests pass, none needing an API key. Next: M8, and running every LLM stage
  once a key exists - start with `odoo-miner segment` and compare to the gold
  file with `boundary_f1`.
  Surprises worth knowing:
  - **No API key on this machine**, so no prompt has ever been exercised. The
    network is fine (`api.anthropic.com` answers 401, not a timeout).
  - `session.page.model` is empty for every step of the reference recording -
    Odoo 18 changes screens without changing the URL. Anything that needs to
    know the current model must read it from `calls`, not from `page`.
  - The guide's two model ids were both invalid (`claude-sonnet-5-5` does not
    exist; date suffixes are not appended). Fixed in `.env.example` and the guide.
  - `deepagents` needs Python >= 3.11, so `requires-python` moved up from 3.10.
  - The deterministic pre-pass drops `Tab` keyDowns before the prompt, so the
    model never sees them; `reattach_noise` puts them back afterwards. That is
    why the contract still covers all 54 steps while the prompt shows 53.
  - `backtrack` must be scoped per segment or it fires on nearly every segment.
- 2026-10-04: Recording pipeline complete (ingest, replay, merge; replay
  verified end to end on `tests/fixtures/rfq_to_payment.json`). Agent guide
  written. Next: M0.
