"""tracer_agent: link each segment to the backend code that ran.

A resolver in code finds the methods behind each write; the agent reads them
and writes the explanation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import NetworkCall, Session, SessionClick
from .config import chat_model, load_prompt, trace_config
from .contracts import (
    CodeRef,
    QueryRef,
    SegmentKind,
    SegmentLog,
    TracedSegment,
    TracedSegmentDraft,
    TraceLog,
)
from .tools import odoo_source

# Calls the framework makes on its own; not something the user looked up.
FRAMEWORK_METHODS = {
    "get_views", "load_views", "fields_get", "default_get", "has_group",
    "check_access_rights", "web_override_translations", "read_progress_bar",
    "get_formview_action", "get_formview_id",
}
# Methods that are a lookup the user actually performed.
RETRIEVAL_METHODS = {
    "name_search", "web_name_search", "web_search_read", "search_read",
    "web_read", "read", "search", "search_count", "read_group", "web_read_group",
}


def segment_clicks(session: Session, step_indexes: list[int]) -> list[SessionClick]:
    """The clicks that belong to a segment."""
    wanted = set(step_indexes)
    return [c for c in session.clicks if c.step_index in wanted]


def classify_segment(clicks: list[SessionClick]) -> SegmentKind:
    """First-pass kind from the calls alone. The agent may correct it."""
    kinds = {call.kind for click in clicks for call in click.calls}
    wrote = "write" in kinds
    read = bool(kinds & {"read", "compute"})

    if wrote and read:
        return "mixed"
    if wrote:
        return "action"
    if read:
        return "retrieval"
    return "navigation"


def retrievals_for(clicks: list[SessionClick]) -> list[QueryRef]:
    """Lookups the user needed, straight from the captured calls."""
    out: list[QueryRef] = []
    seen: set[tuple] = set()

    for click in clicks:
        for call in click.calls:
            method, model = call.method or "", call.model
            if not model or method in FRAMEWORK_METHODS:
                continue
            if method not in RETRIEVAL_METHODS:
                continue
            kwargs = call.kwargs or {}
            domain = kwargs.get("domain") or _first_list(call.args)
            spec = kwargs.get("specification") or kwargs.get("fields")
            fields = sorted(spec) if isinstance(spec, dict) else (
                list(spec) if isinstance(spec, list) else None
            )
            key = (model, method, repr(domain)[:200])
            if key in seen:
                continue
            seen.add(key)
            out.append(QueryRef(model=model, method=method, domain=domain, fields=fields))
    return out


def _first_list(args: list | None) -> list | None:
    """The first list among a call's positional arguments."""
    if not args:
        return None
    for arg in args:
        if isinstance(arg, list):
            return arg
    return None


def action_calls(clicks: list[SessionClick]) -> list[NetworkCall]:
    """The calls that changed data."""
    return [c for click in clicks for c in click.calls if c.kind == "write"]


def resolve_actions(clicks: list[SessionClick]) -> list[CodeRef]:
    """The code behind each write call, found by searching Odoo's source.

    Empty when the source checkout is missing; the agent is told so.
    """
    if not odoo_source.source_available():
        return []

    refs: list[CodeRef] = []
    seen: set[tuple] = set()
    for call in action_calls(clicks):
        if not call.model or not call.method:
            continue
        for ref in odoo_source.find_method(call.model, call.method):
            key = (ref.file, ref.line)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return refs


def verify_refs(refs: list[CodeRef]) -> tuple[list[CodeRef], list[CodeRef]]:
    """Split references into those that exist on disk and those that do not."""
    if not odoo_source.source_available():
        return [], list(refs)
    root = odoo_source.settings().odoo_source_abs
    good, bad = [], []
    for ref in refs:
        path = root / ref.file
        if path.is_file() and ref.line >= 1:
            good.append(ref)
        else:
            bad.append(ref)
    return good, bad


