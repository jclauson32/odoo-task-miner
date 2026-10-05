# Agent Build Guide

How to build the analysis agents on top of the `odoo-miner` pipeline, using
LangChain, LangGraph, Deep Agents and LangSmith. Written for two readers:

- **You**, building the agents by hand.
- **A coding agent** picking up the work mid-way. Everything it needs is in
  this file, `AGENTS.md`, and the code. Start at [Handoff protocol](#handoff-protocol).

Versions this guide was checked against (October 2026): `langchain 1.4.3`,
`langgraph 1.2.12`, `deepagents 0.7.21`, `langsmith 0.14.4`,
`langchain-anthropic 1.7.5`. If you upgrade, re-check the API notes in each
section; these libraries move quickly.

---

## 1. What already exists

```
recording.json ─ingest─▶ clicks.json ─┐
       └───────replay──▶ network.json ┴─merge─▶ session.json
```

`odoo-miner run <recording> -d out/<run>` produces `out/<run>/session.json`, a
`Session` (see `src/odoo_miner/models.py`). Each element of `session.clicks` is
one user step with:

| Field | Meaning |
|---|---|
| `index` | position in `session.clicks` |
| `step_index` | position in the original recording (stable ID, use this for joins) |
| `type` | `click`, `change`, `navigate`, `keyDown`, … |
| `target` | `aria_label`, `text`, `button_name`, `css`, `selectors` |
| `value` / `key` | typed value or key pressed |
| `page_url`, `page` | Odoo context parsed from the URL (often sparse: Odoo changes URLs without page loads) |
| `calls` | backend calls this step triggered: `kind` (`read`/`write`/`compute`/`action_load`/`unknown`), `model`, `method`, `args`, `kwargs`, `rpc_error` |
| `has_write` | any call of kind `write` |

Reference recording: `tests/fixtures/rfq_to_payment.json` (create an RFQ for
Apex Guidewire Supply with two product lines → open each product and set its
price → confirm the order → receive and validate → create the bill → hit the
"bill date required" error → set the date → post → pay). Its session is
committed as `tests/fixtures/rfq_to_payment.session.json` and is the main test
input for every agent below; the gold segments in `evals/datasets/` are
labelled against it. Regenerate it with:

```bash
odoo-miner run tests/fixtures/rfq_to_payment.json -d out/rfq_to_payment \
  --pre-hook ./scripts/restore_db.sh
```

---

## 2. Target architecture

```
                  LangGraph StateGraph  (pipeline/graph.py)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ load_session → segmenter → tracer → assessor → planner → [approve] → builder → remeasure │
 └──────────────────────────────────────────────────────────────────────────┘
        code      create_agent create_agent  code +    Deep Agent  interrupt()  Deep Agent   code
                  (structured) (+ tools)     create_agent
                                                                   LangSmith traces + evals everywhere
```

| Stage | Built with | Why that tool |
|---|---|---|
| Pipeline / orchestration | **LangGraph** `StateGraph` + checkpointer | Fixed order of stages, typed shared state, resumable runs, and `interrupt()` for the human approval gate. |
| `segmenter_agent` | **LangChain** `create_agent` with `response_format` | One focused judgement (where do actions start and end, what to call them). Needs validated structured output, few or no tools. |
| `tracer_agent` | Deterministic code + **LangChain** `create_agent` with source-search tools | The read/write split is already computed. The agent's job is locating and explaining backend code, which is tool use in a loop. |
| `assessor_agent` | Deterministic signal extraction + **LangChain** `create_agent` | Scores must be reproducible; the LLM only explains and adjusts within bounds. |
| `planner_agent` | **Deep Agents** `create_deep_agent` | Open-ended: explore Odoo source, weigh options, keep a todo list, delegate research to subagents, write a plan file. |
| Human approval | **LangGraph** `interrupt()` | Nothing gets built or pushed without a person approving the plan. Regulated-industry requirement. |
| `builder_agent` | **Deep Agents** with a scoped filesystem and custom tools | Writes a module, runs tests, replays the workflow; long multi-step work with file edits. |
| Observability and quality | **LangSmith** | Tracing for every run; datasets and evaluators per agent so prompt changes are measured, not guessed. |

Design rules (apply to every stage):

1. **Code first, LLM second.** If something can be computed (grouping by
   record, read/write kind, counting clicks), compute it and give the result to
   the model. The model handles naming, explaining, judging.
2. **Every stage reads a file and writes a file**, validated by a Pydantic
   model in `src/odoo_miner/agents/contracts.py`. A stage can be re-run alone,
   inspected, or swapped.
3. **Every stage has a CLI subcommand** (`odoo-miner segment`, `trace`, …) and
   is also a node in the LangGraph pipeline. Same function underneath.
4. **Never edit Odoo's own code.** Customizations go in `addons/<module>/`.
5. **Model name comes from config**, never hard-coded.

---

## 3. Project layout to create

```
src/odoo_miner/
  agents/
    __init__.py
    config.py          # model names, paths, settings (env-driven)
    contracts.py       # Pydantic models for every stage's output
    segmenter.py       # build_segmenter(), run_segmenter(session) -> SegmentLog
    tracer.py          # deterministic resolver + build_tracer(), run_tracer(...)
    assessor.py        # signal extraction + build_assessor(), run_assessor(...)
    planner.py         # build_planner() (deep agent), run_planner(...)
    builder.py         # build_builder() (deep agent), run_builder(...)
    tools/
      odoo_source.py   # find_method, find_button, read_source, find_view_fields
      odoo_ops.py      # run_module_tests, install_module, replay_workflow
      delivery.py      # git_push_feature_branch, send_report_email
    prompts/
      segmenter.md  tracer.md  assessor.md  planner.md  builder.md
  pipeline/
    __init__.py
    state.py           # PipelineState TypedDict
    graph.py           # build_graph(checkpointer) -> compiled StateGraph
evals/
  datasets/rfq_to_payment.segments.json   # hand-labelled gold data
  run_evals.py                              # LangSmith evaluations
langgraph.json                              # for `langgraph dev` / Studio
.env.example
```

Run artifacts for a run named `<run>` live in `out/<run>/`:

```
session.json  segments.json  traces.json  assessment.json  plan.json  plan.md  build.json
```

---

## 4. Setup (milestone M0)

`deepagents` requires Python >= 3.11, so `requires-python` is `>=3.11`.

```bash
pip install "langchain==1.4.3" "langgraph==1.2.12" "deepagents==0.7.21" \
            "langsmith==0.14.4" "langchain-anthropic==1.7.5" python-dotenv \
            langgraph-checkpoint-sqlite
pip install -U "langgraph-cli[inmem]"     # optional: local Studio UI
git clone --depth 1 -b 18.0 https://github.com/odoo/odoo vendor/odoo   # source for the agents to read
echo "vendor/" >> .gitignore
```

Add these to `pyproject.toml` dependencies too, so the project stays installable.

`.env.example` (copy to `.env`, never commit `.env`):

```
ANTHROPIC_API_KEY=
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=odoo-miner
ODOO_MINER_MODEL=anthropic:claude-sonnet-5
ODOO_MINER_FAST_MODEL=anthropic:claude-haiku-4-5
ODOO_SOURCE=vendor/odoo
```

With `LANGSMITH_TRACING=true` set, every LangChain/LangGraph/Deep Agents call is
traced automatically; no code needed. Add run metadata so traces are findable:

```python
agent.invoke(inputs, config={"metadata": {"run": run_name, "stage": "segmenter"},
                             "tags": ["odoo-miner", "segmenter"]})
```

**Done when:** a trivial `create_agent(model=MODEL, tools=[])` call shows up as
a trace in your LangSmith project.

---

## 5. Contracts (milestone M1)

Write `agents/contracts.py` first. Every agent is built to fill one of these.
Suggested shapes (adjust names if you like, but update this guide):

```python
class Segment(BaseModel):
    segment_id: str                    # "s01", "s02", ...
    step_indexes: list[int]            # Click.step_index values, in order
    label: str                         # "Create RFQ for Apex Guidewire"
    intent: str                        # short verb phrase: "create purchase order"
    record: Optional[RecordRef]        # model + id the segment worked on, if known
    outcome: Literal["completed", "failed", "abandoned", "recovered"]

class SegmentLog(BaseModel):
    session: str
    segments: list[Segment]

class CodeRef(BaseModel):
    module: str; file: str; line: int; symbol: str   # "purchase", "addons/purchase/models/purchase_order.py", 812, "PurchaseOrder.button_confirm"

class QueryRef(BaseModel):
    model: str; method: str; domain: Optional[list]; fields: Optional[list[str]]

class TracedSegment(BaseModel):
    segment_id: str
    kind: Literal["retrieval", "action", "navigation", "mixed"]
    actions: list[CodeRef]             # Python methods that changed data (override chain included)
    retrievals: list[QueryRef]         # data the user had to look up
    explanation: str                   # what the backend actually did, plain language

class StepDifficulty(BaseModel):
    step_index: int
    score: int                         # 1 (trivial) .. 5 (hard)
    signals: dict[str, float]          # see section 8
    rationale: str

class SegmentAssessment(BaseModel):
    segment_id: str
    effort: float                      # sum or weighted sum of step scores
    steps: list[StepDifficulty]
    friction: list[str]                # "error dialog: bill date required", ...

class Plan(BaseModel):
    decision: Literal["customize", "configure", "data_fix", "no_change"]
    target_segments: list[str]
    summary: str
    changes: list[PlannedChange]       # file, kind, description
    module_name: Optional[str]         # addons/<module_name>, if customize
    expected_steps_before: int
    expected_steps_after: int
    acceptance_criteria: list[str]     # testable statements
    risks: list[str]

class BuildResult(BaseModel):
    branch: str; commit: str
    tests_passed: bool; test_output_path: str
    replay_completed: bool
    effort_before: float; effort_after: float
    screenshots: list[str]
    email_sent: bool
```

Also in M1:

- Hand-label the reference session into segments: `evals/datasets/rfq_to_payment.segments.json`.
  This is your gold data. Expect roughly: login, open Purchase, create RFQ,
  add freight line and price, confirm order, update product cost, open order
  and create bill, failed confirm (missing bill date), set date and save,
  confirm bill, register payment.
- Push it to LangSmith as a dataset (section 11).

**Done when:** contracts import cleanly, gold file validates against `SegmentLog`.

---

## 6. `segmenter_agent` (milestone M2) — LangChain `create_agent`

**Job:** group clicks into segments that each accomplish one thing, and name them.

**Deterministic pre-pass** (code, before the LLM):
- Drop pure noise: `keyDown` of `Tab`.
- Propose boundaries: a step with a `write` call usually *ends* a segment; an
  `action_load` call or a navigation to a different model usually *starts* one;
  an `rpc_error` marks a failed attempt.
- Render the session compactly for the prompt, one line per step:
  `27 | click "Confirm Order" | write purchase.order.button_confirm, read purchase.order.web_read`

**Agent:**

```python
from langchain.agents import create_agent

segmenter = create_agent(
    model=config.MODEL,
    tools=[],                         # optional: get_step(step_index) for full detail
    system_prompt=load_prompt("segmenter"),
    response_format=SegmentLog,       # validated output lands in result["structured_response"]
    name="segmenter_agent",
)
result = segmenter.invoke({"messages": [{"role": "user", "content": rendered_session}]})
segments: SegmentLog = result["structured_response"]
```

Prompt essentials: what a segment is, the boundary hints from the pre-pass,
"every step_index belongs to exactly one segment, in order", label in business
language a buyer would use.

**Validate in code after the call** (and retry once with the error message if
it fails): all step indexes covered exactly once, in order, no unknown indexes.

**CLI:** `odoo-miner segment out/<run>/session.json -o out/<run>/segments.json`

**Done when:** on the reference session, boundaries match the gold file closely
(see evaluator in section 11) and validation passes.

---

## 7. `tracer_agent` (milestone M3) — deterministic resolver + `create_agent`

**Job:** for each segment, classify it as retrieval/action/navigation/mixed and
link actions to the Python code that ran and retrievals to the data queried.

**Deterministic part** (`tools/odoo_source.py`), plain functions that are also
exposed as tools:

- `find_method(model: str, method: str) -> list[CodeRef]`: search
  `vendor/odoo/addons/*/models/*.py` and `vendor/odoo/odoo/addons/*/models/*.py`
  for classes with `_name = 'model'` or `_inherit = 'model'` (or a list
  containing it) that define `def method(`. **Return all of them, filtered to
  installed modules**: Odoo modules override each other, and the override
  chain is the real behavior. Two traps visible in the 18.0 source:
  `purchase_requisition` also defines `button_confirm` on `purchase.order`
  (only relevant if installed), and `mail`'s `fetchmail.server` has an
  unrelated `button_confirm` (wrong model). Get the installed-module list once
  per run from `ir.module.module` (`state = 'installed'`) via the same
  JSON-RPC client `scripts/seed.py` uses, and cache it in `out/<run>/`.
- `find_button(name: str, model: str | None) -> list[CodeRef]`: search
  `views/*.xml` for `name="<name>"` on `<button>` elements; return file, line,
  `type` (`object` = Python method, `action` = window action).
- `read_source(file: str, start: int, end: int) -> str`: bounded read, refuse
  paths outside `vendor/odoo` and `addons/`.
- Retrieval queries come straight from the session: `web_search_read` /
  `web_read` / `name_search` calls already carry `model`, `kwargs.domain`,
  `kwargs.specification`. Convert to `QueryRef` in code.

**Agent:**

```python
tracer = create_agent(
    model=config.MODEL,
    tools=[find_method, find_button, read_source],
    system_prompt=load_prompt("tracer"),
    response_format=TracedSegment,
    name="tracer_agent",
)
```

Run it once per segment (they're independent; later you can fan out with
LangGraph `Send`). Give it the segment's steps, their calls, and the code refs
you already resolved; ask it to read the relevant methods and write the
`explanation`, and to correct the classification if needed (e.g.
`action_create_invoice` is an action, `retrieve_dashboard` is a retrieval).

**CLI:** `odoo-miner trace out/<run>/segments.json --session out/<run>/session.json -o out/<run>/traces.json`

**Done when:** for the reference run, the confirm-order segment links to
`purchase.order.button_confirm` in `purchase` (and its overrides), the bill
confirm links to `account.move.action_post`, and every `CodeRef` file:line exists.

---

## 8. `assessor_agent` (milestone M4) — signals in code + `create_agent`

**Job:** score how hard each step and segment is.

**Signals, computed in code per step** (`assessor.py`). Start with these, each
0/1 or a count, and keep the weights in config:

| Signal | How to detect |
|---|---|
| `typing` | step `type == "change"` |
| `lookup` | a `name_search` call (autocomplete/dropdown) |
| `tab_switch` | click on a notebook tab (`target.aria_label` matches a tab, or selector contains `nav-link` / `o_notebook`) |
| `screen_change` | `action_load` call, or `web_read` on a different model than the previous step |
| `modal` | target inside a dialog (`dialog_` in selectors) or a wizard model opened (`get_views` on a `*.wizard`/`account.payment.register`) |
| `error` | any `rpc_error` in the step's calls |
| `backtrack` | returns to a model/record visited earlier **in the same segment** (scoped per segment: crossing into a new segment is the next task, not a backtrack) |
| `hidden_field` | field the user edited sits in a notebook page or under `invisible=` in the form view (look up in view XML via `find_view_fields`) |
| `wasted_click` | click with no calls and no following `change` (e.g. clicking a heading) |

Score = clamp(1 + Σ weight × signal, 1, 5). Segment effort = sum of step scores.

**Agent:** `create_agent(..., response_format=SegmentAssessment)`. It receives
the computed signals and scores, may adjust any step score by at most ±1 with a
reason, and writes `rationale` and `friction`. Enforce the ±1 limit in code.

**CLI:** `odoo-miner assess out/<run>/segments.json --session out/<run>/session.json -o out/<run>/assessment.json`

Scoring needs each segment's `step_indexes`, which only `segments.json` carries
(`traces.json` has `segment_id` but not the steps). Passing `traces.json` works -
the command resolves `segments.json` beside it. `--offline` skips the model call,
so the deterministic scores can be produced without an API key.

**Done when:** scores are identical across two runs before the LLM step, and the
bill segment shows `error` and `modal` friction.

---

## 9. LangGraph pipeline (milestone M5)

Wire M2–M4 together now, before the planner. Each node calls the same
`run_*` function the CLI uses and writes its artifact.

```python
# pipeline/state.py
class PipelineState(TypedDict, total=False):
    run_dir: str
    session_path: str
    segments_path: str
    traces_path: str
    assessment_path: str
    plan_path: str
    approval: dict          # {"approved": bool, "notes": str}
    build_path: str
    errors: list[str]

# pipeline/graph.py
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

def build_graph(checkpointer):
    g = StateGraph(PipelineState)
    g.add_node("segment", segment_node)
    g.add_node("trace", trace_node)
    g.add_node("assess", assess_node)
    g.add_node("plan", plan_node)
    g.add_node("approve", approve_node)
    g.add_node("build", build_node)
    g.add_edge(START, "segment"); g.add_edge("segment", "trace")
    g.add_edge("trace", "assess"); g.add_edge("assess", "plan")
    g.add_edge("plan", "approve")
    g.add_conditional_edges("approve", lambda s: "build" if s["approval"]["approved"] else END)
    g.add_edge("build", END)
    return g.compile(checkpointer=checkpointer)
```

Pass file paths in state, not whole documents: keeps checkpoints small and the
artifacts inspectable. Use `SqliteSaver` (package `langgraph-checkpoint-sqlite`)
so a crashed run resumes from the last finished node with the same `thread_id`.

Add `langgraph.json` so `langgraph dev` opens the graph in LangGraph Studio
(good for the demo):

```json
{ "dependencies": ["."], "graphs": { "odoo_miner": "src/odoo_miner/pipeline/graph.py:graph" }, "env": ".env" }
```

(`graph` there is a module-level compiled graph; Studio supplies its own persistence.)

**CLI:** `odoo-miner analyze out/<run> [--until assess]`

**Done when:** `odoo-miner analyze out/rfq_to_payment --until assess` produces
all three artifacts in one command, and the run appears as one trace tree in LangSmith.

---

## 10. `planner_agent` (milestone M6) — Deep Agents

**Job:** decide whether customizing makes sense; if so, plan it.

```python
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import CompositeBackend, StateBackend, FilesystemBackend

planner = create_deep_agent(
    model=config.MODEL,
    tools=[find_method, find_button, find_view_fields],
    system_prompt=load_prompt("planner"),
    subagents=[{
        "name": "odoo-source-researcher",
        "description": "Answers one specific question about how Odoo 18 implements something, citing files and lines.",
        "system_prompt": load_prompt("planner_researcher"),
        "tools": [find_method, find_button, read_source],
        "model": config.FAST_MODEL,
    }],
    backend=CompositeBackend(
        default=StateBackend(),                                   # scratch notes stay in memory
        routes={
            "/odoo/": FilesystemBackend(root_dir="vendor/odoo", virtual_mode=True),
            "/run/":  FilesystemBackend(root_dir=run_dir, virtual_mode=True),
        },
    ),
    permissions=[FilesystemPermission(operations=["write"], paths=["/odoo/**"], mode="deny")],
    response_format=Plan,
    name="planner_agent",
)
```

What the deep agent gets for free: `write_todos` (planning), `ls`/`read_file`/
`grep`/`glob`/`write_file`/`edit_file` on its backend, and `task` to delegate to
the subagent. `virtual_mode=True` keeps it inside each root; the permission rule
makes Odoo's source read-only.

Prompt essentials:
- Inputs: `/run/assessment.json`, `/run/traces.json`, `/run/segments.json`.
- Consider, in order: no change, data fix, configuration, customization. A
  customization must beat the others on effort saved vs. risk.
- Customizations: a new module under `addons/`, inherit models/views, never
  patch core. Name the exact views and methods to extend (cite file:line).
- Write a human-readable `/run/plan.md` and return the `Plan`.

For the reference run, good candidate plans: make Bill Date default to today
(or required in the form) so the failed confirm can't happen; surface the
product cost on the PO line. "No change" with reasons is also a valid answer.

**Approval node** (in the graph):

```python
def approve_node(state):
    plan = Plan.model_validate_json(Path(state["plan_path"]).read_text())
    decision = interrupt({"plan_summary": plan.summary, "plan_md": state["run_dir"] + "/plan.md"})
    return {"approval": decision}      # {"approved": bool, "notes": str}
```

The CLI shows the plan and resumes with
`graph.invoke(Command(resume={"approved": True, "notes": ""}), config)`.
A run paused at approval shows `result["__interrupt__"]`.

**Done when:** the planner produces a valid `Plan` citing real files, `plan.md`
reads well to a non-developer, and the graph pauses for approval.

---

## 11. LangSmith evaluations (run alongside M2–M6)

Datasets live in LangSmith; the source JSON lives in `evals/datasets/`.

```python
from langsmith import Client
client = Client()
ds = client.create_dataset(dataset_name="odoo-miner/segmenter")
client.create_examples(dataset_id=ds.id, examples=[
    {"inputs": {"session_path": "out/rfq_to_payment/session.json"},
     "outputs": {"segments": gold_segments}},
])

def boundary_f1(outputs: dict, reference_outputs: dict) -> dict:
    ...  # compare segment start indexes; return {"key": "boundary_f1", "score": f1}

client.evaluate(lambda inp: {"segments": run_segmenter(inp["session_path"]).model_dump()["segments"]},
                data="odoo-miner/segmenter", evaluators=[boundary_f1],
                experiment_prefix="segmenter-v1")
```

| Agent | Evaluator |
|---|---|
| segmenter | boundary F1 vs. gold; coverage (every step exactly once) |
| tracer | exact match on expected `(model, method)` per action segment; every `CodeRef` exists on disk |
| assessor | deterministic part: equality with stored expected scores; LLM part: adjustments within ±1 |
| planner | LLM-as-judge rubric: cites real code, picks least-invasive option, testable acceptance criteria |
| builder | tests pass; replay completes; `effort_after < effort_before` |

Add more recordings to every dataset as you capture them (each scenario from
`scripts/seed.py` is a candidate). Run evals before and after every prompt change.

---

## 12. `builder_agent` (milestone M7) — Deep Agents with custom tools

**Job:** implement the approved plan, prove it works, deliver it.

Backend scoped to the new module only:

```python
builder = create_deep_agent(
    model=config.MODEL,
    tools=[read_source, run_module_tests, install_module, replay_workflow,
           measure_effort, git_push_feature_branch, send_report_email],
    system_prompt=load_prompt("builder"),
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            f"/addons/{plan.module_name}/": FilesystemBackend(root_dir=f"addons/{plan.module_name}", virtual_mode=True),
            "/run/": FilesystemBackend(root_dir=run_dir, virtual_mode=True),
        },
    ),
    interrupt_on={"git_push_feature_branch": True, "send_report_email": True},
    response_format=BuildResult,
    name="builder_agent",
)
```

Custom tools (`tools/odoo_ops.py`, `tools/delivery.py`), all thin wrappers over
commands that already work by hand:

| Tool | Wraps |
|---|---|
| `install_module(name)` | `docker compose run --rm odoo odoo -d demo -i <name> --stop-after-init`, then restart Odoo |
| `run_module_tests(name)` | `docker compose run --rm odoo odoo -d demo -i <name> --test-tags /<name> --stop-after-init`; save output to `/run/tests.log` |
| `replay_workflow(recording_path)` | `odoo-miner run <recording> -d <run>/after --pre-hook ./scripts/restore_db.sh --screenshots` (restore first, then install the module, then replay) |
| `measure_effort(run_dir)` | runs segment → trace → assess on the after-run and returns total effort |
| `git_push_feature_branch(title)` | creates `feat/<module>`, commits `addons/<module>`, pushes to the configured remote |
| `send_report_email(to, subject, body, attachments)` | SMTP via env settings; attaches screenshots and `plan.md` |

The "after" recording: the builder writes a new Recorder-format JSON for the
improved workflow (it can copy steps from the original and remove the ones the
change eliminates), saves it to `/run/after_recording.json`, and replays it.
If the replay fails, it reads `network.failure.png` and fixes the recording or
the module.

Module checklist the prompt should enforce: `__manifest__.py` with correct
`depends`, inherited views by XML ID, `tests/` with at least one
`TransactionCase` (and an `HttpCase` tour if UI changed), no core edits.

`interrupt_on` pauses before push and email even after the plan was approved:
two human checkpoints, one before building, one before anything leaves the machine.

**Done when:** on the reference run, the module installs, tests pass, the after
replay completes, effort drops, branch `feat/<module>` exists, and the email
(sent to yourself) has screenshots attached.

---

## 13. Milestones and status

| ID | Milestone | Acceptance check |
|---|---|---|
| M0 | Setup | Trivial agent call appears in LangSmith |
| M1 | Contracts + gold data | Gold segments validate; dataset in LangSmith |
| M2 | segmenter | `odoo-miner segment` works; boundary F1 measured |
| M3 | tracer | Expected methods found; all CodeRefs exist |
| M4 | assessor | Deterministic scores stable; friction lists the bill error |
| M5 | LangGraph pipeline | `odoo-miner analyze --until assess` in one trace |
| M6 | planner + approval | Valid Plan, plan.md, graph pauses |
| M7 | builder | Tests pass, effort drops, branch + email delivered with approvals |
| M8 | Demo polish | Studio view, one-command demo script, recorded backup |

Short on time? M1 → M3 plus the M5 graph skeleton (nodes for M4–M7 returning
stub artifacts) is a credible demo: real recording in, segmented and traced to
Odoo source out, with the rest of the architecture visible in Studio.

Keep the status of each milestone in `AGENTS.md` (see below).

---

## Handoff protocol

For any agent (or person) continuing this work:

1. Read `AGENTS.md` first: it has the current milestone status and notes from
   whoever worked last.
2. Run `pytest` and `odoo-miner run tests/fixtures/rfq_to_payment.json -d out/rfq_to_payment --pre-hook ./scripts/restore_db.sh`.
   Both must pass before you change anything. If they don't, fixing that is the task.
3. Work on the lowest milestone not marked done. Don't start a later milestone
   on top of a failing earlier one.
4. Contracts in `agents/contracts.py` are the interface between stages. Changing
   one means updating its producer, its consumers, this guide, and the tests in
   the same change.
5. Every new stage ships with: a CLI subcommand, a unit test that runs without
   an API key (mock the model or test only the deterministic part), and a
   LangSmith evaluator.
6. Never edit `vendor/odoo`, never push to `main`, never send email to anyone
   other than the configured address.
7. Before you stop, update `AGENTS.md`: milestone status, what you changed,
   what's next, anything surprising. That note is the next agent's starting point.
