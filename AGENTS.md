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
| M0 | Setup (deps, .env, LangSmith tracing) | not started |
| M1 | Contracts + gold data | not started |
| M2 | segmenter_agent | not started |
| M3 | tracer_agent | not started |
| M4 | assessor_agent | not started |
| M5 | LangGraph pipeline | not started |
| M6 | planner_agent + approval | not started |
| M7 | builder_agent | not started |
| M8 | Demo polish | not started |

## Handoff log

Newest first. One entry per working session: date, who, what changed, what's
next, anything surprising.

- 2026-10-04: Recording pipeline complete (ingest, replay, merge; replay
  verified end to end on `tests/fixtures/rfq_to_payment.json`). Agent guide
  written. Next: M0.