def render_segment(segment, clicks: list[SessionClick], refs: list[CodeRef], queries: list[QueryRef]) -> str:
    """What the agent is given for one segment."""
    lines = [
        f"Segment {segment.segment_id}: {segment.label}",
        f"Intent: {segment.intent}   Outcome: {segment.outcome}",
        f"Pre-computed kind: {classify_segment(clicks)}",
        "",
        "Steps:",
    ]
    for click in clicks:
        target = click.target_label()
        calls = ", ".join(
            f"{c.kind} {c.model or '-'}.{c.method or '-'}" for c in click.calls
        ) or "-"
        lines.append(f"  {click.step_index:>3} {click.type:<11} \"{target[:44]}\" | {calls[:130]}")
        for call in click.calls:
            if call.rpc_error:
                lines.append(f"      error: {call.rpc_error[:160]}")

    lines += ["", "Code references already resolved (verify and extend):"]
    lines += [f"  {r.module}  {r.file}:{r.line}  {r.symbol}" for r in refs] or ["  (none)"]
    if not odoo_source.source_available():
        lines.append("  NOTE: the Odoo source checkout is missing, so none could be resolved.")

    lines += ["", "Retrieval queries extracted from the calls:"]
    lines += [
        f"  {q.model}.{q.method} domain={q.domain!r} fields={(q.fields or [])[:6]}"
        for q in queries
    ] or ["  (none)"]
    return "\n".join(lines)


def tracer_middleware() -> list:
    """Prompt caching for the tracer's tool loop, which re-sends its context every turn.

    The single-shot stages are below the minimum cacheable length, so they go without.
    """
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    return [AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")]


def build_tracer(model: str | None = None):
    """The LangChain agent, with the source-search tools."""
    from langchain.agents import create_agent

    return create_agent(
        model=model or chat_model("tracer"),
        tools=odoo_source.TOOLS,
        system_prompt=load_prompt("tracer"),
        middleware=tracer_middleware(),
        response_format=TracedSegmentDraft,
        name="tracer_agent",
    )


def trace_segment(segment, session: Session, agent: Any = None, run: str = "adhoc") -> TracedSegment:
    """Trace one segment. Segments are independent, so this can be fanned out."""
    agent = agent or build_tracer()
    clicks = segment_clicks(session, segment.step_indexes)
    refs, _ = verify_refs(resolve_actions(clicks))
    queries = retrievals_for(clicks)

    result = agent.invoke(
        {"messages": [{"role": "user", "content": render_segment(segment, clicks, refs, queries)}]},
        config=trace_config(run, "tracer", segment=segment.segment_id),
    )
    draft = result["structured_response"]
    if not isinstance(draft, TracedSegmentDraft):
        draft = TracedSegmentDraft.model_validate(draft)

    # Keep the agent's kind and explanation, but only code references that exist
    # on disk. Retrievals come from the session.
    good, _bad = verify_refs(draft.actions)
    return TracedSegment(
        segment_id=segment.segment_id,
        kind=draft.kind,
        actions=good or refs,
        retrievals=queries,
        explanation=draft.explanation,
    )


def run_tracer(segments: SegmentLog, session: Session, agent: Any = None, run: str = "adhoc") -> TraceLog:
    """Trace every segment of a session."""
    agent = agent or build_tracer()
    traced = [trace_segment(s, session, agent=agent, run=run) for s in segments.segments]
    return TraceLog(session=session.source, segments=traced)


def run_tracer_path(
    segments_path: Path, session_path: Path, out: Path, agent: Any = None, run: str = "adhoc"
) -> TraceLog:
    """Trace a segments file and write traces.json."""
    segments = SegmentLog.model_validate_json(Path(segments_path).read_text(encoding="utf-8"))
    session = Session.model_validate_json(Path(session_path).read_text(encoding="utf-8"))
    log = run_tracer(segments, session, agent=agent, run=run)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    return log
