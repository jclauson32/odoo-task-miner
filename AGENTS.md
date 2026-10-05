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
source .venv/bin/activate          # or rely on direnv
docker compose up -d               # Odoo 18 + Postgres 16
pytest
odoo-miner run tests/fixtures/rfq_to_payment.json -d out/rfq_to_payment \
  --pre-hook ./scripts/restore_db.sh
```

Both must succeed. If they don't, fix that first.

## Rules

- Never edit `vendor/odoo` (Odoo's source, read-only reference) or Odoo's code
  in the container. Customizations go in `addons/<module>/`.
- Every stage reads one file and writes one file, validated by the Pydantic
  models in `src/odoo_miner/models.py` and `src/odoo_miner/agents/contracts.py`.
  Changing a contract means updating producer, consumers, tests and the guide together.
- Model names come from config (`ODOO_MINER_MODEL`), never hard-coded.
- Tests must run without API keys; mock models or test the deterministic parts.
- Never push to `main`; feature branches are `feat/<name>`.
- Don't send email anywhere except the configured address.

## Milestone status

Update this table and the log below whenever you stop working.

| ID | Milestone | Status |
|---|---|---|
| M0 | Setup (deps, .env, LangSmith tracing) | **done**, except the LangSmith trace check (needs a key) |
| M1 | Contracts + gold data | **done** (dataset not pushed: needs a key) |
| M2 | segmenter_agent | **code done**, LLM half unrun |
| M3 | tracer_agent | **code done**, resolver verified against real source; LLM half unrun |
| M4 | assessor_agent | **done** (deterministic half verified; LLM half unrun) |
| M5 | LangGraph pipeline | **code done**, topology verified; end-to-end needs a key |
| M6 | planner_agent + approval | **code done**, unrun |
| M7 | builder_agent | **code done**, unrun |
| M8 | Demo polish | not started |

"Code done, unrun" means the stage is written, imports, is wired into the
graph and the CLI, and its deterministic parts are tested - but no model has
ever been called, because this machine has no `ANTHROPIC_API_KEY`. Treat every
prompt as a first draft until it has been run and measured.

## What needs an API key

Nothing in `pytest` does. These do:

```bash
cp .env.example .env          # then fill ANTHROPIC_API_KEY (and LANGSMITH_API_KEY)
odoo-miner segment out/rfq_to_payment/session.json -o out/rfq_to_payment/segments.json
odoo-miner analyze out/rfq_to_payment --until assess
python evals/run_evals.py push && python evals/run_evals.py segmenter
```

Without a key, this still works and is the useful offline loop:

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
