"""segmenter_agent: group clicks into segments that each accomplish one thing.

Noise, boundary hints and the compact rendering are computed here; the model
only decides where segments start and end, and what to call them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Session, SessionClick
from .config import chat_model, load_prompt, trace_config
from .contracts import SegmentLog

# Keys that only move focus. Enter is not one of them: it submits.
NOISE_KEYS = {"Tab"}


def is_noise(click: SessionClick) -> bool:
    """Whether a step is a keypress that only moves focus."""
    return click.type in ("keyDown", "keyUp") and click.key in NOISE_KEYS


def boundary_hints(session: Session) -> dict[int, list[str]]:
    """Hints for the prompt about where segments may start and end.

    Screen loads, model changes, writes and RPC errors are marked per step.
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
        f'"{click.target_label()[:46]}"',
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
    """The step indexes a segmentation has to cover."""
    return [
        c.step_index for c in session.clicks if include_noise or not is_noise(c)
    ]


def validate_segment_log(log: SegmentLog, session: Session) -> list[str]:
    """Problems with a segmentation; an empty list means it is valid."""
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
    """Put the dropped noise steps back, each in the segment of the step before it."""
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
    """The segmenter agent: one judgement, returned as a validated SegmentLog."""
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
    """Segment a session, retrying once with the validation errors if it is invalid.

    `agent` can be a stand-in, so tests need no API key.
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
    """Segment a session file and write segments.json."""
    session = Session.model_validate_json(Path(session_path).read_text(encoding="utf-8"))
    log = run_segmenter(session, agent=agent, run=run)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    return log
