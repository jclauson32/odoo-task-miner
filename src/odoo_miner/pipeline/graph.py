"""The analysis pipeline as a LangGraph StateGraph.

Fixed order of stages, typed shared state, resumable runs, and a human
approval gate between planning and building. Each node calls the same
`run_*` function the CLI calls, so there is one implementation per stage.
"""

from __future__ import annotations

from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..agents import audit
from ..agents.assessor import run_assessor_path
from ..agents.contracts import Plan
from ..agents.planner import run_planner_path
from ..agents.segmenter import run_segmenter_path
from ..agents.tracer import run_tracer_path
from .state import STAGES, PipelineState


def _run_dir(state: PipelineState) -> Path:
    return Path(state.get("run_dir") or ".")


def _session_path(state: PipelineState) -> Path:
    return Path(state.get("session_path") or _run_dir(state) / "session.json")


def segment_node(state: PipelineState) -> dict:
    out = _run_dir(state) / "segments.json"
    run_segmenter_path(_session_path(state), out, run=state.get("run", "adhoc"))
    return {"segments_path": str(out)}


def trace_node(state: PipelineState) -> dict:
    out = _run_dir(state) / "traces.json"
    run_tracer_path(
        Path(state["segments_path"]), _session_path(state), out,
        run=state.get("run", "adhoc"),
    )
    return {"traces_path": str(out)}


def assess_node(state: PipelineState) -> dict:
    out = _run_dir(state) / "assessment.json"
    run_assessor_path(
        Path(state["segments_path"]), _session_path(state), out,
        run=state.get("run", "adhoc"),
    )
    return {"assessment_path": str(out)}


# A reviewer can send a plan back with notes this many times; then it ends.
MAX_PLAN_REVISIONS = 2


def plan_node(state: PipelineState) -> dict:
    run_dir = _run_dir(state)
    out = run_dir / "plan.json"
    feedback = state.get("review_notes") or None
    if feedback:
        # Keep the plan that was sent back, for the record and for the planner to read.
        number = state.get("plan_revisions", 1)
        for name in ("plan.json", "plan.md"):
            previous = run_dir / name
            if previous.exists():
                previous.rename(run_dir / name.replace("plan.", f"plan.rejected-{number}.", 1))
    plan = run_planner_path(run_dir, out, run=state.get("run", "adhoc"), feedback=feedback)
    return {"plan_path": str(out), "decision": plan.decision, "review_notes": ""}


def review_update(decision: dict, revisions: int) -> dict:
    """The state change for a reviewer's answer at the plan gate.

    Rejecting with notes sends the plan back for a revision, up to
    MAX_PLAN_REVISIONS times; rejecting without notes, or once the revisions
    are used up, ends the run.
    """
    update: dict = {"approval": decision}
    notes = (decision.get("notes") or "").strip()
    if not decision.get("approved") and notes and revisions < MAX_PLAN_REVISIONS:
        update["review_notes"] = notes
        update["plan_revisions"] = revisions + 1
    return update


def approve_node(state: PipelineState) -> dict:
    """Pause for a person. Nothing is built or pushed without this."""
    plan = Plan.model_validate_json(
        Path(state["plan_path"]).read_text(encoding="utf-8")
    )
    decision = interrupt({
        "decision": plan.decision,
        "plan_summary": plan.summary,
        "module_name": plan.module_name,
        "expected_steps_before": plan.expected_steps_before,
        "expected_steps_after": plan.expected_steps_after,
        "acceptance_criteria": plan.acceptance_criteria,
        "risks": plan.risks,
        "unverified_citations": plan.unverified_citations,
        "plan_md": str(_run_dir(state) / "plan.md"),
    })
    if isinstance(decision, bool):           # tolerate a bare yes/no
        decision = {"approved": decision, "notes": ""}
    decision = dict(decision or {"approved": False, "notes": "no decision"})
    update = review_update(decision, state.get("plan_revisions", 0))
    outcome = "approved" if decision.get("approved") else (
        "sent back" if "review_notes" in update else "rejected"
    )
    audit.record(
        "approve_plan", outcome,
        run=state.get("run"), plan=state["plan_path"], decision=plan.decision,
        module=plan.module_name, notes=decision.get("notes") or None,
        revision=state.get("plan_revisions") or None,
    )
    return update


def build_node(state: PipelineState) -> dict:
    from ..agents.builder import run_builder_path

    out = _run_dir(state) / "build.json"
    run_builder_path(
        _run_dir(state), Path(state["plan_path"]), out, run=state.get("run", "adhoc")
    )
    return {"build_path": str(out)}


