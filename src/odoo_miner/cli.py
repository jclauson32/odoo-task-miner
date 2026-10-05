"""odoo-miner command line.

    odoo-miner ingest recording.json            -> clicks.json
    odoo-miner replay recording.json            -> network.json  (Node + Puppeteer)
    odoo-miner merge clicks.json network.json   -> session.json
    odoo-miner run recording.json               -> all three, into one folder
    odoo-miner show clicks.json|session.json    -> readable table
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .merge import merge as merge_logs
from .models import ClickLog, NetworkLog, Session
from .recorder import RecordingError, load_recording

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Parse Odoo workflow recordings for the analysis agents.")
console = Console()
err = Console(stderr=True)

DEFAULT_SCRIPT = Path(__file__).resolve().parents[2] / "replay" / "capture.mjs"


def _write(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def _fail(message: str) -> None:
    err.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=1)


def _load(path: Path, model: type[BaseModel]):
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"{path} not found.")
    except ValidationError as exc:
        _fail(f"{path} is not a valid {model.__name__} file:\n{exc}")


def _do_ingest(recording: Path, out: Path, keep_noise: bool) -> ClickLog:
    try:
        log = load_recording(recording, keep_noise=keep_noise)
    except FileNotFoundError:
        _fail(f"{recording} not found.")
    except RecordingError as exc:
        _fail(str(exc))
    _write(log, out)
    console.print(f"[green]✓[/green] {len(log.clicks)} steps → {out}")
    return log


def _do_replay(
    recording: Path, out: Path, script: Path, headless: bool, cookie: str | None,
    pre_hook: str | None, timeout_ms: int, settle_ms: int, chrome: str | None,
    screenshots: Path | None = None,
) -> NetworkLog:
    if not script.exists():
        _fail(f"Replay script not found at {script}. Pass --script or set ODOO_MINER_REPLAY.")
    if shutil.which("node") is None:
        _fail("Node.js is required for replay but 'node' was not found on PATH.")
    if not (script.parent / "node_modules").exists():
        _fail(f"Replay dependencies missing. Run:  cd {script.parent} && npm install")

    if pre_hook:
        console.print(f"Running pre-hook: {pre_hook}")
        result = subprocess.run(pre_hook, shell=True)
        if result.returncode != 0:
            _fail(f"Pre-hook failed with exit code {result.returncode}; not replaying.")

    cmd = [
        "node", str(script),
        "--recording", str(recording.resolve()),
        "--out", str(out.resolve()),
        "--timeout", str(timeout_ms),
        "--settle", str(settle_ms),
    ]
    if headless:
        cmd.append("--headless")
    if cookie:
        cmd += ["--cookie", cookie]
    if chrome:
        cmd += ["--chrome", chrome]
    if screenshots:
        cmd += ["--screenshots", str(screenshots.resolve())]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)  # never mistake an old run's output for this one
    console.print(f"Replaying {recording} …")
    result = subprocess.run(cmd)
    if not out.exists():
        _fail(f"Replay produced no output (exit code {result.returncode}).")

    log = _load(out, NetworkLog)
    if log.completed:
        console.print(f"[green]✓[/green] {len(log.calls)} backend calls → {out}")
    else:
        err.print(
            f"[yellow]Replay stopped at step {log.failed_step}: {log.error}[/yellow]\n"
            f"Partial capture saved ({len(log.calls)} calls) → {out}"
        )
        if log.failure_screenshot:
            err.print(f"What the page looked like when it stopped: {log.failure_screenshot}")
    return log


def _do_merge(clicks: ClickLog, network: NetworkLog | None, out: Path) -> Session:
    session = merge_logs(clicks, network)
    _write(session, out)
    writes = sum(1 for c in session.clicks if c.has_write)
    console.print(f"[green]✓[/green] {len(session.clicks)} steps, {writes} with backend writes → {out}")
    return session


@app.command()
def ingest(
    recording: Path = typer.Argument(..., help="Chrome DevTools Recorder export (.json)."),
    out: Path = typer.Option(Path("clicks.json"), "--out", "-o", help="Where to write the click log."),
    keep_noise: bool = typer.Option(False, help="Keep keyUp steps, which normally duplicate keyDown."),
):
    """Parse a Recorder export into a normalized click log."""
    _do_ingest(recording, out, keep_noise)


ReplayScript = typer.Option(
    Path(os.environ.get("ODOO_MINER_REPLAY", DEFAULT_SCRIPT)), "--script", help="Path to capture.mjs."
)


@app.command()
def replay(
    recording: Path = typer.Argument(..., help="Chrome DevTools Recorder export (.json)."),
    out: Path = typer.Option(Path("network.json"), "--out", "-o"),
    script: Path = ReplayScript,
    headless: bool = typer.Option(True, help="Run Chrome without a window."),
    cookie: str | None = typer.Option(None, help="Session cookie to set before replay, e.g. 'session_id=abc123'."),
    pre_hook: str | None = typer.Option(
        None, help="Shell command to run first, e.g. a database restore. Replay aborts if it fails."
    ),
    timeout_ms: int = typer.Option(10000, "--timeout", help="Per-step timeout in milliseconds."),
    settle_ms: int = typer.Option(500, "--settle", help="Network idle time to wait for after each step."),
    chrome: str | None = typer.Option(None, help="Path to a Chrome/Chromium executable."),
    screenshots: Path | None = typer.Option(None, help="Folder to save a screenshot after every step."),
):
    """Replay a recording with Puppeteer and capture Odoo backend calls per step.

    Replaying repeats every write in the recording. Restore your database first
    (see --pre-hook) or the second run will fail or duplicate records.
    """
    _do_replay(recording, out, script, headless, cookie, pre_hook, timeout_ms, settle_ms, chrome, screenshots)


@app.command()
def merge(
    clicks: Path = typer.Argument(..., help="Output of `ingest`."),
    network: Path | None = typer.Argument(None, help="Output of `replay` (optional)."),
    out: Path = typer.Option(Path("session.json"), "--out", "-o"),
):
    """Attach captured backend calls to the steps that triggered them."""
    click_log = _load(clicks, ClickLog)
    net_log = _load(network, NetworkLog) if network else None
    _do_merge(click_log, net_log, out)


@app.command()
def run(
    recording: Path = typer.Argument(..., help="Chrome DevTools Recorder export (.json)."),
    out_dir: Path = typer.Option(Path("out"), "--out-dir", "-d"),
    skip_replay: bool = typer.Option(False, help="Only ingest; no network capture."),
    script: Path = ReplayScript,
    headless: bool = typer.Option(True),
    cookie: str | None = typer.Option(None),
    pre_hook: str | None = typer.Option(None),
    timeout_ms: int = typer.Option(10000, "--timeout"),
    settle_ms: int = typer.Option(500, "--settle"),
    chrome: str | None = typer.Option(None),
    screenshots: bool = typer.Option(False, help="Save a screenshot after every replay step into OUT_DIR/screenshots."),
    keep_noise: bool = typer.Option(False),
):
    """Ingest, replay and merge in one go. Writes clicks.json, network.json and session.json."""
    click_log = _do_ingest(recording, out_dir / "clicks.json", keep_noise)
    net_log = None
    if not skip_replay:
        net_log = _do_replay(
            recording, out_dir / "network.json", script, headless, cookie,
            pre_hook, timeout_ms, settle_ms, chrome,
            out_dir / "screenshots" if screenshots else None,
        )
    _do_merge(click_log, net_log, out_dir / "session.json")


@app.command()
def show(
    path: Path = typer.Argument(..., help="clicks.json or session.json"),
    first: int | None = typer.Option(None, "--from", help="First step number to show."),
    last: int | None = typer.Option(None, "--to", help="Last step number to show."),
):
    """Print any pipeline file as a table: clicks, session, segments, traces, assessment or plan."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for looks_like, render in _ARTIFACT_VIEWS:
        if looks_like(data):
            render(data, path)
            return
    is_session = "unattributed_calls" in data
    log = Session.model_validate(data) if is_session else ClickLog.model_validate(data)
    clicks = [
        c for c in log.clicks
        if (first is None or c.step_index >= first) and (last is None or c.step_index <= last)
    ]

    def screen(c) -> str:
        parts = [c.page.model, str(c.page.record_id or "") or None, c.page.view_type]
        return " ".join(filter(None, parts)) or "/".join(c.page.path_slugs)

    # Odoo 18 rarely puts the screen in the URL; drop the column when it is empty.
    show_screen = any(screen(c) for c in clicks)

    table = Table(title=log.title or str(path))
    table.add_column("Step", justify="right")
    table.add_column("Type")
    table.add_column("Target")
    table.add_column("Value")
    if show_screen:
        table.add_column("Screen")
    if is_session:
        table.add_column("Backend calls", overflow="fold")

    for c in clicks:
        t = c.target
        target = (t.aria_label or t.text or t.button_name or t.css or "") if t else (c.url or "")
        row = [str(c.step_index), c.type, escape(target[:50]), escape((c.value or c.key or "")[:30])]
        if show_screen:
            row.append(escape(screen(c)))
        if is_session:
            calls = []
            for k in c.calls:
                name = f"{k.model}.{k.method}" if k.model else (k.method or k.endpoint)
                calls.append(escape(f"{k.kind} {name}"))
                if k.rpc_error:
                    calls.append(f"[red]error: {escape(k.rpc_error.strip()[:80])}[/red]")
            row.append("\n".join(calls))
        table.add_row(*row)

    console.print(table)


