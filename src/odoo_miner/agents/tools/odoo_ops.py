"""Tools that drive Odoo and the recording pipeline.

Thin wrappers over commands that already work by hand. Every one returns text
the agent can read, including the failure, rather than raising.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import settings

DEFAULT_TIMEOUT = 900
# docker-compose.yml and scripts/ live at the repository root; run from there
# whatever the caller's working directory is.
REPO_ROOT = Path(__file__).resolve().parents[4]
RESTORE_HOOK = str(REPO_ROOT / "scripts" / "restore_db.sh")
# The same scripts a person runs, so the builder installs and tests a module
# exactly the way the demo and the runbook do.
INSTALL_SCRIPT = str(REPO_ROOT / "scripts" / "install_module.sh")
TEST_SCRIPT = str(REPO_ROOT / "scripts" / "test_module.sh")


def _run(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    cwd: Path | None = REPO_ROOT,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {shlex.join(cmd)}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def install_module(name: str) -> str:
    """Install or update an Odoo module in the demo database, then restart Odoo.

    Returns once Odoo answers again, so the next tool never races its start-up.

    Args:
        name: module directory name under addons/, e.g. "odoo_miner_bill_date".

    Returns:
        "ok" plus the tail of Odoo's log, or the failure output.
    """
    code, output = _run(["bash", INSTALL_SCRIPT, name], env={"DB": settings().odoo_db})
    if code != 0:
        return f"install failed (exit {code}):\n{output[-4000:]}"
    return f"ok: {name} installed and Odoo restarted.\n{output[-1500:]}"


# Odoo's own summary: "0 failed, 0 error(s) of 3 tests when loading database 'demo'".
_TEST_RESULT = re.compile(r"(\d+) failed, (\d+) error\(s\) of (\d+) tests")


def run_module_tests(name: str, run_dir: str | None = None) -> str:
    """Run an Odoo module's tests and save the full output.

    Installs the module if needed and updates it if not (`-i` and `-u`), so
    its tests run whether or not they are tagged post_install. Passing needs
    Odoo's own result line with at least one test: a run where nothing was
    tested is reported as a failure, not a pass.

    Args:
        name: module directory name under addons/.
        run_dir: where to write tests.log. Defaults to out/<name>/, never the module
            itself, so the log cannot end up in a commit.

    Returns:
        Whether the tests passed and how many ran, with the failing lines when not.
    """
    code, output = _run(["bash", TEST_SCRIPT, name], env={"DB": settings().odoo_db})
    log_dir = Path(run_dir) if run_dir else (REPO_ROOT / "out" / name)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tests.log"
    log_path.write_text(output, encoding="utf-8")

    results = _TEST_RESULT.findall(output)
    failed = [
        line for line in output.splitlines()
        if "FAIL:" in line or "ERROR:" in line or "Module loading failed" in line
    ]
    if code != 0 or failed:
        return (
            f"tests FAILED (exit {code}); full log at {log_path}\n"
            + "\n".join(failed[-25:] or output.splitlines()[-25:])
        )
    if not results or int(results[-1][2]) == 0:
        return (
            f"tests FAILED: no tests ran for {name}. Add a tests/ package that imports "
            f"its test modules, with at least one TransactionCase. Log at {log_path}"
        )
    bad, errors, total = (int(n) for n in results[-1])
    if bad or errors:
        return f"tests FAILED: {bad} failed, {errors} error(s) of {total}; log at {log_path}"
    return f"tests passed: {total} test(s) ran; log at {log_path}"


def replay_workflow(
    recording_path: str, run_dir: str, module: str | None = None,
    original_recording: str | None = None,
) -> str:
    """Restore the database, install a module, then replay a recording.

    In that order: restoring after the install would remove the module, and
    the replay would measure the workflow without the change.

    Args:
        recording_path: a Chrome Recorder JSON file.
        run_dir: folder for the replay's artifacts (clicks/network/session).
        module: module to install after the restore, before replaying.
        original_recording: the recording redacted values come from; they are
            restored into a temporary copy that is deleted after the replay.

    Returns:
        Whether the replay completed, and where the artifacts are.
    """
    from ...recorder import RecordingError, rehydrate_secrets

    code, output = _run(["bash", RESTORE_HOOK], timeout=600)
    if code != 0:
        return f"did not replay: restoring the database failed:\n{output[-2000:]}"
    if module:
        installed = install_module(module)
        if installed.startswith("install failed"):
            return f"did not replay: {installed}"

    try:
        recording = json.loads(Path(recording_path).read_text(encoding="utf-8"))
        if original_recording:
            original = json.loads(Path(original_recording).read_text(encoding="utf-8"))
            recording = rehydrate_secrets(recording, original)
    except (OSError, json.JSONDecodeError, RecordingError) as exc:
        return f"did not replay: {exc}"

    with tempfile.TemporaryDirectory(prefix="odoo-miner-replay-") as tmp:
        replayable = Path(tmp) / Path(recording_path).name
        replayable.write_text(json.dumps(recording), encoding="utf-8")
        # The same interpreter and package that are running now, not whatever
        # `odoo-miner` happens to be first on PATH. No pre-hook: restored above.
        code, output = _run(
            [sys.executable, "-m", "odoo_miner.cli", "run", str(replayable), "-d", run_dir,
             "--screenshots"],
            timeout=DEFAULT_TIMEOUT,
        )
    session = Path(run_dir) / "session.json"
    if not session.exists():
        return f"replay produced no session.json (exit {code}):\n{output[-3000:]}"

    data = json.loads(session.read_text(encoding="utf-8"))
    note = "completed" if data.get("replay_completed") else "STOPPED before the end"
    return f"replay {note}; artifacts in {run_dir}\n{output[-1500:]}"


def _effort(run_dir: Path, segment_if_missing: bool) -> tuple[float, int]:
    """Deterministic effort and step count for one run."""
    from ...models import Session
    from ..assessor import assess_deterministic
    from ..contracts import SegmentLog
    from ..segmenter import run_segmenter

    session = Session.model_validate_json((run_dir / "session.json").read_text(encoding="utf-8"))
    segments_path = run_dir / "segments.json"
    if segments_path.exists():
        segments = SegmentLog.model_validate_json(segments_path.read_text(encoding="utf-8"))
    elif segment_if_missing:
        segments = run_segmenter(session)
        segments_path.write_text(segments.model_dump_json(indent=2), encoding="utf-8")
    else:
        raise FileNotFoundError(f"{segments_path} not found")
    assessment = assess_deterministic(session, segments)
    (run_dir / "assessment.deterministic.json").write_text(
        assessment.model_dump_json(indent=2), encoding="utf-8"
    )
    return assessment.total_effort, len(session.clicks)


def measure_effort(run_dir: str, before_dir: str | None = None) -> str:
    """Effort for a replayed run, and the change from a run before it.

    Both sides are scored the same way - the deterministic signals, without
    the model's adjustments - so the difference is the workflow's, not the
    scorer's. The after-run is segmented first if it has not been.

    Args:
        run_dir: a folder containing session.json (the after-run).
        before_dir: the original run to compare with.

    Returns:
        Effort and step counts, before and after.
    """
    after_path = Path(run_dir)
    if not (after_path / "session.json").exists():
        return f"no session.json in {run_dir}; replay it first."
    after, after_steps = _effort(after_path, segment_if_missing=True)
    if not before_dir:
        return f"effort {after:g} over {after_steps} steps"
    before, before_steps = _effort(Path(before_dir), segment_if_missing=True)
    change = (after - before) / before * 100 if before else 0.0
    return (
        f"effort before {before:g} ({before_steps} steps), after {after:g} "
        f"({after_steps} steps): {change:+.0f}%"
    )


TOOLS = [install_module, run_module_tests, replay_workflow, measure_effort]