NODES = {
    "segment": segment_node,
    "trace": trace_node,
    "assess": assess_node,
    "plan": plan_node,
    "approve": approve_node,
    "build": build_node,
}


def is_tool_gate(payload: Any) -> bool:
    """True when a pause is the builder asking to run a gated tool.

    The pipeline pauses for two different reasons, and each expects a
    differently shaped answer: the plan gate (approve_node) takes
    `{"approved", "notes"}`, while the builder's tool gates (push, pull
    request, email) take `{"decisions": [...]}`, one per requested action.
    """
    return isinstance(payload, dict) and "action_requests" in payload


def resume_value(payload: Any, approved: bool, notes: str = "") -> dict:
    """The answer to send back for a pause, in the shape that pause expects."""
    if is_tool_gate(payload):
        if approved:
            decision: dict = {"type": "approve"}
        else:
            decision = {"type": "reject", "message": notes or "Rejected by the reviewer."}
        return {"decisions": [decision for _ in payload["action_requests"]]}
    return {"approved": approved, "notes": notes}


def record_tool_decision(payload: Any, approved: bool, notes: str, run: str | None) -> None:
    """Audit each gated tool call a person approved or rejected."""
    for request in payload.get("action_requests", []):
        audit.record(
            f"approve_{request.get('name', 'tool')}", "approved" if approved else "rejected",
            run=run, args=request.get("args"), notes=notes or None,
        )


def _after_review(state: PipelineState) -> str:
    """Build an approved plan that asks for a module; revise one sent back; else end."""
    if (state.get("approval") or {}).get("approved"):
        return "build" if state.get("decision") == "customize" else END
    if state.get("review_notes"):
        return "plan"
    return END


def build_graph(checkpointer: Any = None, until: str | None = None):
    """Compile the pipeline.

    Args:
        checkpointer: a LangGraph checkpointer. With one, a crashed run resumes
            from the last finished node under the same thread_id - and it is
            required for the approval interrupt to be resumable.
        until: last stage to include, one of STAGES. Defaults to the whole
            pipeline.

    Returns:
        The compiled graph.
    """
    if until is not None and until not in STAGES:
        raise ValueError(f"Unknown stage {until!r}; expected one of {', '.join(STAGES)}.")

    stages = STAGES[: STAGES.index(until) + 1] if until else list(STAGES)
    graph = StateGraph(PipelineState)
    for name in stages:
        graph.add_node(name, NODES[name])

    graph.add_edge(START, stages[0])
    for current, following in pairwise(stages):
        if current == "approve":
            graph.add_conditional_edges(
                "approve", _after_review, {"build": "build", "plan": "plan", END: END}
            )
        else:
            graph.add_edge(current, following)

    last = stages[-1]
    if last == "approve":
        graph.add_conditional_edges("approve", _after_review, {"build": END, "plan": "plan", END: END})
    else:
        graph.add_edge(last, END)

    return graph.compile(checkpointer=checkpointer)


def start_or_resume(graph: Any, config: dict, initial_state: dict, restart: bool = False) -> dict:
    """Run the pipeline on a thread, continuing an unfinished run instead of redoing it.

    LangGraph resumes from the last finished node only when invoked with no
    input; invoking with the initial state starts over and pays for every
    stage again. A run paused for approval is left alone - answering it is
    `--approve` / `--reject`.
    """
    snapshot = graph.get_state(config)
    if snapshot.interrupts:
        return {"__interrupt__": list(snapshot.interrupts)}
    if snapshot.next and not restart:
        return graph.invoke(None, config=config)
    return graph.invoke(initial_state, config=config)


def checkpoint_types() -> list[tuple[str, str]]:
    """Every contract model, registered so checkpoints can restore it.

    Agents run inside pipeline nodes inherit the checkpointer, so their
    structured responses (SegmentLog, TracedSegmentDraft, Plan, ...) are
    saved in it. LangGraph restores only registered types - unregistered ones
    warn today and are slated to be refused - and a refused type would break
    resuming a run. Derived from the module so a new contract cannot be missed.
    """
    from pydantic import BaseModel

    from ..agents import contracts

    return sorted(
        (value.__module__, value.__name__)
        for value in vars(contracts).values()
        if isinstance(value, type) and issubclass(value, BaseModel)
        and value.__module__ == contracts.__name__
    )


@contextmanager
def sqlite_checkpointer(path: str | Path):
    """A SqliteSaver for one run folder, so runs resume across processes."""
    import sqlite3

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        yield SqliteSaver(
            connection, serde=JsonPlusSerializer(allowed_msgpack_modules=checkpoint_types())
        )
    finally:
        connection.close()


# Module-level graph for `langgraph dev` / LangGraph Studio, which supplies
# its own persistence.
graph = build_graph()
