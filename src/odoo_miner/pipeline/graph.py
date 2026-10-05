"""The analysis pipeline as a LangGraph StateGraph.

Fixed order of stages, typed shared state, resumable runs, and a human
approval gate between planning and building. Each node calls the same
`run_*` function the CLI calls, so there is one implementation per stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

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


def plan_node(state: PipelineState) -> dict:
    out = _run_dir(state) / "plan.json"
    run_planner_path(_run_dir(state), out, run=state.get("run", "adhoc"))
    return {"plan_path": str(out)}


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
        "plan_md": str(_run_dir(state) / "plan.md"),
    })
    if isinstance(decision, bool):           # tolerate a bare yes/no
        decision = {"approved": decision, "notes": ""}
    return {"approval": dict(decision or {"approved": False, "notes": "no decision"})}


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


def _approved(state: PipelineState) -> str:
    return "build" if (state.get("approval") or {}).get("approved") else END


def build_graph(checkpointer: Any = None, until: Optional[str] = None):
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
    for current, following in zip(stages, stages[1:]):
        if current == "approve":
            graph.add_conditional_edges("approve", _approved, {"build": "build", END: END})
        else:
            graph.add_edge(current, following)

    last = stages[-1]
    if last == "approve":
        graph.add_conditional_edges("approve", _approved, {"build": END, END: END})
    else:
        graph.add_edge(last, END)

    return graph.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: str | Path):
    """A SqliteSaver context manager, so runs resume across processes."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(path))


# Module-level graph for `langgraph dev` / LangGraph Studio, which supplies
# its own persistence.
graph = build_graph()
