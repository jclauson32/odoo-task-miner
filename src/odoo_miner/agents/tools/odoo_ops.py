"""Tools that drive Odoo and the recording pipeline.

Thin wrappers over commands that already work by hand. Every one returns text
the agent can read, including the failure, rather than raising.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from ..config import settings

COMPOSE = ["docker", "compose"]
DEFAULT_TIMEOUT = 900


def _run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT, cwd: Optional[Path] = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None
        )
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {shlex.join(cmd)}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def install_module(name: str) -> str:
    """Install or update an Odoo module in the demo database, then restart Odoo.

    Args:
        name: module directory name under addons/, e.g. "odoo_miner_bill_date".

    Returns:
        "ok" plus the tail of Odoo's log, or the failure output.
    """
    s = settings()
    code, output = _run(
        COMPOSE + ["run", "--rm", "odoo", "odoo", "-d", s.odoo_db, "-i", name, "--stop-after-init"]
    )
    if code != 0:
        return f"install failed (exit {code}):\n{output[-4000:]}"
    restart_code, restart_output = _run(COMPOSE + ["restart", "odoo"], timeout=180)
    if restart_code != 0:
        return f"module installed but Odoo restart failed:\n{restart_output[-2000:]}"
    return f"ok: {name} installed and Odoo restarted.\n{output[-1500:]}"


def run_module_tests(name: str, run_dir: Optional[str] = None) -> str:
    """Run an Odoo module's tests and save the full output.

    Args:
        name: module directory name under addons/.
        run_dir: where to write tests.log. Defaults to the module's own folder.

    Returns:
        Whether the tests passed, with the failing lines when they did not.
    """
    s = settings()
    code, output = _run(
        COMPOSE + ["run", "--rm", "odoo", "odoo", "-d", s.odoo_db, "-i", name,
                   "--test-tags", f"/{name}", "--stop-after-init"]
    )
    log_dir = Path(run_dir) if run_dir else (Path.cwd() / s.addons_dir / name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tests.log"
    log_path.write_text(output, encoding="utf-8")

    # Odoo reports test failures in its log; a zero exit code alone is not proof.
    failed = [
        line for line in output.splitlines()
        if "FAIL:" in line or "ERROR:" in line or "Module loading failed" in line
    ]
    if code != 0 or failed:
        return (
            f"tests FAILED (exit {code}); full log at {log_path}\n"
            + "\n".join(failed[-25:] or output.splitlines()[-25:])
        )
    return f"tests passed; log at {log_path}"


def replay_workflow(recording_path: str, run_dir: str, module: Optional[str] = None) -> str:
    """Restore the database, optionally install a module, and replay a recording.

    Args:
        recording_path: a Chrome Recorder JSON file.
        run_dir: folder for the replay's artifacts (clicks/network/session).
        module: module to install after the restore, before replaying.

    Returns:
        Whether the replay completed, and where the artifacts are.
    """
    if module:
        installed = install_module(module)
        if installed.startswith(("install failed", "module installed but")):
            return f"did not replay: {installed}"

    code, output = _run(
        ["odoo-miner", "run", recording_path, "-d", run_dir,
         "--pre-hook", "./scripts/restore_db.sh", "--screenshots"],
        timeout=DEFAULT_TIMEOUT,
    )
    session = Path(run_dir) / "session.json"
    if not session.exists():
        return f"replay produced no session.json (exit {code}):\n{output[-3000:]}"

    data = json.loads(session.read_text(encoding="utf-8"))
    completed = data.get("replay_completed")
    note = "completed" if completed else "STOPPED before the end"
    return f"replay {note}; artifacts in {run_dir}\n{output[-1500:]}"


def measure_effort(run_dir: str) -> str:
    """Segment, trace and assess a finished run, and report its total effort.

    Args:
        run_dir: a folder containing session.json.

    Returns:
        The total effort, and the per-segment efforts.
    """
    from ..contracts import SegmentLog
    from ...models import Session
    from ..assessor import assess_deterministic
    from ..segmenter import run_segmenter

    path = Path(run_dir) / "session.json"
    if not path.exists():
        return f"no session.json in {run_dir}; replay it first."

    session = Session.model_validate_json(path.read_text(encoding="utf-8"))
    segments_path = Path(run_dir) / "segments.json"
    if segments_path.exists():
        segments = SegmentLog.model_validate_json(segments_path.read_text(encoding="utf-8"))
    else:
        segments = run_segmenter(session)
        segments_path.write_text(segments.model_dump_json(indent=2), encoding="utf-8")

    assessment = assess_deterministic(session, segments)
    (Path(run_dir) / "assessment.json").write_text(
        assessment.model_dump_json(indent=2), encoding="utf-8"
    )
    per_segment = ", ".join(f"{a.segment_id}={a.effort:g}" for a in assessment.segments)
    return f"total effort {assessment.total_effort:g} over {len(assessment.segments)} segments ({per_segment})"


TOOLS = [install_module, run_module_tests, replay_workflow, measure_effort]