# ---------------------------------------------------------------- analysis stages
#
# Each of these is also a node in the LangGraph pipeline (see
# odoo_miner/pipeline/graph.py); the function underneath is the same one. The
# agent libraries are imported inside the commands so that `ingest`, `replay`
# and `merge` stay fast and keep working without them.


def _session_arg(path: Path) -> Path:
    if not path.exists():
        _fail(f"{path} not found. Run `odoo-miner run` first.")
    return path


def _resolve_segments(path: Path) -> Path:
    """Accept segments.json, or traces.json and find segments.json beside it.

    Scoring needs each segment's step indexes, which only segments.json
    carries, so a traces.json argument is resolved to its sibling.
    """
    if path.name == "traces.json":
        sibling = path.with_name("segments.json")
        if not sibling.exists():
            _fail(f"{path} has no segments.json beside it; run `odoo-miner segment` first.")
        return sibling
    return path


@app.command()
def segment(
    session: Path = typer.Argument(..., help="Output of `run`/`merge` (session.json)."),
    out: Path = typer.Option(Path("segments.json"), "--out", "-o"),
    run_name: str = typer.Option("adhoc", "--run", help="Run name, used in LangSmith metadata."),
):
    """Group a session's clicks into segments that each accomplish one thing."""
    from .agents.segmenter import run_segmenter_path

    log = run_segmenter_path(_session_arg(session), out, run=run_name)
    console.print(f"[green]✓[/green] {len(log.segments)} segments → {out}")
    for seg in log.segments:
        console.print(f"  {seg.segment_id} {escape(f'[{seg.outcome}]')} {escape(seg.label)}")


