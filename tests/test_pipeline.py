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

    r = runner.invoke(app, ["show", str(session)], env={"COLUMNS": "200"})
    assert r.exit_code == 0 and "account.move.action_post" in r.output


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


# --- Real Odoo 18 recording ------------------------------------------------

# Exercises the new-tab start and Odoo 18 selectors; rfq_to_payment.json is the
# reference workflow the agents and gold labels use.
REAL = FIXTURES / "rfq_expedite_freight.json"


def test_real_recording_skips_new_tab_and_keeps_indexes():
    log = load_recording(REAL)
    assert all(not (c.url or "").startswith("chrome://") for c in log.clicks)
    first = log.clicks[0]
    assert first.type == "navigate" and first.url.endswith("/web/login") and first.step_index == 2
    new_button = next(c for c in log.clicks if c.target and c.target.aria_label == "New")
    assert new_button.step_index == 11  # matches the replay's step numbering


def test_real_recording_page_context():
    log = load_recording(REAL)
    after_login = next(c for c in log.clicks if c.step_index == 9)
    assert after_login.page_url == "http://localhost:8069/odoo"


def test_aria_label_walks_out_of_icon_chains():
    log = load_recording(REAL)
    by_step = {c.step_index: c for c in log.clicks}
    assert by_step[14].target.aria_label == "Save manually"     # icon inside the save button
    assert by_step[27].target.aria_label == "Confirm Order"
    assert by_step[9].target.aria_label is None                  # apps-menu icon has no name
    assert by_step[30].target.aria_label == "Products"           # 'Products[role="menuitem"]'


def test_show_numbers_rows_by_recording_step_and_filters_a_range(tmp_path):
    out = tmp_path / "clicks.json"
    runner.invoke(app, ["ingest", str(REAL), "-o", str(out)])
    r = runner.invoke(app, ["show", str(out), "--from", "27", "--to", "27"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "Confirm Order" in r.output            # step 27 by recording number
    assert "Save manually" not in r.output        # step 14 is outside the range


def test_show_does_not_eat_brackets(tmp_path):
    out = tmp_path / "clicks.json"
    runner.invoke(app, ["ingest", str(REAL), "-o", str(out)])
    r = runner.invoke(app, ["show", str(out)], env={"COLUMNS": "200"})
    assert "[FRT-EXP] Expedite freight" in r.output


# --- show: agent artifacts ---------------------------------------------------

GOLD_SEGMENTS = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "rfq_to_payment.segments.json"


def _show(path, columns="200"):
    return runner.invoke(app, ["show", str(path)], env={"COLUMNS": columns})


def test_show_renders_segments():
    r = _show(GOLD_SEGMENTS)
    assert r.exit_code == 0, r.output
    assert "Confirm the bill and hit the missing bill date error" in r.output
    assert "failed" in r.output and "recovered" in r.output


def test_show_renders_traces(tmp_path):
    traces = tmp_path / "traces.json"
    traces.write_text(json.dumps({"session": "s", "segments": [{
        "segment_id": "s07", "kind": "action", "retrievals": [],
        "actions": [{"module": "purchase", "file": "addons/purchase/models/purchase_order.py",
                     "line": 538, "symbol": "PurchaseOrder.button_confirm"}],
        "explanation": "Confirming the RFQ turns it into a purchase order.",
    }]}))
    r = _show(traces)
    assert r.exit_code == 0, r.output
    assert "PurchaseOrder.button_confirm" in r.output and "purchase_order.py:538" in r.output


def test_show_renders_assessment_and_plan(tmp_path):
    assessment = tmp_path / "assessment.json"
    assessment.write_text(json.dumps({"session": "s", "total_effort": 5, "segments": [{
        "segment_id": "s10", "effort": 5, "friction": ["error: bill date required"],
        "steps": [{"step_index": 50, "score": 4, "signals": {"error": 1.0}}],
    }]}))
    r = _show(assessment)
    assert r.exit_code == 0 and "bill date required" in r.output and "50 (4)" in r.output

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "decision": "no_change", "summary": "The bill date is a deliberate control.",
        "risks": ["Defaulting it would weaken an audit control."],
        "unverified_citations": ["`x` is cited in a.py but does not appear there."],
    }))
    r = _show(plan)
    assert r.exit_code == 0
    assert "no change" in r.output and "deliberate control" in r.output
    assert "check by hand" in r.output


# --- secrets ---------------------------------------------------------------------


def test_passwords_typed_in_a_recording_are_redacted_at_ingest():
    """session.json is sent to the model and stored in traces; a password must not be."""
    log = load_recording(FIXTURES / "rfq_to_payment.json")
    by_step = {c.step_index: c for c in log.clicks}
    assert by_step[5].value == "<redacted>"          # the password field
    assert by_step[2].value == "admin"               # the login name is kept


@pytest.mark.parametrize("selectors", [
    [["input[type='password']"]], [["#pwd"]], [["aria/API key"]], [["[name='otp']"]],
])
def test_secret_fields_are_recognised(selectors):
    from odoo_miner.recorder import parse_recording

    log = parse_recording({"steps": [{"type": "change", "value": "hunter2", "selectors": selectors}]})
    assert log.clicks[0].value == "<redacted>"


def test_ordinary_fields_are_not_redacted():
    from odoo_miner.recorder import parse_recording

    log = parse_recording({"steps": [{"type": "change", "value": "12.50", "selectors": [["aria/Sales Price"]]}]})
    assert log.clicks[0].value == "12.50"


def test_smart_buttons_that_open_records_are_navigation_not_writes():
    from odoo_miner.merge import classify
    from odoo_miner.models import NetworkCall

    def call(method, endpoint="/web/dataset/call_button/purchase.order/" + "x"):
        return NetworkCall(step_index=1, timestamp_ms=0, endpoint=endpoint, model="purchase.order", method=method)

    assert classify(call("action_view_picking")) == "action_load"
    assert classify(call("action_view_source_purchase_orders")) == "action_load"
    assert classify(call("action_post")) == "write"            # a real write through the same endpoint
    assert classify(call("button_confirm")) == "write"
