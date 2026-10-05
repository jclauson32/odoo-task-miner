"""assessor_agent: score how hard each step and segment was.

Scores must be reproducible, so every signal is detected in code here and the
same input always produces the same number. The LLM only explains the scores
and may nudge a step by at most one point - a limit enforced in code, not
asked for in the prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..models import Session, SessionClick
from .config import load_prompt, model_for, settings, trace_config
from .contracts import (
    Assessment, Segment, SegmentAssessment, SegmentLog, StepDifficulty,
)
from .tools import odoo_source

MIN_SCORE, MAX_SCORE = 1, 5
MAX_ADJUSTMENT = 1

# Wizards whose appearance means a modal opened.
WIZARD_HINTS = (".wizard", "account.payment.register")
TAB_HINTS = ("nav-link", "o_notebook", "nav-item")
DIALOG_HINTS = ("dialog_", "o_dialog", "modal")


def _selectors(click: SessionClick) -> str:
    if not click.target:
        return ""
    return " ".join(click.target.selectors or []) + " " + (click.target.css or "")


def signals_for(
    click: SessionClick,
    previous_models: list[str],
    visited: set[tuple[str, Optional[int]]],
    hidden_fields: Optional[set[str]] = None,
) -> dict[str, float]:
    """Detect each difficulty signal for one step. 0/1 per signal."""
    selectors = _selectors(click).lower()
    calls = click.calls
    models = [c.model for c in calls if c.model]
    methods = {c.method for c in calls if c.method}

    typing = 1.0 if click.type == "change" else 0.0
    lookup = 1.0 if {"name_search", "web_name_search"} & methods else 0.0
    tab_switch = 1.0 if any(h in selectors for h in TAB_HINTS) else 0.0

    screen_change = 0.0
    if any(c.kind == "action_load" for c in calls):
        screen_change = 1.0
    elif models and previous_models and models[0] != previous_models[-1]:
        screen_change = 1.0

    modal = 0.0
    if any(h in selectors for h in DIALOG_HINTS):
        modal = 1.0
    elif any(any(w in (m or "") for w in WIZARD_HINTS) for m in models):
        modal = 1.0

    error = 1.0 if any(c.rpc_error for c in calls) else 0.0

    backtrack = 0.0
    for model in models:
        key = (model, click.page.record_id)
        if key in visited and model != (previous_models[-1] if previous_models else None):
            backtrack = 1.0
            break

    hidden_field = 0.0
    if click.type == "change" and hidden_fields:
        label = (click.target.aria_label or click.target.text or "") if click.target else ""
        if label and any(label.strip().rstrip("?").lower() == f.lower() for f in hidden_fields):
            hidden_field = 1.0
    if click.type == "change" and tab_switch:
        hidden_field = max(hidden_field, 1.0)

    wasted_click = 1.0 if (click.type == "click" and not calls) else 0.0

    return {
        "typing": typing, "lookup": lookup, "tab_switch": tab_switch,
        "screen_change": screen_change, "modal": modal, "error": error,
        "backtrack": backtrack, "hidden_field": hidden_field,
        "wasted_click": wasted_click,
    }


def score_from(signals: dict[str, float], weights: Optional[dict[str, float]] = None) -> int:
    """clamp(1 + sum(weight x signal), 1, 5)."""
    weights = weights or settings().weights
    total = 1.0 + sum(weights.get(name, 0.0) * value for name, value in signals.items())
    return int(max(MIN_SCORE, min(MAX_SCORE, round(total))))


def hidden_fields_for(session: Session) -> set[str]:
    """Field labels that sit behind a notebook page or are conditionally invisible.

    Needs the Odoo source; returns an empty set without it, which only means
    the `hidden_field` signal falls back to the notebook-tab heuristic.
    """
    if not odoo_source.source_available():
        return set()
    models = {c.model for click in session.clicks for c in click.calls if c.model}
    hidden: set[str] = set()
    for model in sorted(models):
        try:
            for entry in odoo_source.find_view_fields(model):
                if entry.get("page") or entry.get("invisible"):
                    hidden.add(entry["field"])
        except odoo_source.SourceUnavailable:
            break
    return hidden


def assess_steps(session: Session, segments: SegmentLog) -> dict[str, list[StepDifficulty]]:
    """Deterministic scores for every step, grouped by segment.

    Walks the session in order so that `screen_change` and `backtrack` see the
    real history, not just the steps of one segment.
    """
    hidden = hidden_fields_for(session)
    owner = {i: seg.segment_id for seg in segments.segments for i in seg.step_indexes}

    out: dict[str, list[StepDifficulty]] = {seg.segment_id: [] for seg in segments.segments}
    seen_models: list[str] = []
    visited_in_segment: dict[str, set[tuple[str, Optional[int]]]] = {}
    unassigned: set[tuple[str, Optional[int]]] = set()

    for click in session.clicks:
        segment_id = owner.get(click.step_index)
        # A backtrack means returning to something visited earlier in the same
        # segment; across segments it is just the next task.
        visited = visited_in_segment.setdefault(segment_id, set()) if segment_id else unassigned
        signals = signals_for(click, seen_models, visited, hidden)
        if segment_id is not None:
            out.setdefault(segment_id, []).append(
                StepDifficulty(
                    step_index=click.step_index,
                    score=score_from(signals),
                    signals={k: v for k, v in signals.items() if v},
                )
            )
        for model in [c.model for c in click.calls if c.model]:
            visited.add((model, click.page.record_id))
            seen_models.append(model)

    return out


def friction_for(session: Session, steps: list[StepDifficulty]) -> list[str]:
    """Obstacles worth naming, computed before the LLM rewrites them."""
    by_index = {c.step_index: c for c in session.clicks}
    out: list[str] = []
    for step in steps:
        click = by_index.get(step.step_index)
        if not click:
            continue
        for call in click.calls:
            if call.rpc_error:
                out.append(f"error: {call.rpc_error.strip()[:160]}")
        if step.signals.get("modal"):
            out.append("had to work through a dialog or wizard")
        if step.signals.get("backtrack"):
            out.append("came back to a record already left earlier in this task")
        if step.signals.get("hidden_field"):
            out.append("edited a field that is not visible on the main form")
    seen: set[str] = set()
    return [f for f in out if not (f in seen or seen.add(f))]


def assess_deterministic(session: Session, segments: SegmentLog) -> Assessment:
    """The whole assessment without any LLM. Stable across runs."""
    per_segment = assess_steps(session, segments)
    assessments = []
    for seg in segments.segments:
        steps = per_segment.get(seg.segment_id, [])
        assessments.append(
            SegmentAssessment(
                segment_id=seg.segment_id,
                effort=float(sum(s.score for s in steps)),
                steps=steps,
                friction=friction_for(session, steps),
            )
        )
    return Assessment(
        session=session.source,
        total_effort=float(sum(a.effort for a in assessments)),
        segments=assessments,
    )


def clamp_adjustments(
    proposed: SegmentAssessment, baseline: SegmentAssessment
) -> SegmentAssessment:
    """Keep the LLM's score changes within +/-1 and recompute effort ourselves."""
    base = {s.step_index: s for s in baseline.steps}
    steps: list[StepDifficulty] = []

    for step in proposed.steps:
        original = base.get(step.step_index)
        if original is None:
            continue                      # invented step; drop it
        low = max(MIN_SCORE, original.score - MAX_ADJUSTMENT)
        high = min(MAX_SCORE, original.score + MAX_ADJUSTMENT)
        steps.append(
            step.model_copy(update={
                "score": int(max(low, min(high, step.score))),
                "signals": original.signals,     # signals are evidence, not opinion
            })
        )

    missing = [s for index, s in base.items() if index not in {p.step_index for p in steps}]
    steps = sorted(steps + missing, key=lambda s: s.step_index)
    return proposed.model_copy(update={
        "segment_id": baseline.segment_id,
        "steps": steps,
        "effort": float(sum(s.score for s in steps)),
    })