@app.command()
def trace(
    segments: Path = typer.Argument(..., help="Output of `segment`."),
    session: Path = typer.Option(..., "--session", help="session.json for the same run."),
    out: Path = typer.Option(Path("traces.json"), "--out", "-o"),
    run_name: str = typer.Option("adhoc", "--run"),
):
    """Explain each segment against the Odoo code that ran."""
    from .agents.tracer import run_tracer_path

    log = run_tracer_path(segments, _session_arg(session), out, run=run_name)
    console.print(f"[green]✓[/green] {len(log.segments)} segments traced → {out}")
    for seg in log.segments:
        console.print(f"  {seg.segment_id} [{seg.kind}] {len(seg.actions)} action(s), {len(seg.retrievals)} lookup(s)")


@app.command()
def assess(
    segments: Path = typer.Argument(..., help="Output of `segment` (or `trace`; segments.json is found beside it)."),
    session: Path = typer.Option(..., "--session", help="session.json for the same run."),
    out: Path = typer.Option(Path("assessment.json"), "--out", "-o"),
    offline: bool = typer.Option(
        False, help="Deterministic scores only; no model call, so no API key needed."
    ),
    run_name: str = typer.Option("adhoc", "--run"),
):
    """Score how hard each step and segment was."""
    from .agents.assessor import run_assessor_path

    assessment = run_assessor_path(
        _resolve_segments(segments), _session_arg(session), out,
        run=run_name, explain=not offline,
    )
    console.print(
        f"[green]✓[/green] total effort {assessment.total_effort:g} "
        f"over {len(assessment.segments)} segments → {out}"
    )
    for seg in assessment.segments:
        friction = f"  [yellow]{escape('; '.join(seg.friction))}[/yellow]" if seg.friction else ""
        console.print(f"  {seg.segment_id} effort {seg.effort:g}{friction}")


