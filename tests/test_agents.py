"""Tests for the agent stages.

Every test here runs without an API key: the deterministic halves are tested
directly, and the LLM halves are tested with a fake agent that returns a
canned structured response. Tests that need the Odoo source checkout skip
when it is absent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from odoo_miner.agents import assessor, segmenter, tracer
from odoo_miner.agents.contracts import (
    Assessment, BuildResult, CodeRef, Plan, Segment, SegmentAssessment,
    SegmentLog, StepDifficulty, TracedSegment,
)
from odoo_miner.agents.tools import delivery, odoo_source
from odoo_miner.models import Session

GOLD = ROOT / "evals/datasets/rfq_to_payment.segments.json"
SESSION = ROOT / "out/rfq_to_payment/session.json"


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def session() -> Session:
    if not SESSION.exists():
        pytest.skip(f"{SESSION} not found; run the pipeline first")
    return Session.model_validate_json(SESSION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gold() -> SegmentLog:
    return SegmentLog.model_validate_json(GOLD.read_text(encoding="utf-8"))


class FakeAgent:
    """Stands in for a create_agent result. Records what it was asked."""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def invoke(self, payload, config=None):
        self.calls.append({"payload": payload, "config": config})
        response = (
            self.response(payload) if callable(self.response) else self.response
        )
        return {"structured_response": response}


# ----------------------------------------------------------------- contracts (M1)


def test_gold_validates_and_covers_the_session(session, gold):
    assert len(gold.segments) >= 8
    covered = [i for s in gold.segments for i in s.step_indexes]
    assert covered == [c.step_index for c in session.clicks]
    assert len(covered) == len(set(covered))


def test_contracts_round_trip():
    plan = Plan(decision="customize", summary="s", module_name="m")
    assert Plan.model_validate_json(plan.model_dump_json()).module_name == "m"
    built = BuildResult(branch="feat/m", tests_passed=True)
    assert BuildResult.model_validate_json(built.model_dump_json()).branch == "feat/m"


def test_step_score_is_bounded():
    with pytest.raises(Exception):
        StepDifficulty(step_index=1, score=9)


# ----------------------------------------------------------------- segmenter (M2)


def test_drops_tab_but_keeps_enter(session):
    noise = [c for c in session.clicks if segmenter.is_noise(c)]
    assert noise, "the reference session has a Tab keyDown"
    assert all(c.key == "Tab" for c in noise)
    enters = [c for c in session.clicks if c.key == "Enter"]
    assert enters and not any(segmenter.is_noise(c) for c in enters)


def test_rendering_is_one_line_per_visible_step(session):
    rendered = segmenter.render_session(session)
    body = rendered.split("\n\n", 1)[1]
    assert len(body.strip().splitlines()) == len(segmenter.expected_indexes(session))


def test_boundary_hints_mark_writes_and_errors(session):
    hints = segmenter.boundary_hints(session)
    writes = [i for i, marks in hints.items() if "WRITE" in marks]
    errors = [i for i, marks in hints.items() if "ERROR" in marks]
    assert writes and errors
    by_index = {c.step_index: c for c in session.clicks}
    assert all(by_index[i].has_write for i in writes)


def test_validation_catches_bad_segmentations(session, gold):
    assert segmenter.validate_segment_log(_without_noise(gold, session), session) == []

    missing = gold.model_copy(update={"segments": gold.segments[:-1]})
    assert any("not assigned" in p for p in segmenter.validate_segment_log(missing, session))

    first = gold.segments[0]
    duplicated = gold.model_copy(update={"segments": list(gold.segments) + [
        first.model_copy(update={"segment_id": "sXX"})
    ]})
    assert any("more than one" in p for p in segmenter.validate_segment_log(duplicated, session))

    unknown = gold.model_copy(update={"segments": [
        first.model_copy(update={"step_indexes": [99999]})
    ] + list(gold.segments[1:])})
    assert any("not in the session" in p for p in segmenter.validate_segment_log(unknown, session))


def _without_noise(gold: SegmentLog, session: Session) -> SegmentLog:
    """Gold data as the model would return it: noise steps absent."""
    noise = {c.step_index for c in session.clicks if segmenter.is_noise(c)}
    return gold.model_copy(update={"segments": [
        s.model_copy(update={"step_indexes": [i for i in s.step_indexes if i not in noise]})
        for s in gold.segments
    ]})


def test_run_segmenter_reattaches_noise(session, gold):
    agent = FakeAgent(_without_noise(gold, session))
    result = segmenter.run_segmenter(session, agent=agent, run="test")
    covered = [i for s in result.segments for i in s.step_indexes]
    assert covered == [c.step_index for c in session.clicks]
    assert agent.calls[0]["config"]["metadata"]["stage"] == "segmenter"


def test_run_segmenter_retries_then_succeeds(session, gold):
    good = _without_noise(gold, session)
    bad = good.model_copy(update={"segments": good.segments[:2]})
    responses = iter([bad, good])

    agent = FakeAgent(lambda payload: next(responses))
    result = segmenter.run_segmenter(session, agent=agent, run="test")
    assert len(agent.calls) == 2
    assert len(result.segments) == len(gold.segments)
    # The retry must tell the model what was wrong.
    retry = agent.calls[1]["payload"]["messages"][-1]["content"]
    assert "not valid" in retry and "not assigned" in retry


def test_run_segmenter_raises_when_never_valid(session, gold):
    bad = _without_noise(gold, session)
    bad = bad.model_copy(update={"segments": bad.segments[:1]})
    with pytest.raises(ValueError, match="invalid segmentation"):
        segmenter.run_segmenter(session, agent=FakeAgent(bad), run="test")


# ----------------------------------------------------------------- tracer (M3)


def test_classify_segment_from_calls(session, gold):
    by_id = {s.segment_id: s for s in gold.segments}
    confirm = tracer.segment_clicks(session, by_id["s07"].step_indexes)
    assert tracer.classify_segment(confirm) in ("action", "mixed")
    assert tracer.action_calls(confirm), "confirming an order writes"

    login = tracer.segment_clicks(session, by_id["s01"].step_indexes)
    assert tracer.classify_segment(login) == "navigation"


def test_retrievals_skip_framework_chatter(session, gold):
    by_id = {s.segment_id: s for s in gold.segments}
    clicks = tracer.segment_clicks(session, by_id["s03"].step_indexes)
    methods = {q.method for q in tracer.retrievals_for(clicks)}
    assert "name_search" in methods
    assert not methods & tracer.FRAMEWORK_METHODS


def test_tracer_drops_invented_coderefs(session, gold):
    invented = TracedSegment(
        segment_id="s07", kind="action",
        actions=[CodeRef(module="purchase", file="addons/nope/no_such_file.py", line=1, symbol="X.y")],
        explanation="made up",
    )
    traced = tracer.trace_segment(gold.segments[6], session, agent=FakeAgent(invented), run="test")
    assert all(ref.file != "addons/nope/no_such_file.py" for ref in traced.actions)


@pytest.mark.skipif(not odoo_source.source_available(), reason="vendor/odoo not cloned")
class TestOdooSource:
    def test_finds_button_confirm_in_purchase(self):
        refs = odoo_source.find_method("purchase.order", "button_confirm")
        assert refs
        assert any(r.module == "purchase" and "purchase_order.py" in r.file for r in refs)

    def test_finds_action_post_on_account_move(self):
        refs = odoo_source.find_method("account.move", "action_post")
        assert any(r.module == "account" for r in refs)

    def test_every_ref_exists_at_that_line(self):
        root = odoo_source.settings().odoo_source_abs
        for model, method in [("purchase.order", "button_confirm"), ("account.move", "action_post")]:
            for ref in odoo_source.find_method(model, method):
                lines = (root / ref.file).read_text(encoding="utf-8", errors="replace").splitlines()
                assert 1 <= ref.line <= len(lines)
                assert f"def {method}" in lines[ref.line - 1]

    def test_wrong_model_is_not_returned(self):
        # mail's fetchmail.server also defines button_confirm; different model.
        refs = odoo_source.find_method("purchase.order", "button_confirm", installed_only=False)
        assert all("fetchmail" not in r.file for r in refs)

    def test_read_source_refuses_paths_outside_the_roots(self):
        for bad in ["../../../etc/passwd", "/etc/passwd"]:
            with pytest.raises((PermissionError, ValueError)):
                odoo_source.read_source(bad, 1, 2)

    def test_read_source_is_bounded(self):
        text = odoo_source.read_source("addons/purchase/models/purchase_order.py", 1, 10_000)
        assert len(text.splitlines()) <= odoo_source.MAX_READ_LINES


def test_model_names_reads_name_and_inherit():
    import ast

    tree = ast.parse(
        "class A(models.Model):\n"
        "    _name = 'x.y'\n"
        "class B(models.Model):\n"
        "    _inherit = ['a.b', 'c.d']\n"
    )
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert odoo_source.model_names(classes[0]) == {"x.y"}
    assert odoo_source.model_names(classes[1]) == {"a.b", "c.d"}


# ----------------------------------------------------------------- assessor (M4)


def test_deterministic_scores_are_stable(session, gold):
    first = assessor.assess_deterministic(session, gold)
    second = assessor.assess_deterministic(session, gold)
    assert first.model_dump_json() == second.model_dump_json()


def test_every_step_is_scored_once(session, gold):
    assessment = assessor.assess_deterministic(session, gold)
    scored = [s.step_index for seg in assessment.segments for s in seg.steps]
    assert sorted(scored) == sorted(c.step_index for c in session.clicks)


def test_scores_are_in_range(session, gold):
    assessment = assessor.assess_deterministic(session, gold)
    assert all(1 <= s.score <= 5 for seg in assessment.segments for s in seg.steps)


def test_bill_segment_reports_error_and_modal(session, gold):
    assessment = assessor.assess_deterministic(session, gold)
    bill = next(s for s in assessment.segments if s.segment_id == "s10")
    assert any("error" in f.lower() for f in bill.friction)
    assert any("dialog" in f.lower() or "wizard" in f.lower() for f in bill.friction)


def test_error_signal_raises_the_score(session, gold):
    assessment = assessor.assess_deterministic(session, gold)
    errored = [
        s for seg in assessment.segments for s in seg.steps if s.signals.get("error")
    ]
    assert errored and all(s.score >= 3 for s in errored)


def test_backtrack_is_scoped_to_one_segment(session, gold):
    """A new segment visiting an earlier model is the next task, not a backtrack."""
    assessment = assessor.assess_deterministic(session, gold)
    flagged = {
        seg.segment_id for seg in assessment.segments
        for s in seg.steps if s.signals.get("backtrack")
    }
    assert len(flagged) < len(gold.segments)


def test_llm_adjustments_are_clamped(session, gold):
    baseline = assessor.assess_deterministic(session, gold).segments[0]
    runaway = baseline.model_copy(update={
        "steps": [s.model_copy(update={"score": 5}) for s in baseline.steps],
        "effort": 999.0,
    })
    clamped = assessor.clamp_adjustments(runaway, baseline)
    for original, adjusted in zip(baseline.steps, clamped.steps):
        assert abs(adjusted.score - original.score) <= assessor.MAX_ADJUSTMENT
    assert clamped.effort == sum(s.score for s in clamped.steps)


def test_clamp_drops_invented_steps_and_restores_missing(session, gold):
    baseline = assessor.assess_deterministic(session, gold).segments[0]
    tampered = baseline.model_copy(update={
        "steps": [StepDifficulty(step_index=424242, score=5)],
    })
    clamped = assessor.clamp_adjustments(tampered, baseline)
    assert [s.step_index for s in clamped.steps] == [s.step_index for s in baseline.steps]


def test_run_assessor_offline_makes_no_agent_call(session, gold):
    agent = FakeAgent(None)
    result = assessor.run_assessor(gold, session, agent=agent, explain=False)
    assert isinstance(result, Assessment)
    assert agent.calls == []


def test_run_assessor_with_agent_clamps_and_recomputes(session, gold):
    baseline = assessor.assess_deterministic(session, gold)
    by_id = {s.segment_id: s for s in baseline.segments}

    def respond(payload):
        content = payload["messages"][0]["content"]
        segment_id = content.split()[1].rstrip(":")
        original = by_id[segment_id]
        return original.model_copy(update={
            "steps": [s.model_copy(update={"score": 5, "rationale": "bumped"}) for s in original.steps],
            "effort": 0.0,
        })

    result = assessor.run_assessor(gold, session, agent=FakeAgent(respond), run="test")
    for segment in result.segments:
        original = by_id[segment.segment_id]
        for before, after in zip(original.steps, segment.steps):
            assert after.score - before.score <= assessor.MAX_ADJUSTMENT
        assert segment.effort == sum(s.score for s in segment.steps)


# ----------------------------------------------------------------- pipeline (M5)


def test_graph_topology_and_slicing():
    from odoo_miner.pipeline.graph import build_graph
    from odoo_miner.pipeline.state import STAGES

    full = build_graph()
    nodes = {n for n in full.get_graph().nodes} - {"__start__", "__end__"}
    assert nodes == set(STAGES)

    edges = {(e.source, e.target) for e in full.get_graph().edges}
    assert ("segment", "trace") in edges
    assert ("trace", "assess") in edges
    assert ("assess", "plan") in edges
    assert ("plan", "approve") in edges
    assert ("approve", "build") in edges

    sliced = build_graph(until="assess")
    sliced_nodes = {n for n in sliced.get_graph().nodes} - {"__start__", "__end__"}
    assert sliced_nodes == {"segment", "trace", "assess"}
    assert ("assess", "__end__") in {(e.source, e.target) for e in sliced.get_graph().edges}


def test_graph_rejects_unknown_stage():
    from odoo_miner.pipeline.graph import build_graph

    with pytest.raises(ValueError, match="Unknown stage"):
        build_graph(until="not_a_stage")


def test_build_only_runs_when_approved():
    from langgraph.graph import END

    from odoo_miner.pipeline.graph import _approved

    assert _approved({"approval": {"approved": True}}) == "build"
    assert _approved({"approval": {"approved": False}}) == END
    assert _approved({}) == END


def test_errors_accumulate_rather_than_overwrite():
    from odoo_miner.pipeline.state import _extend

    assert _extend(["a"], ["b"]) == ["a", "b"]
    assert _extend(None, ["b"]) == ["b"]
    assert _extend(["a"], None) == ["a"]


# ----------------------------------------------------------------- delivery guards


def test_push_refuses_a_missing_module():
    assert "does not exist" in delivery.git_push_feature_branch("no_such_module_xyz", "t")


def test_email_refuses_without_a_configured_recipient(monkeypatch):
    monkeypatch.delenv("REPORT_EMAIL_TO", raising=False)
    assert "REPORT_EMAIL_TO" in delivery.send_report_email("s", "b")


def test_email_refuses_without_smtp_host(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL_TO", "someone@example.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert "SMTP_HOST" in delivery.send_report_email("s", "b")


def test_main_is_a_protected_branch():
    assert "main" in delivery.PROTECTED_BRANCHES
    assert "master" in delivery.PROTECTED_BRANCHES


# ----------------------------------------------------------------- evaluators (section 11)


def test_evaluators_score_gold_against_itself(gold):
    import run_evals

    reference = {"segments": [s.model_dump() for s in gold.segments]}
    assert run_evals.boundary_f1(reference, reference)["score"] == 1.0
    assert run_evals.coverage(reference, reference)["score"] == 1.0


def test_boundary_f1_penalises_wrong_boundaries(gold):
    import run_evals

    reference = {"segments": [s.model_dump() for s in gold.segments]}
    merged = {"segments": [{
        "segment_id": "s01",
        "step_indexes": [i for s in gold.segments for i in s.step_indexes],
        "label": "everything at once", "intent": "x", "outcome": "completed",
    }]}
    assert run_evals.boundary_f1(merged, reference)["score"] < 0.5
    assert run_evals.coverage(merged, reference)["score"] == 1.0


def test_coverage_catches_duplicates(gold):
    import run_evals

    reference = {"segments": [s.model_dump() for s in gold.segments]}
    doubled = {"segments": reference["segments"] + [reference["segments"][0]]}
    assert run_evals.coverage(doubled, reference)["score"] == 0.0


def test_adjustments_evaluator_flags_a_two_point_move():
    import run_evals

    baseline = {"segments": [{"segment_id": "s01", "steps": [{"step_index": 1, "score": 2}]}]}
    moved = {"segments": [{"segment_id": "s01", "steps": [{"step_index": 1, "score": 4}]}]}
    assert run_evals.adjustments_within_one(moved, baseline)["score"] == 0.0
    nudged = {"segments": [{"segment_id": "s01", "steps": [{"step_index": 1, "score": 3}]}]}
    assert run_evals.adjustments_within_one(nudged, baseline)["score"] == 1.0
