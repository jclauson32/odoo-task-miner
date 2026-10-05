# Runbook: the reference scenario, end to end

You will replay a recording of a buyer's purchase-to-pay workflow against a
freshly restored Odoo, analyze it with the agent pipeline, review the plan at
the approval gate, and email the findings. Every command below was run as
written; the outputs shown are from those runs.

Time: about 20 minutes once set up. Model usage: $0.66 to about $7 per full
run, depending mostly on how much source the planner reads (see [Cost](#cost)).

All commands run from the repository root with the environment active:

```bash
source .venv/bin/activate        # or prefix each command with `uv run`
```

---

## 0. Once per machine

```bash
git clone https://github.com/jclauson32/odoo-task-miner && cd odoo-task-miner
uv sync --extra dev
(cd replay && npm install)                 # downloads its own Chrome
cp .env.example .env                       # then edit it, see below
git clone --depth 1 -b 18.0 https://github.com/odoo/odoo vendor/odoo
```

In `.env`, `ANTHROPIC_API_KEY` is required. `LANGSMITH_API_KEY` (with
`LANGSMITH_TRACING=true`) gives you a trace of every run. The `SMTP_*`
settings and `REPORT_EMAIL_TO` are only needed for step 6. Never commit
`.env`; it is ignored by git.

Start Odoo and build the database the recordings expect:

```bash
docker compose up -d                       # Odoo 18 + PostgreSQL 16
./scripts/init_db.sh                       # "demo" with Purchase, Inventory, Invoicing
python scripts/seed.py                     # vendors, products, purchase scenarios
./scripts/snapshot_db.sh                   # the state every replay restores to
```

Odoo is at http://localhost:8069, `admin` / `admin`.

## 1. Pre-flight

```bash
odoo-miner doctor
```

Every line should be a green ✓ and it should end with `Ready.` A `!` is a
warning you can live with; a red ✗ needs fixing first, and the line under it
says how. The one that bites most often:

```
!  Environment   shell overrides .env: SMTP_PORT=587 (.env says 465)
                → unset SMTP_PORT  (or open a new terminal)
```

Variables exported in your shell take precedence over `.env`. Unset them, or
open a new terminal.

## 2. Replay the recording

```bash
odoo-miner run tests/fixtures/rfq_to_payment.json -d out/demo \
  --pre-hook ./scripts/restore_db.sh --screenshots
```

What happens: the pre-hook resets the database to the snapshot (Odoo
restarts), then a headless Chrome replays the recording step by step against it,
capturing the backend calls each one makes. About 50 seconds.

```
✓ 54 steps → out/demo/clicks.json
Restored demo from demo_snapshot
✓ 57 backend calls → out/demo/network.json
✓ 54 steps, 12 with backend writes → out/demo/session.json
```

The replay is deterministic: run it twice and every step makes the same
calls. `--screenshots` saves `out/demo/screenshots/step-NNN.png` after each
step, named by step number.

## 3. See what the buyer did

```bash
odoo-miner show out/demo/session.json --from 44 --to 51
```

One row per recorded step, numbered as in the recording, with every backend
call as `kind model.method`:

```
  44 │ click │ P00008        │ read purchase.order.web_read
  45 │ click │ Confirm Order │ write purchase.order.button_confirm
  ...
  49 │ click │ Create Bill   │ write purchase.order.action_create_invoice
  50 │ click │ Confirm       │ write account.move.action_post
     │       │               │ error: The Bill/Refund date is required to validate this document.
  51 │ click │ Close         │
```

Drop `--from`/`--to` to see all 54 steps.

## 4. Analyze to the approval gate

```bash
odoo-miner analyze out/demo --until approve
```

Runs segment → trace → assess → plan as one LangGraph run, one trace tree in
LangSmith, then pauses. Five to ten minutes; the planner is most of it. When it
stops you will see the plan and the citation check:

```
Waiting for approval of the plan.
  decision: no_change
  plan_summary: …
  acceptance_criterion: …
  risk: …
  Citations to check by hand before approving:
    - `product_template_tree_view` is cited in addons/product/views/product_views.xml but does not appear there.

Approve with:  odoo-miner analyze out/demo --approve --thread demo
Reject with:   odoo-miner analyze out/demo --reject --thread demo --notes "why"
```

The model's wording differs between runs; the shape does not. Read the plan
before deciding:

```bash
less out/demo/plan.md
```

If the run stops partway (a network error, Ctrl-C), run the same command
again: it resumes from the last finished stage instead of starting over and
paying for every stage again. `--restart` starts over on purpose.

Artifacts in `out/demo/`: `segments.json`, `traces.json`,
`assessment.json`, `plan.json`, `plan.md`, and `pipeline.sqlite` (the
checkpoints).

## 5. Approve or reject

```bash
odoo-miner analyze out/demo --approve --thread demo --notes "Reviewed; agree no change is needed."
```

The decision, who made it and your notes go into the audit log:

```bash
tail -1 out/audit.jsonl
```

```json
{"action": "approve_plan", "actor": "you@example.com", "at": "…", "decision": "no_change", "notes": "Reviewed; agree no change is needed.", "outcome": "approved", …}
```

With a `no_change` plan the run ends here. With `customize`, approving starts
the builder; see [Building a change](#building-a-change).

## 6. Email the findings

```bash
odoo-miner report out/demo
```

Shows the recipient, subject and attachments - `plan.md` and the screenshots
of the steps where the buyer hit an error or a dialog - and asks before
sending. The email is built from the run's files, with no model call. The
recipient is always `REPORT_EMAIL_TO`.

## Running stages one at a time

Useful while working on one stage; each reads and writes one file.

```bash
odoo-miner segment out/demo/session.json -o out/demo/segments.json
odoo-miner trace   out/demo/segments.json --session out/demo/session.json -o out/demo/traces.json
odoo-miner assess  out/demo/segments.json --session out/demo/session.json -o out/demo/assessment.json
odoo-miner plan    out/demo
```

`odoo-miner assess … --offline` scores with no model call and no API key.
`python evals/run_evals.py check` scores the evaluators against the gold
labels offline; `push` and `segmenter` run them in LangSmith.

## Building a change

When a plan's decision is `customize`, approving it starts the builder. It
writes `addons/<module>/`, installs it, runs the module's tests until they
pass, replays the workflow with the change, measures effort before and after,
and then asks to push `feat/<module>`, open a pull request, and email the
report - pausing for each one:

```
The builder wants to send something out. Approve each action:
  git_push_feature_branch
    module_name: …
    title: …
```

Answer each with `--approve` or `--reject --notes "…"` on the same thread.
Every answer and every action goes into `out/audit.jsonl`.

The builder is written and its safeguards are tested, but it has not yet
been run end to end: the reference recording's plan was `no_change`, so there
was nothing to build.

## Cost

Measured on the reference recording with Claude Sonnet 5:

| Stage | Model calls | Cost |
|---|---|---|
| segment | 1 | ~$0.09 |
| trace | ~45, 82% of input from cache | ~$0.25 |
| assess | 11-12 | ~$0.08 |
| plan | 11-69 (a Deep Agent; it decides how much of Odoo's source to read) | $0.31-$6.65 |

LangSmith shows the actual figures for every run, per stage.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `doctor`: shell overrides `.env` | A variable exported in your shell, often from an old `.env` | `unset NAME`, or open a new terminal |
| `doctor`: Odoo no answer | Docker or the containers are not running | `systemctl --user start docker-desktop` (Docker Desktop on Linux), then `docker compose up -d` |
| `No snapshot 'demo_snapshot'` | The snapshot was never taken | `./scripts/snapshot_db.sh` |
| `Replay stopped at step N` | The page differed from the recording | Open `out/demo/network.failure.png`; rerun with `--no-headless` to watch |
| `Nothing is waiting for approval` | Wrong thread | The thread defaults to the run folder's name; pass `--thread` |
| Email: `SMTP_PORT is 587, a STARTTLS port` | `SMTP_SSL=true` with a STARTTLS port | Port 465 for implicit TLS, or `SMTP_SSL=false` |
| Email: `rejected the credentials` | Wrong `SMTP_USER` / `SMTP_PASSWORD` | Fix them in `.env` |
| A stage crashed partway | Network or API error | Rerun the same `analyze` command; it resumes |