@app.command()
def plan(
    run_dir: Path = typer.Argument(..., help="Run folder holding segments/traces/assessment."),
    out: Path | None = typer.Option(None, "--out", "-o"),
    run_name: str = typer.Option("adhoc", "--run"),
):
    """Decide whether the workflow is worth changing, and plan the change."""
    from .agents.planner import run_planner_path

    target = out or run_dir / "plan.json"
    result = run_planner_path(run_dir, target, run=run_name)
    console.print(f"[green]✓[/green] decision: [bold]{result.decision}[/bold] → {target}")
    console.print(escape(result.summary))
    _show_citation_check(result.unverified_citations)


def _show_pending(payload: dict) -> None:
    """Print what a pause is asking a person to approve."""
    from .pipeline.graph import is_tool_gate

    if is_tool_gate(payload):
        console.print("\n[yellow]The builder wants to send something out. Approve each action:[/yellow]")
        for request in payload["action_requests"]:
            console.print(f"  [bold]{escape(request.get('name', '?'))}[/bold]")
            for key, value in (request.get("args") or {}).items():
                text = str(value)
                console.print(f"    {key}: {escape(text[:300] + ('…' if len(text) > 300 else ''))}")
        return

    console.print("\n[yellow]Waiting for approval of the plan.[/yellow]")
    for key in ("decision", "plan_summary", "module_name", "plan_md"):
        if payload.get(key) is not None:
            console.print(f"  {key}: {escape(str(payload[key]))}")
    for key, label in (("acceptance_criteria", "acceptance criterion"), ("risks", "risk")):
        for item in payload.get(key) or []:
            console.print(f"  {label}: {escape(str(item))}")
    _show_citation_check(payload.get("unverified_citations") or [])


def _show_citation_check(problems: list[str]) -> None:
    if problems:
        console.print("  [yellow]Citations to check by hand before approving:[/yellow]")
        for problem in problems:
            console.print(f"    - {escape(problem)}")
    else:
        console.print("  [green]Every file and line the plan cites was found in the source.[/green]")