def render_segment(seg: Segment, assessment: SegmentAssessment, session: Session) -> str:
    by_index = {c.step_index: c for c in session.clicks}
    lines = [
        f"Segment {assessment.segment_id}: {seg.label}",
        f"Intent: {seg.intent}   Outcome: {seg.outcome}   Computed effort: {assessment.effort}",
        "",
        "Steps (score and the signals behind it):",
    ]
    for step in assessment.steps:
        click = by_index.get(step.step_index)
        target = ""
        if click and click.target:
            target = (click.target.aria_label or click.target.text
                      or click.target.button_name or click.target.css or "")
        signals = ", ".join(sorted(step.signals)) or "none"
        value = f" = {click.value!r}" if click and click.value else ""
        lines.append(
            f"  {step.step_index:>3} score {step.score}  "
            f"{(click.type if click else '?'):<11} \"{target[:38]}\"{value}  [{signals}]"
        )
    if assessment.friction:
        lines += ["", "Detected friction:"] + [f"  - {f}" for f in assessment.friction]
    return "\n".join(lines)


def build_assessor(model: Optional[str] = None):
    from langchain.agents import create_agent

    return create_agent(
        model=model or model_for("assessor"),
        tools=[],
        system_prompt=load_prompt("assessor"),
        response_format=SegmentAssessment,
        name="assessor_agent",
    )


def run_assessor(
    segments: SegmentLog, session: Session, agent: Any = None, run: str = "adhoc",
    explain: bool = True,
) -> Assessment:
    """Score the session, then let the model explain. `explain=False` stays offline."""
    baseline = assess_deterministic(session, segments)
    if not explain:
        return baseline

    agent = agent or build_assessor()
    by_id = {s.segment_id: s for s in segments.segments}
    out = []
    for assessment in baseline.segments:
        seg = by_id[assessment.segment_id]
        result = agent.invoke(
            {"messages": [{"role": "user", "content": render_segment(seg, assessment, session)}]},
            config=trace_config(run, "assessor", segment=assessment.segment_id),
        )
        proposed = result["structured_response"]
        if not isinstance(proposed, SegmentAssessment):
            proposed = SegmentAssessment.model_validate(proposed)
        out.append(clamp_adjustments(proposed, assessment))

    return Assessment(
        session=session.source,
        total_effort=float(sum(a.effort for a in out)),
        segments=out,
    )


def run_assessor_path(
    segments_path: Path, session_path: Path, out: Path, agent: Any = None,
    run: str = "adhoc", explain: bool = True,
) -> Assessment:
    segments = SegmentLog.model_validate_json(Path(segments_path).read_text(encoding="utf-8"))
    session = Session.model_validate_json(Path(session_path).read_text(encoding="utf-8"))
    assessment = run_assessor(segments, session, agent=agent, run=run, explain=explain)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(assessment.model_dump_json(indent=2), encoding="utf-8")
    return assessment
