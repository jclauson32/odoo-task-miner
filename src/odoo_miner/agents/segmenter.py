"""segmenter_agent: group clicks into segments that each accomplish one thing.

Code first, LLM second. Everything that can be computed - which steps are
noise, where a write or a screen change suggests a boundary, how to render the
session compactly - is computed here. The model only decides where the
boundaries actually fall and what to call each segment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Session, SessionClick
from .config import chat_model, load_prompt, trace_config
from .contracts import SegmentLog

# keyDown steps that carry no intent of their own. Enter is meaningful (it
# submits), Tab only moves focus.
NOISE_KEYS = {"Tab"}


def is_noise(click: SessionClick) -> bool:
    return click.type in ("keyDown", "keyUp") and click.key in NOISE_KEYS


def target_text(click: SessionClick) -> str:
    t = click.target
    if not t:
        return click.url or ""
    return t.aria_label or t.text or t.button_name or t.css or ""


def boundary_hints(session: Session) -> dict[int, list[str]]:
    """Structural hints about where segments start and end.

    These are hints for the prompt, not decisions: a write usually ends a
    segment, an action load or a change of model usually starts one, and an
    RPC error marks a failed attempt.
    """
    hints: dict[int, list[str]] = {}
    last_model: str | None = None

    for click in session.clicks:
        marks: list[str] = []
        models = [c.model for c in click.calls if c.model]

        if any(c.kind == "action_load" for c in click.calls):
            marks.append("screen-load")
        if models and last_model and models[0] != last_model:
            marks.append(f"model-change->{models[0]}")
        if click.has_write:
            marks.append("WRITE")
        if any(c.rpc_error for c in click.calls):
            marks.append("ERROR")

        if marks:
            hints[click.step_index] = marks
        if models:
            last_model = models[-1]

    return hints


def render_step(click: SessionClick, hints: dict[int, list[str]]) -> str:
    """One compact line per step, as the model sees it."""
    calls = ", ".join(
        f"{c.kind} {c.model or '-'}.{c.method or '-'}" for c in click.calls
    ) or "-"
    value = click.value or click.key or ""
    parts = [
        f"{click.step_index:>3}",
        f"{click.type:<11}",
        f'"{target_text(click)[:46]}"',
    ]
    if value:
        parts.append(f"= {value[:20]!r}")
    parts.append(f"| {calls[:120]}")
    if click.step_index in hints:
        parts.append(f"| {' '.join(hints[click.step_index])}")
    errors = [c.rpc_error for c in click.calls if c.rpc_error]
    if errors:
        parts.append(f'| error: "{errors[0][:90]}"')
    return " ".join(parts)


def render_session(session: Session) -> str:
    """The whole session as text for the prompt, noise dropped."""
    hints = boundary_hints(session)
    lines = [
        render_step(c, hints) for c in session.clicks if not is_noise(c)
    ]
    header = (
        f"Session: {session.source}\n"
        f"Title: {session.title or '-'}\n"
        f"Steps shown: {len(lines)}\n"
    )
    return header + "\n" + "\n".join(lines)


def expected_indexes(session: Session, include_noise: bool = False) -> list[int]:
    return [
        c.step_index for c in session.clicks if include_noise or not is_noise(c)
    ]


def validate_segment_log(log: SegmentLog, session: Session) -> list[str]:
    """Check the model's segmentation against the session. Empty list = valid."""
    problems: list[str] = []
    want = expected_indexes(session)
    got = [i for s in log.segments for i in s.step_indexes]

    if not log.segments:
        return ["No segments returned."]

    seen: set[int] = set()
    duplicates = sorted({i for i in got if i in seen or seen.add(i)})
    if duplicates:
        problems.append(f"Steps assigned to more than one segment: {duplicates}")

    unknown = sorted(set(got) - set(want))
    if unknown:
        problems.append(f"Step indexes that are not in the session: {unknown}")

    missing = sorted(set(want) - set(got))
    if missing:
        problems.append(f"Steps not assigned to any segment: {missing}")

    if got != sorted(got):
        problems.append("Step indexes are not in increasing order across segments.")

    for seg in log.segments:
        if not seg.step_indexes:
            problems.append(f"{seg.segment_id} has no steps.")
        elif seg.step_indexes != sorted(seg.step_indexes):
            problems.append(f"{seg.segment_id} lists its steps out of order.")

    return problems


def reattach_noise(log: SegmentLog, session: Session) -> SegmentLog:
    """Put dropped noise steps back, in the segment of the step before them.

    The model never sees noise steps, but the contract says every step in the
    session belongs to exactly one segment.
    """
    owner: dict[int, str] = {
        i: seg.segment_id for seg in log.segments for i in seg.step_indexes
    }
    additions: dict[str, list[int]] = {}
    previous: int | None = None

    for click in session.clicks:
        if is_noise(click) and previous is not None:
            segment_id = owner.get(previous)
            if segment_id:
                additions.setdefault(segment_id, []).append(click.step_index)
        else:
            previous = click.step_index

    if not additions:
        return log

    segments = [
        seg.model_copy(
            update={"step_indexes": sorted(seg.step_indexes + additions.get(seg.segment_id, []))}
        )
        for seg in log.segments
    ]
    return log.model_copy(update={"segments": segments})


def build_segmenter(model: str | None = None):
    """The LangChain agent. One focused judgement, validated structured output."""
    from langchain.agents import create_agent

    return create_agent(
        model=model or chat_model("segmenter"),
        tools=[],
        system_prompt=load_prompt("segmenter"),
        response_format=SegmentLog,
        name="segmenter_agent",
    )


def run_segmenter(
    session: Session,
    agent: Any = None,
    run: str = "adhoc",
    retries: int = 1,
) -> SegmentLog:
    """Segment a session. Retries once with the validation errors appended.

    `agent` is injectable so tests can run without an API key.
    """
    agent = agent or build_segmenter()
    rendered = render_session(session)
    messages = [{"role": "user", "content": rendered}]
    problems: list[str] = []

    for attempt in range(retries + 1):
        result = agent.invoke(
            {"messages": messages},
            config=trace_config(run, "segmenter", attempt=attempt),
        )
        log = result["structured_response"]
        if not isinstance(log, SegmentLog):
            log = SegmentLog.model_validate(log)
        log = log.model_copy(update={"session": session.source})

        problems = validate_segment_log(log, session)
        if not problems:
            return reattach_noise(log, session)

        messages = messages + [
            {"role": "assistant", "content": log.model_dump_json()},
            {
                "role": "user",
                "content": (
                    "That segmentation is not valid:\n- "
                    + "\n- ".join(problems)
                    + "\n\nReturn the whole segmentation again, corrected."
                ),
            },
        ]

    raise ValueError("segmenter produced an invalid segmentation:\n- " + "\n- ".join(problems))


def run_segmenter_path(session_path: Path, out: Path, agent: Any = None, run: str = "adhoc") -> SegmentLog:
    session = Session.model_validate_json(Path(session_path).read_text(encoding="utf-8"))
    log = run_segmenter(session, agent=agent, run=run)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    return log
