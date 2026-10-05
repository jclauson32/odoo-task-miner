"""The findings report: what the analysis found, for the person who owns the process.

Composed entirely from a run's artifacts - no model call - so the email says
exactly what the files say. Screenshots are chosen in code: the steps where
the user hit an error or had to work through a dialog.
"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import Assessment, Plan

MAX_SCREENSHOTS = 4
FRICTION_SIGNALS = ("error", "modal")


def _title(run_dir: Path) -> str:
    session = run_dir / "session.json"
    if session.exists():
        data = json.loads(session.read_text(encoding="utf-8"))
        return data.get("title") or run_dir.name
    return run_dir.name


def friction_screenshots(run_dir: Path, assessment: Assessment | None) -> list[Path]:
    """Screenshots of the steps where the user got stuck, in step order."""
    shots = run_dir / "screenshots"
    if assessment is None or not shots.is_dir():
        return []
    steps = sorted({
        step.step_index
        for segment in assessment.segments for step in segment.steps
        if any(step.signals.get(name) for name in FRICTION_SIGNALS)
    })
    paths = [shots / f"step-{index:03d}.png" for index in steps]
    return [path for path in paths if path.is_file()][:MAX_SCREENSHOTS]


def compose_report(run_dir: Path) -> tuple[str, str, list[str]]:
    """Subject, plain-text body and attachment paths for a run's findings."""
    run_dir = Path(run_dir)
    plan_path = run_dir / "plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"{plan_path} not found; run `odoo-miner plan {run_dir}` first.")
    plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))

    assessment = None
    if (run_dir / "assessment.json").exists():
        assessment = Assessment.model_validate_json(
            (run_dir / "assessment.json").read_text(encoding="utf-8")
        )

    title = _title(run_dir)
    decision = plan.decision.replace("_", " ")
    subject = f"odoo-miner: {decision} - {title}"

    lines = [f"Workflow: {title}", f"Recommendation: {decision}", "", plan.summary.strip(), ""]
    if assessment is not None:
        worst = sorted(assessment.segments, key=lambda s: s.effort, reverse=True)[:3]
        lines.append(f"Where the effort went (total {assessment.total_effort:g}):")
        lines += [
            f"  - {segment.segment_id}: effort {segment.effort:g}"
            + (f" - {'; '.join(segment.friction)}" if segment.friction else "")
            for segment in worst
        ]
        lines.append("")
    if plan.acceptance_criteria:
        lines.append("How we would know it worked:")
        lines += [f"  - {item}" for item in plan.acceptance_criteria]
        lines.append("")
    if plan.risks:
        lines.append("Risks:")
        lines += [f"  - {item}" for item in plan.risks]
        lines.append("")
    if plan.unverified_citations:
        lines.append("Source references the automatic check could not confirm:")
        lines += [f"  - {item}" for item in plan.unverified_citations]
    else:
        lines.append("Every source file and line the plan cites was checked and found.")

    attachments: list[str] = []
    if (run_dir / "plan.md").exists():
        attachments.append(str(run_dir / "plan.md"))
    shots = friction_screenshots(run_dir, assessment)
    attachments += [str(path) for path in shots]
    if shots:
        lines += ["", "Attached screenshots show the steps where the user hit an error or a dialog:"]
        lines += [f"  - {path.name}" for path in shots]
    return subject, "\n".join(lines) + "\n", attachments
