"""assessor_agent: score how hard each step and segment was.

Scores must be reproducible, so every signal is detected in code here and the
same input always produces the same number. The LLM only explains the scores
and may nudge a step by at most one point - a limit enforced in code, not
asked for in the prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Session, SessionClick
from .config import load_prompt, model_for, settings, trace_config
from .contracts import (
    Assessment,
    Segment,
    SegmentAssessment,
    SegmentLog,
    StepDifficulty,
)
from .tools import odoo_source

MIN_SCORE, MAX_SCORE = 1, 5
MAX_ADJUSTMENT = 1

# Wizards whose appearance means a modal opened.
WIZARD_HINTS = (".wizard", "account.payment.register")
TAB_HINTS = ("nav-link", "nav-item")


def normalise_label(text: str | None) -> str:
    """A label as the view spells it: no non-breaking spaces, no trailing "?".

    Odoo renders some field labels with a non-breaking space, and the Recorder
    captures it verbatim ("Sales\xa0Price?"), so a raw comparison against the
    view's `string="Sales Price"` can never match.
    """
    if not text:
        return ""
    collapsed = " ".join(text.replace("\xa0", " ").split())
    return collapsed.rstrip("?").strip().lower()
DIALOG_HINTS = ("dialog_", "o_dialog", "modal")


def _selectors(click: SessionClick) -> str:
    if not click.target:
        return ""
    return " ".join(click.target.selectors or []) + " " + (click.target.css or "")


def signals_for(
    click: SessionClick,
    previous_models: list[str],
    visited: set[tuple[str, int | None]],
    hidden_fields: dict[str, set[str]] | None = None,
    next_click: SessionClick | None = None,
    models_in_scope: set[str] | None = None,
    tab_opened_earlier: bool = False,
    page_labels: dict[str, set[str]] | None = None,
) -> dict[str, float]:
    """Detect each difficulty signal for one step. 0/1 per signal."""
    selectors = _selectors(click).lower()
    calls = click.calls
    models = [c.model for c in calls if c.model]
    methods = {c.method for c in calls if c.method}

    label = normalise_label(
        (click.target.aria_label or click.target.text) if click.target else None
    )

    typing = 1.0 if click.type == "change" else 0.0
    lookup = 1.0 if {"name_search", "web_name_search"} & methods else 0.0
    tab_switch = 1.0 if any(h in selectors for h in TAB_HINTS) else 0.0
    if not tab_switch and click.type == "click" and label and page_labels:
        in_scope: set[str] = set()
        for model in (models_in_scope or set()):
            in_scope |= page_labels.get(model, set())
        tab_switch = 1.0 if label in in_scope else 0.0

    screen_change = 0.0
    if any(c.kind == "action_load" for c in calls) or models and previous_models and models[0] != previous_models[-1]:
        screen_change = 1.0

    modal = 0.0
    if any(h in selectors for h in DIALOG_HINTS) or any(any(w in (m or "") for w in WIZARD_HINTS) for m in models):
        modal = 1.0

    error = 1.0 if any(c.rpc_error for c in calls) else 0.0

    backtrack = 0.0
    for model in models:
        key = (model, click.page.record_id)
        if key in visited and model != (previous_models[-1] if previous_models else None):
            backtrack = 1.0
            break

    hidden_field = 0.0
    if click.type == "change":
        candidates: set[str] = set()
        for model in (models_in_scope or set()):
            candidates |= (hidden_fields or {}).get(model, set())
        if label and any(label == normalise_label(name) for name in candidates) or tab_opened_earlier:
            hidden_field = 1.0

    # A click with no calls that is immediately followed by typing was
    # focusing the field, not wasted: it takes both halves to be waste.
    # Opening a tab is navigation, not waste - and counting it as both would
    # charge the same click twice.
    leads_to_typing = next_click is not None and next_click.type in ("change", "doubleClick")
    wasted_click = 1.0 if (
        click.type == "click" and not calls and not leads_to_typing and not tab_switch
    ) else 0.0

    return {
        "typing": typing, "lookup": lookup, "tab_switch": tab_switch,
        "screen_change": screen_change, "modal": modal, "error": error,
        "backtrack": backtrack, "hidden_field": hidden_field,
        "wasted_click": wasted_click,
    }


def score_from(signals: dict[str, float], weights: dict[str, float] | None = None) -> int:
    """clamp(1 + sum(weight x signal), 1, 5)."""
    weights = weights or settings().weights
    total = 1.0 + sum(weights.get(name, 0.0) * value for name, value in signals.items())
    return int(max(MIN_SCORE, min(MAX_SCORE, round(total))))


def hidden_fields_for(session: Session) -> dict[str, set[str]]:
    """Field labels that sit behind a notebook page or are conditionally invisible.

    Keyed by model: a label that is hidden on one model must not mark a
    same-named field on another ("Email" is behind a page somewhere, but the
    login box is not). Variant models inherit their template's views, so
    `x.product` also gets `x.template`'s hidden fields.

    Needs the Odoo source; returns an empty map without it, which only means
    the `hidden_field` signal falls back to the notebook-tab heuristic.
    """
    if not odoo_source.source_available():
        return {}
    models = {c.model for click in session.clicks for c in click.calls if c.model}
    lookup = set(models)
    for model in models:                     # product.product reuses product.template's views
        if model.endswith(".product"):
            lookup.add(model.rsplit(".", 1)[0] + ".template")

    hidden: dict[str, set[str]] = {}
    for model in sorted(lookup):
        try:
            entries = odoo_source.find_view_fields(model)
        except odoo_source.SourceUnavailable:
            break
        for entry in entries:
            if not (entry.get("page") or entry.get("invisible")):
                continue
            names = hidden.setdefault(model, set())
            names.add(entry["field"])
            if entry.get("string"):          # the label the user actually sees
                names.add(entry["string"])

    for model in list(models):               # fold the template's back onto the variant
        if model.endswith(".product"):
            template = model.rsplit(".", 1)[0] + ".template"
            if template in hidden:
                hidden.setdefault(model, set()).update(hidden[template])
    return hidden


def page_labels_for(session: Session) -> dict[str, set[str]]:
    """Notebook page labels per model, normalised.

    Keyed by model so a page named "Purchase" on `product.template` does not
    turn the Purchase app menu into a tab switch.
    """
    if not odoo_source.source_available():
        return {}
    models = {c.model for click in session.clicks for c in click.calls if c.model}
    lookup = set(models)
    for model in models:
        if model.endswith(".product"):
            lookup.add(model.rsplit(".", 1)[0] + ".template")

    labels: dict[str, set[str]] = {}
    for model in sorted(lookup):
        try:
            found = {normalise_label(page) for page in odoo_source.find_view_pages(model)}
        except odoo_source.SourceUnavailable:
            break
        if found:
            labels[model] = {label for label in found if label}

    for model in list(models):
        if model.endswith(".product"):
            template = model.rsplit(".", 1)[0] + ".template"
            if template in labels:
                labels.setdefault(model, set()).update(labels[template])
    return labels


def assess_steps(session: Session, segments: SegmentLog) -> dict[str, list[StepDifficulty]]:
    """Deterministic scores for every step, grouped by segment.

    Walks the session in order so that `screen_change` and `backtrack` see the
    real history, not just the steps of one segment.
    """
    hidden = hidden_fields_for(session)
    pages = page_labels_for(session)
    owner = {i: seg.segment_id for seg in segments.segments for i in seg.step_indexes}

    out: dict[str, list[StepDifficulty]] = {seg.segment_id: [] for seg in segments.segments}
    seen_models: list[str] = []
    visited_in_segment: dict[str, set[tuple[str, int | None]]] = {}
    unassigned: set[tuple[str, int | None]] = set()

    models_seen: dict[str, set[str]] = {}
    tab_opened: set[str] = set()

    clicks = session.clicks
    for position, click in enumerate(clicks):
        segment_id = owner.get(click.step_index)
        # A backtrack means returning to something visited earlier in the same
        # segment; across segments it is just the next task.
        visited = visited_in_segment.setdefault(segment_id, set()) if segment_id else unassigned
        following = clicks[position + 1] if position + 1 < len(clicks) else None
        scope = models_seen.setdefault(segment_id, set()) if segment_id else set()
        signals = signals_for(
            click, seen_models, visited, hidden, next_click=following,
            models_in_scope=scope, tab_opened_earlier=segment_id in tab_opened,
            page_labels=pages,
        )
        if signals.get("tab_switch") and segment_id:
            tab_opened.add(segment_id)
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
            if segment_id:
                models_seen.setdefault(segment_id, set()).add(model)

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


def build_assessor(model: str | None = None):
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
