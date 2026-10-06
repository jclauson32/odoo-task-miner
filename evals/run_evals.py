#!/usr/bin/env python
"""LangSmith datasets and evaluators for the agent stages.

The evaluators are plain functions over dicts, so they can be unit-tested
without LangSmith or an API key. Only `push_datasets` and `evaluate_*` talk to
LangSmith, and they are the only things that need LANGSMITH_API_KEY.

    python evals/run_evals.py push                  # create/refresh datasets
    python evals/run_evals.py segmenter             # run the segmenter eval
    python evals/run_evals.py check                 # offline: score the gold file against itself
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from odoo_miner.agents.assessor import assess_deterministic  # noqa: E402
from odoo_miner.agents.config import load_env  # noqa: E402
from odoo_miner.agents.contracts import SegmentLog  # noqa: E402
from odoo_miner.agents.segmenter import run_segmenter  # noqa: E402
from odoo_miner.models import Session  # noqa: E402

# The source of truth for the LangSmith datasets: `push` makes each dataset match
# these entries. Paths are relative to the repository.
DATASETS = {
    "odoo-miner/segmenter": [
        {
            "gold": "evals/datasets/rfq_to_payment.segments.json",
            "session": "tests/fixtures/rfq_to_payment.session.json",
        },
    ],
}


# ----------------------------------------------------------------- evaluators


def _starts(segments: list[dict]) -> set[int]:
    """The first step of each segment: where a boundary was drawn."""
    return {min(s["step_indexes"]) for s in segments if s.get("step_indexes")}


def boundary_f1(outputs: dict, reference_outputs: dict) -> dict:
    """F1 over segment start indexes against the gold segmentation."""
    predicted = _starts(outputs.get("segments", []))
    gold = _starts(reference_outputs.get("segments", []))
    if not predicted and not gold:
        return {"key": "boundary_f1", "score": 1.0}

    true_positives = len(predicted & gold)
    precision = true_positives / len(predicted) if predicted else 0.0
    recall = true_positives / len(gold) if gold else 0.0
    score = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )
    return {
        "key": "boundary_f1",
        "score": score,
        "comment": (
            f"precision {precision:.2f}, recall {recall:.2f}; "
            f"missed {sorted(gold - predicted)}, extra {sorted(predicted - gold)}"
        ),
    }


def coverage(outputs: dict, reference_outputs: dict) -> dict:
    """1.0 only when every gold step appears in exactly one predicted segment."""
    gold_steps = [i for s in reference_outputs.get("segments", []) for i in s["step_indexes"]]
    predicted = [i for s in outputs.get("segments", []) for i in s["step_indexes"]]
    duplicates = len(predicted) - len(set(predicted))
    missing = set(gold_steps) - set(predicted)
    unknown = set(predicted) - set(gold_steps)
    ok = not duplicates and not missing and not unknown
    return {
        "key": "coverage",
        "score": 1.0 if ok else 0.0,
        "comment": f"duplicates {duplicates}, missing {sorted(missing)}, unknown {sorted(unknown)}",
    }


def label_quality(outputs: dict, reference_outputs: dict) -> dict:
    """Cheap proxy: labels are present, specific enough, and not duplicated."""
    segments = outputs.get("segments", [])
    if not segments:
        return {"key": "label_quality", "score": 0.0, "comment": "no segments"}
    labels = [(s.get("label") or "").strip() for s in segments]
    specific = [label for label in labels if len(label.split()) >= 3]
    unique = len(set(labels)) == len(labels)
    score = (len(specific) / len(labels)) * (1.0 if unique else 0.5)
    return {
        "key": "label_quality",
        "score": score,
        "comment": f"{len(specific)}/{len(labels)} specific, unique={unique}",
    }


def coderefs_exist(outputs: dict, reference_outputs: dict | None = None) -> dict:
    """Every CodeRef a tracer returned must exist on disk at that line."""
    from odoo_miner.agents.tools import odoo_source

    refs = [ref for seg in outputs.get("segments", []) for ref in seg.get("actions", [])]
    if not refs:
        return {"key": "coderefs_exist", "score": 1.0, "comment": "no actions to check"}
    if not odoo_source.source_available():
        return {"key": "coderefs_exist", "score": 0.0, "comment": "Odoo source missing"}

    root = odoo_source.settings().odoo_source_abs
    bad = []
    for ref in refs:
        path = root / ref["file"]
        if not path.is_file():
            bad.append(f"{ref['file']} (missing)")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not (1 <= ref["line"] <= len(lines)):
            bad.append(f"{ref['file']}:{ref['line']} (out of range)")
    return {
        "key": "coderefs_exist",
        "score": (len(refs) - len(bad)) / len(refs),
        "comment": f"{len(bad)} bad of {len(refs)}: {bad[:5]}",
    }


def expected_methods(outputs: dict, reference_outputs: dict) -> dict:
    """Did the tracer find the (model, method) pairs the gold data expects?"""
    wanted = {
        (segment_id, model, method)
        for segment_id, pairs in (reference_outputs.get("expected_actions") or {}).items()
        for model, method in pairs
    }
    if not wanted:
        return {"key": "expected_methods", "score": 1.0, "comment": "nothing expected"}

    found: set[tuple[str, str, str]] = set()
    for segment in outputs.get("segments", []):
        for ref in segment.get("actions", []):
            symbol = ref.get("symbol", "")
            method = symbol.split(".")[-1]
            for segment_id, model, want_method in wanted:
                if segment_id == segment.get("segment_id") and want_method == method:
                    found.add((segment_id, model, want_method))
    return {
        "key": "expected_methods",
        "score": len(found) / len(wanted),
        "comment": f"found {len(found)} of {len(wanted)}; missing {sorted(wanted - found)}",
    }


def scores_match(outputs: dict, reference_outputs: dict) -> dict:
    """Assessor's deterministic half: scores equal the stored expected scores."""
    expected = {
        (s["segment_id"], step["step_index"]): step["score"]
        for s in reference_outputs.get("segments", [])
        for step in s.get("steps", [])
    }
    if not expected:
        return {"key": "scores_match", "score": 1.0, "comment": "no expected scores"}
    actual = {
        (s["segment_id"], step["step_index"]): step["score"]
        for s in outputs.get("segments", [])
        for step in s.get("steps", [])
    }
    same = sum(1 for key, value in expected.items() if actual.get(key) == value)
    return {
        "key": "scores_match",
        "score": same / len(expected),
        "comment": f"{same} of {len(expected)} identical",
    }