@app.command()
def analyze(
    run_dir: Path = typer.Argument(..., help="Run folder containing session.json."),
    until: str | None = typer.Option(
        None, "--until", help="Stop after this stage: segment, trace, assess, plan, approve, build."
    ),
    run_name: str | None = typer.Option(None, "--run", help="Defaults to the run folder's name."),
    thread: str | None = typer.Option(None, "--thread", help="Resume a run by thread id."),
    approve: bool | None = typer.Option(
        None, "--approve/--reject", help="Answer a pending approval and continue."
    ),
    notes: str = typer.Option("", "--notes", help="Notes to record with the approval."),
    restart: bool = typer.Option(
        False, "--restart", help="Start over instead of resuming an unfinished run on this thread."
    ),
):
    """Run the stages as one LangGraph pipeline, resumable and traced as one tree."""
    from langgraph.types import Command

    from .pipeline.graph import (
        build_graph,
        is_tool_gate,
        record_tool_decision,
        resume_value,
        sqlite_checkpointer,
        start_or_resume,
    )
    from .pipeline.state import STAGES

    if until is not None and until not in STAGES:
        _fail(f"Unknown stage {until!r}; expected one of {', '.join(STAGES)}.")

    session = run_dir / "session.json"
    _session_arg(session)
    name = run_name or run_dir.name
    thread_id = thread or name

    with sqlite_checkpointer(run_dir / "pipeline.sqlite") as checkpointer:
        graph = build_graph(checkpointer=checkpointer, until=until)
        config = {"configurable": {"thread_id": thread_id}}

        if approve is None:
            state = {
                "run": name,
                "run_dir": str(run_dir),
                "session_path": str(session),
            }
            unfinished = graph.get_state(config).next
            if unfinished and not restart:
                console.print(f"Resuming {thread_id} at {', '.join(unfinished)} (pass --restart to start over).")
            result = start_or_resume(graph, config, state, restart=restart)
        else:
            waiting = graph.get_state(config).interrupts
            if not waiting:
                _fail(f"Nothing is waiting for approval on thread {thread_id!r}.")
            payload = waiting[0].value
            if is_tool_gate(payload):
                record_tool_decision(payload, approve, notes, name)
            result = graph.invoke(
                Command(resume=resume_value(payload, approve, notes)), config=config
            )

    pending = result.get("__interrupt__")
    if pending:
        payload = pending[0].value if hasattr(pending[0], "value") else pending[0]
        _show_pending(payload)
        console.print(
            f"\nApprove with:  odoo-miner analyze {run_dir} --approve "
            f"--thread {thread_id}\nReject with:   odoo-miner analyze {run_dir} --reject "
            f"--thread {thread_id} --notes \"why\""
        )
        return

    written = [
        (label, result[key]) for label, key in [
            ("segments", "segments_path"), ("traces", "traces_path"),
            ("assessment", "assessment_path"), ("plan", "plan_path"),
            ("build", "build_path"),
        ] if result.get(key)
    ]
    console.print(f"[green]✓[/green] pipeline finished ({len(written)} artifact(s))")
    for label, path in written:
        console.print(f"  {label}: {path}")
    for problem in result.get("errors") or []:
        err.print(f"[yellow]{escape(problem)}[/yellow]")

@app.command()
def doctor():
    """Check this machine is ready: keys, Odoo, databases, source, replay, email, GitHub."""
    from .doctor import run_checks

    marks = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(width=1)
    table.add_column(style="bold")
    table.add_column(overflow="fold")
    results = run_checks()
    for check in results:
        detail = escape(check.detail) + (f"\n[dim]→ {escape(check.fix)}[/dim]" if check.fix and check.status != "ok" else "")
        table.add_row(marks[check.status], check.name, detail)
    console.print(table)
    failed = [check for check in results if check.status == "fail"]
    if failed:
        err.print(f"\n[red]{len(failed)} check(s) failed.[/red]")
        raise typer.Exit(code=1)
    console.print("\n[green]Ready.[/green]")


