"""Tests for the tools the builder uses to install, test, replay and measure a module.

No Docker and no Odoo: the commands are replaced by a recorder, which is
enough to check what is run and in what order - the part that was wrong.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odoo_miner.agents.tools import odoo_ops
from odoo_miner.cli import app
from odoo_miner.recorder import REDACTED, RecordingError, redact_recording, rehydrate_secrets

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "tests/fixtures/rfq_to_payment.json"
SESSION = ROOT / "tests/fixtures/rfq_to_payment.session.json"
GOLD = ROOT / "evals/datasets/rfq_to_payment.segments.json"


# ----------------------------------------------------------------- recordings and secrets


def test_redacting_a_recording_hides_only_secrets_and_round_trips():
    original = json.loads(RECORDING.read_text())
    redacted = redact_recording(original)
    values = [s.get("value") for s in redacted["steps"] if s.get("value") is not None]
    assert REDACTED in values and "admin" in values          # password hidden, login name kept
    assert rehydrate_secrets(redacted, original) == original


def test_a_redacted_value_without_an_original_is_an_error_not_a_login_attempt():
    redacted = {"steps": [{"type": "change", "value": REDACTED, "selectors": [["#password"]]}]}
    with pytest.raises(RecordingError, match="No original value"):
        rehydrate_secrets(redacted, {"steps": []})


def test_run_keeps_a_redacted_copy_of_its_recording(tmp_path):
    result = CliRunner().invoke(app, ["run", str(RECORDING), "-d", str(tmp_path), "--skip-replay"])
    assert result.exit_code == 0, result.output
    kept = json.loads((tmp_path / "recording.json").read_text())
    assert REDACTED in [s.get("value") for s in kept["steps"]]
    assert (tmp_path / "recording.source").read_text().strip() == str(RECORDING.resolve())


# ----------------------------------------------------------------- module tests


@pytest.mark.parametrize("log, verdict", [
    ("INFO demo odoo.tests.result: 0 failed, 0 error(s) of 3 tests when loading database 'demo'",
     "tests passed: 3 test(s) ran"),
    ("INFO demo odoo.tests.result: 0 failed, 0 error(s) of 0 tests when loading database 'demo'",
     "no tests ran"),
    ("nothing about tests at all", "no tests ran"),
    ("INFO demo odoo.tests.result: 1 failed, 0 error(s) of 3 tests when loading database 'demo'",
     "1 failed"),
])
def test_module_tests_pass_only_when_tests_actually_ran(monkeypatch, tmp_path, log, verdict):
    commands = []
    monkeypatch.setattr(odoo_ops, "_run", lambda cmd, **kw: (commands.append(cmd), (0, log))[1])
    result = odoo_ops.run_module_tests("demo_mod", run_dir=str(tmp_path))
    assert verdict in result
    assert (tmp_path / "tests.log").read_text() == log
    assert commands == [["bash", odoo_ops.TEST_SCRIPT, "demo_mod"]]


def test_odoo_is_stopped_while_a_module_installs_or_tests():
    """A running server loading the same database collided with the install
    ("could not serialize access due to concurrent update") in a real build."""
    for script in (odoo_ops.INSTALL_SCRIPT, odoo_ops.TEST_SCRIPT):
        text = Path(script).read_text()
        stop, run = text.index("docker compose stop odoo"), text.index("docker compose run --rm odoo odoo")
        assert stop < run and "start odoo" in text and "wait_for_odoo.sh" in text, script
        # Installs if needed and updates if installed, so it works either way.
        assert '-i "$MODULE" -u "$MODULE"' in text, script
    assert '--test-tags "/$MODULE"' in Path(odoo_ops.TEST_SCRIPT).read_text()


# ----------------------------------------------------------------- replay


def test_replay_restores_then_installs_then_replays_with_secrets_restored(monkeypatch, tmp_path):
    original = tmp_path / "original.json"
    original.write_text(RECORDING.read_text())
    after = tmp_path / "after_recording.json"
    after.write_text(json.dumps(redact_recording(json.loads(RECORDING.read_text()))))
    out = tmp_path / "after"
    order, replayed = [], {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["bash", odoo_ops.RESTORE_HOOK]:
            order.append("restore")
        elif cmd == ["bash", odoo_ops.INSTALL_SCRIPT, "demo_mod"]:
            order.append("install")
        elif "odoo_miner.cli" in cmd:
            order.append("replay")
            assert "--pre-hook" not in cmd, "a restore after the install would remove the module"
            replayed.update(json.loads(Path(cmd[cmd.index("run") + 1]).read_text()))
            out.mkdir()
            (out / "session.json").write_text(json.dumps({"replay_completed": True}))
        return 0, ""

    monkeypatch.setattr(odoo_ops, "_run", fake_run)
    result = odoo_ops.replay_workflow(str(after), str(out), module="demo_mod", original_recording=str(original))

    assert result.startswith("replay completed"), result
    assert order == ["restore", "install", "replay"]
    assert REDACTED not in [s.get("value") for s in replayed["steps"]]


# ----------------------------------------------------------------- effort


def test_effort_is_compared_the_same_way_on_both_sides(tmp_path):
    for side in ("before", "after"):
        folder = tmp_path / side
        folder.mkdir()
        shutil.copy(SESSION, folder / "session.json")
        shutil.copy(GOLD, folder / "segments.json")
    result = odoo_ops.measure_effort(str(tmp_path / "after"), before_dir=str(tmp_path / "before"))
    assert "+0%" in result and "54 steps" in result


# ----------------------------------------------------------------- tool errors


def test_a_failing_tool_reports_the_error_instead_of_ending_the_run():
    from odoo_miner.agents.tools.errors import reports_errors

    def read_thing(path: str) -> str:
        """Read a thing."""
        raise FileNotFoundError(f"No such file: {path}")

    wrapped = reports_errors(read_thing)
    assert wrapped("x.py") == "error: read_thing failed - FileNotFoundError: No such file: x.py"


def test_an_approval_pause_is_never_swallowed():
    """GraphInterrupt subclasses Exception; catching it would remove a human gate."""
    from langgraph.errors import GraphInterrupt

    from odoo_miner.agents.tools.errors import reports_errors

    def gated() -> str:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        reports_errors(gated)()


def test_wrapped_tools_keep_the_schema_the_model_sees():
    import inspect

    from langchain_core.tools import tool as as_tool

    from odoo_miner.agents.tools import odoo_source

    for wrapped in odoo_source.agent_tools():
        original = getattr(odoo_source, wrapped.__name__)
        assert inspect.signature(wrapped) == inspect.signature(original)
        assert as_tool(wrapped).args == as_tool(original).args
        assert wrapped.__doc__ == original.__doc__


def test_unknown_tool_names_are_refused():
    from odoo_miner.agents.tools import odoo_source

    with pytest.raises(ValueError, match="Unknown tools"):
        odoo_source.agent_tools("find_method", "no_such_tool")
