import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from odoo_miner.cli import app
from odoo_miner.merge import merge
from odoo_miner.models import ClickLog, NetworkLog, Session
from odoo_miner.odoo_urls import parse_odoo_url
from odoo_miner.recorder import RecordingError, load_recording, parse_recording

FIXTURES = Path(__file__).parent / "fixtures"
RECORDING = FIXTURES / "vendor_bill.json"
NETWORK = FIXTURES / "vendor_bill_network.json"


@pytest.fixture
def log() -> ClickLog:
    return load_recording(RECORDING)


# --- URL parsing -----------------------------------------------------------

def test_legacy_hash_url():
    ctx = parse_odoo_url("http://x/web#id=1042&action=245&model=account.move&view_type=form&menu_id=115")
    assert (ctx.model, ctx.record_id, ctx.action, ctx.view_type, ctx.menu_id) == (
        "account.move", 1042, "245", "form", 115
    )


def test_new_path_url_with_action():
    ctx = parse_odoo_url("http://x/odoo/action-388/77")
    assert ctx.action == "388" and ctx.record_id == 77 and ctx.view_type == "form"


def test_new_path_url_with_model():
    ctx = parse_odoo_url("http://x/odoo/stock.picking/311")
    assert ctx.model == "stock.picking" and ctx.record_id == 311


def test_new_path_action_path_list_view():
    ctx = parse_odoo_url("http://x/odoo/purchase")
    assert ctx.action == "purchase" and ctx.record_id is None and ctx.view_type is None


def test_new_path_breadcrumb_stack_uses_last_screen():
    ctx = parse_odoo_url("http://x/odoo/action-245/1042/action-388/77")
    assert ctx.action == "388" and ctx.record_id == 77 and ctx.model is None


def test_new_path_model_without_dot_and_new_record():
    ctx = parse_odoo_url("http://x/odoo/m-website/new")
    assert ctx.model == "website" and ctx.record_id is None and ctx.view_type == "form"


def test_new_path_xmlid_action():
    ctx = parse_odoo_url("http://x/odoo/action-account.action_move_in_invoice_type/5")
    assert ctx.action == "account.action_move_in_invoice_type" and ctx.record_id == 5


def test_empty_url():
    assert parse_odoo_url(None).model is None


# --- Recorder parsing ------------------------------------------------------

def test_drops_setup_and_keyup(log):
    types = [c.type for c in log.clicks]
    assert "setViewport" not in types and "keyUp" not in types
    assert len(log.clicks) == 10


def test_keep_noise_keeps_keyup():
    data = json.loads(RECORDING.read_text())
    assert "keyUp" in [c.type for c in parse_recording(data, keep_noise=True).clicks]


def test_step_index_matches_original_recording(log):
    confirm = log.clicks[-1]
    assert confirm.step_index == 11  # index in the raw recording, used to join replay data


def test_target_extraction(log):
    confirm = log.clicks[-1].target
    assert confirm.aria_label == "Confirm"
    assert confirm.button_name == "action_post"
    assert confirm.text == "Confirm"

    tab = log.clicks[2].target
    assert tab.button_name == "other_tab" and tab.text == "Other Info"

    price_cell = log.clicks[6].target  # div[name='invoice_line_ids'] td[name='price_unit']
    assert price_cell.button_name == "price_unit"


def test_page_context_follows_navigation(log):
    open_bill = log.clicks[1]
    assert open_bill.page.view_type == "list"           # clicked from the list
    assert open_bill.navigates_to.endswith("view_type=form&menu_id=115")

    other_tab = log.clicks[2]
    assert other_tab.page.record_id == 1042             # now on the bill form

    receipt_button = log.clicks[4]
    assert receipt_button.page.action == "388" and receipt_button.page.record_id == 77

    price_change = next(c for c in log.clicks if c.type == "change")
    assert price_change.value == "2.15"
    assert price_change.page.model == "account.move" and price_change.page.record_id == 1042


def test_rejects_non_recording():
    with pytest.raises(RecordingError):
        parse_recording({"foo": "bar"})


# --- Merge -----------------------------------------------------------------

def test_merge_attaches_and_classifies(log):
    network = NetworkLog.model_validate_json(NETWORK.read_text())
    session = merge(log, network)

    by_step = {c.step_index: c for c in session.clicks}
    assert [k.kind for k in by_step[1].calls] == ["action_load", "read"]
    assert [k.kind for k in by_step[8].calls] == ["compute"]

    confirm = by_step[11]
    assert confirm.has_write
    assert [(k.method, k.kind) for k in confirm.calls] == [
        ("web_save", "write"), ("action_post", "write"), ("x_custom_thing", "unknown")
    ]
    assert not by_step[2].has_write

    assert [k.method for k in session.unattributed_calls] == ["has_group"]
    assert session.replay_completed is True


def test_merge_without_network(log):
    session = merge(log, None)
    assert session.replay_completed is None
    assert all(c.calls == [] for c in session.clicks)


# --- CLI -------------------------------------------------------------------

runner = CliRunner()


def test_cli_ingest_merge_show(tmp_path):
    clicks = tmp_path / "clicks.json"
    session = tmp_path / "session.json"

    r = runner.invoke(app, ["ingest", str(RECORDING), "-o", str(clicks)])
    assert r.exit_code == 0, r.output
    ClickLog.model_validate_json(clicks.read_text())

    r = runner.invoke(app, ["merge", str(clicks), str(NETWORK), "-o", str(session)])
    assert r.exit_code == 0, r.output
    assert Session.model_validate_json(session.read_text()).clicks[-1].has_write

    r = runner.invoke(app, ["show", str(session)])
    assert r.exit_code == 0 and "action_post" in r.output


def test_cli_run_skip_replay(tmp_path):
    r = runner.invoke(app, ["run", str(RECORDING), "-d", str(tmp_path), "--skip-replay"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "clicks.json").exists() and (tmp_path / "session.json").exists()


def test_cli_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    r = runner.invoke(app, ["ingest", str(bad)])
    assert r.exit_code == 1


def test_cli_replay_missing_script(tmp_path):
    r = runner.invoke(app, ["replay", str(RECORDING), "--script", str(tmp_path / "nope.mjs")])
    assert r.exit_code == 1