def adjustments_within_one(outputs: dict, reference_outputs: dict) -> dict:
    """Assessor's LLM half: no step moved by more than one point."""
    baseline = {
        (s["segment_id"], step["step_index"]): step["score"]
        for s in reference_outputs.get("segments", [])
        for step in s.get("steps", [])
    }
    violations = []
    checked = 0
    for segment in outputs.get("segments", []):
        for step in segment.get("steps", []):
            key = (segment["segment_id"], step["step_index"])
            if key not in baseline:
                continue
            checked += 1
            if abs(step["score"] - baseline[key]) > 1:
                violations.append(f"{key}: {baseline[key]} -> {step['score']}")
    return {
        "key": "adjustments_within_one",
        "score": 1.0 if not violations else 0.0,
        "comment": f"{len(violations)} of {checked} outside +/-1: {violations[:5]}",
    }


SEGMENTER_EVALUATORS = [boundary_f1, coverage, label_quality]
TRACER_EVALUATORS = [coderefs_exist, expected_methods]
ASSESSOR_EVALUATORS = [scores_match, adjustments_within_one]


# ----------------------------------------------------------------- LangSmith


def push_datasets() -> None:
    """Make each LangSmith dataset match DATASETS exactly. Safe to rerun."""
    from langsmith import Client

    client = Client()
    for name, entries in DATASETS.items():
        if client.has_dataset(dataset_name=name):
            dataset = client.read_dataset(dataset_name=name)
        else:
            dataset = client.create_dataset(dataset_name=name)

        wanted = {}
        for entry in entries:
            gold = SegmentLog.model_validate_json((ROOT / entry["gold"]).read_text(encoding="utf-8"))
            wanted[entry["session"]] = {"segments": [s.model_dump() for s in gold.segments]}

        created = updated = deleted = 0
        for example in client.list_examples(dataset_id=dataset.id):
            session = (example.inputs or {}).get("session_path")
            if session in wanted:
                client.update_example(example.id, inputs={"session_path": session},
                                      outputs=wanted.pop(session))
                updated += 1
            else:
                client.delete_example(example.id)
                deleted += 1
        for session, outputs in wanted.items():
            client.create_examples(dataset_id=dataset.id,
                                   examples=[{"inputs": {"session_path": session}, "outputs": outputs}])
            created += 1
        print(f"{name}: {created} created, {updated} updated, {deleted} stale removed")


def evaluate_segmenter(prefix: str = "segmenter-v1") -> None:
    """Run the segmenter over its LangSmith dataset and score it."""
    from langsmith import Client

    client = Client()

    def target(inputs: dict) -> dict:
        """Segment one example's session."""
        session = Session.model_validate_json(
            (ROOT / inputs["session_path"]).read_text(encoding="utf-8")
        )
        return {"segments": [s.model_dump() for s in run_segmenter(session).segments]}

    client.evaluate(
        target,
        data="odoo-miner/segmenter",
        evaluators=SEGMENTER_EVALUATORS,
        experiment_prefix=prefix,
    )


def check_offline() -> int:
    """Score the gold file against itself, and the assessor against its own rerun.

    A sanity check for the evaluators themselves: no API key, no LangSmith.
    """
    paths = {key: ROOT / value for key, value in DATASETS["odoo-miner/segmenter"][0].items()}
    gold = json.loads(paths["gold"].read_text(encoding="utf-8"))
    reference = {"segments": gold["segments"]}

    failures = 0
    for evaluator in SEGMENTER_EVALUATORS:
        result = evaluator(reference, reference)
        ok = result["score"] >= (0.99 if evaluator is not label_quality else 0.5)
        failures += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {result['key']}: {result['score']:.3f}  {result.get('comment','')}")

    if paths["session"].exists():
        session = Session.model_validate_json(paths["session"].read_text(encoding="utf-8"))
        segments = SegmentLog.model_validate(gold)
        first = assess_deterministic(session, segments).model_dump()
        second = assess_deterministic(session, segments).model_dump()
        for evaluator in ASSESSOR_EVALUATORS:
            result = evaluator(second, first)
            ok = result["score"] >= 0.99
            failures += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} {result['key']}: {result['score']:.3f}  {result.get('comment','')}")
    else:
        print(f"skip  assessor evaluators: {paths['session']} not found")

    return failures


def main() -> int:
    """Run the command given on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["push", "segmenter", "check"], help="what to run"
    )
    parser.add_argument("--prefix", default="segmenter-v1")
    args = parser.parse_args()
    load_env()            # LangSmith credentials come from .env

    if args.command == "push":
        push_datasets()
    elif args.command == "segmenter":
        evaluate_segmenter(args.prefix)
    else:
        return check_offline()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