@app.command()
def report(
    run_dir: Path = typer.Argument(..., help="Run folder with plan.json (and screenshots/, if replayed with --screenshots)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Send without asking for confirmation."),
):
    """Email the findings - the plan and screenshots of where the user got stuck - to REPORT_EMAIL_TO."""
    import os

    from .agents.config import settings
    from .agents.reporting import compose_report
    from .agents.tools.delivery import send_report_email

    settings()
    try:
        subject, body, attachments = compose_report(run_dir)
    except FileNotFoundError as exc:
        _fail(str(exc))
    to = os.environ.get("REPORT_EMAIL_TO") or "(REPORT_EMAIL_TO not set)"
    console.print(f"[bold]To:[/bold] {escape(to)}\n[bold]Subject:[/bold] {escape(subject)}")
    for path in attachments:
        console.print(f"  [dim]attach[/dim] {escape(path)}")
    if not yes and not typer.confirm("Send it?", default=False):
        console.print("Not sent.")
        raise typer.Exit(code=1)
    result = send_report_email(subject, body, attachments)
    if result.startswith("sent"):
        console.print(f"[green]✓[/green] {escape(result)}")
    else:
        _fail(result)


# ---------------------------------------------------------------- show: agent artifacts


def _show_segments(data: dict, path: Path) -> None:
    table = Table(title=f"Segments - {data.get('session') or path}")
    for column, justify in [("Segment", "left"), ("Steps", "right"), ("Outcome", "left"), ("What the user did", "left")]:
        table.add_column(column, justify=justify)
    for seg in data["segments"]:
        steps = seg["step_indexes"]
        outcome = seg.get("outcome", "completed")
        color = {"failed": "red", "recovered": "yellow", "abandoned": "red"}.get(outcome, "green")
        span = f"{steps[0]}-{steps[-1]}" if steps else "-"
        table.add_row(seg["segment_id"], span, f"[{color}]{outcome}[/{color}]", escape(seg["label"]))
    console.print(table)


def _show_traces(data: dict, path: Path) -> None:
    for seg in data["segments"]:
        console.print(f"[bold]{seg['segment_id']}[/bold] [dim]{seg['kind']}[/dim]")
        for ref in seg.get("actions", []):
            console.print(f"  [cyan]{escape(ref['symbol'])}[/cyan]  {escape(ref['file'])}:{ref['line']}")
        if seg.get("explanation"):
            console.print(f"  {escape(seg['explanation'])}", soft_wrap=True)
        console.print()


def _show_assessment(data: dict, path: Path) -> None:
    table = Table(title=f"Effort by segment - total {data.get('total_effort', 0):g}")
    table.add_column("Segment")
    table.add_column("Effort", justify="right")
    table.add_column("Hardest step", justify="right")
    table.add_column("Friction")
    for seg in data["segments"]:
        hardest = max(seg.get("steps", []), key=lambda s: s["score"], default=None)
        table.add_row(
            seg["segment_id"], f"{seg['effort']:g}",
            f"{hardest['step_index']} ({hardest['score']})" if hardest else "-",
            escape("; ".join(seg.get("friction", []))),
        )
    console.print(table)


def _show_plan(data: dict, path: Path) -> None:
    console.print(f"[bold]Decision:[/bold] {escape(data['decision'].replace('_', ' '))}")
    if data.get("module_name"):
        console.print(f"[bold]Module:[/bold] {escape(data['module_name'])}")
    console.print(f"\n{escape(data['summary'])}\n", soft_wrap=True)
    for key, title in [("acceptance_criteria", "Acceptance criteria"), ("risks", "Risks")]:
        if data.get(key):
            console.print(f"[bold]{title}[/bold]")
            for item in data[key]:
                console.print(f"  - {escape(item)}", soft_wrap=True)
    _show_citation_check(data.get("unverified_citations") or [])


def _is_segments(data: dict) -> bool:
    first = (data.get("segments") or [{}])[0]
    return "step_indexes" in first


def _is_traces(data: dict) -> bool:
    first = (data.get("segments") or [{}])[0]
    return "kind" in first and "actions" in first


_ARTIFACT_VIEWS = [
    (lambda d: "decision" in d and "summary" in d, _show_plan),
    (lambda d: "total_effort" in d, _show_assessment),
    (_is_traces, _show_traces),
    (_is_segments, _show_segments),
]


if __name__ == "__main__":
    app()
