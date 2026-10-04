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
from typing import Optional

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
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
    recording: Path, out: Path, script: Path, headless: bool, cookie: Optional[str],
    pre_hook: Optional[str], timeout_ms: int, settle_ms: int, chrome: Optional[str],
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
    return log


def _do_merge(clicks: ClickLog, network: Optional[NetworkLog], out: Path) -> Session:
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
    cookie: Optional[str] = typer.Option(None, help="Session cookie to set before replay, e.g. 'session_id=abc123'."),
    pre_hook: Optional[str] = typer.Option(
        None, help="Shell command to run first, e.g. a database restore. Replay aborts if it fails."
    ),
    timeout_ms: int = typer.Option(10000, "--timeout", help="Per-step timeout in milliseconds."),
    settle_ms: int = typer.Option(500, "--settle", help="Network idle time to wait for after each step."),
    chrome: Optional[str] = typer.Option(None, help="Path to a Chrome/Chromium executable."),
):
    """Replay a recording with Puppeteer and capture Odoo backend calls per step.

    Replaying repeats every write in the recording. Restore your database first
    (see --pre-hook) or the second run will fail or duplicate records.
    """
    _do_replay(recording, out, script, headless, cookie, pre_hook, timeout_ms, settle_ms, chrome)


@app.command()
def merge(
    clicks: Path = typer.Argument(..., help="Output of `ingest`."),
    network: Optional[Path] = typer.Argument(None, help="Output of `replay` (optional)."),
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
    cookie: Optional[str] = typer.Option(None),
    pre_hook: Optional[str] = typer.Option(None),
    timeout_ms: int = typer.Option(10000, "--timeout"),
    settle_ms: int = typer.Option(500, "--settle"),
    chrome: Optional[str] = typer.Option(None),
    keep_noise: bool = typer.Option(False),
):
    """Ingest, replay and merge in one go. Writes clicks.json, network.json and session.json."""
    click_log = _do_ingest(recording, out_dir / "clicks.json", keep_noise)
    net_log = None
    if not skip_replay:
        net_log = _do_replay(
            recording, out_dir / "network.json", script, headless, cookie,
            pre_hook, timeout_ms, settle_ms, chrome,
        )
    _do_merge(click_log, net_log, out_dir / "session.json")


@app.command()
def show(path: Path = typer.Argument(..., help="clicks.json or session.json")):
    """Print a click log or session as a table."""
    data = json.loads(path.read_text(encoding="utf-8"))
    is_session = "unattributed_calls" in data
    log = Session.model_validate(data) if is_session else ClickLog.model_validate(data)

    table = Table(title=log.title or str(path))
    table.add_column("#", justify="right")
    table.add_column("Type")
    table.add_column("Target")
    table.add_column("Value")
    table.add_column("Screen")
    if is_session:
        table.add_column("Backend calls", overflow="fold")

    for c in log.clicks:
        t = c.target
        target = (t.aria_label or t.text or t.button_name or t.css or "") if t else (c.url or "")
        screen = " ".join(filter(None, [c.page.model, str(c.page.record_id or "") or None, c.page.view_type])) or (
            "/".join(c.page.path_slugs)
        )
        row = [str(c.index), c.type, target[:50], (c.value or c.key or "")[:30], screen]
        if is_session:
            row.append("\n".join(f"{k.kind}: {k.method}" for k in c.calls))
        table.add_row(*row)

    console.print(table)


if __name__ == "__main__":
    app()
