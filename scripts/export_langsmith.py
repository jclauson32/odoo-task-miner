#!/usr/bin/env python3
"""Export the project's LangSmith runs to local files: cost, tokens and time per run and stage.

    python scripts/export_langsmith.py [--since 2026-10-01] [--out out/langsmith]

Writes runs.json (every pipeline run, with a breakdown per stage) and runs.csv
(one row per run). Reads LANGSMITH_API_KEY and LANGSMITH_PROJECT from .env.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from odoo_miner.agents.config import load_env  # noqa: E402


def _seconds(run) -> float | None:
    """How long a run took, if it has finished."""
    return round((run.end_time - run.start_time).total_seconds(), 1) if run.end_time else None


def _summary(run) -> dict:
    """The fields worth keeping from one LangSmith run."""
    metadata = (run.extra or {}).get("metadata") or {}
    return {
        "id": str(run.id),
        "name": run.name,
        "run": metadata.get("run"),
        "stage": metadata.get("stage"),
        "started": run.start_time.isoformat() if run.start_time else None,
        "seconds": _seconds(run),
        "status": run.status,
        "error": (run.error or "")[:300] or None,
        "tokens": run.total_tokens or 0,
        "cost_usd": round(float(run.total_cost or 0), 4),
    }


def _stages(client, root) -> list[dict]:
    """The outermost run of each stage inside one pipeline trace."""
    stages: dict[str, dict] = {}
    for run in client.list_runs(trace_id=root.trace_id):
        stage = ((run.extra or {}).get("metadata") or {}).get("stage")
        if not stage or run.id == root.id:
            continue
        seen = stages.get(stage)
        if seen is None or run.start_time < datetime.fromisoformat(seen["started"]):
            stages[stage] = _summary(run)
    return sorted(stages.values(), key=lambda s: s["started"])


def main() -> int:
    """Export the runs started since --since."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2026-10-01", help="Only runs started on or after this date.")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "langsmith")
    args = parser.parse_args()

    load_env()
    if not os.environ.get("LANGSMITH_API_KEY"):
        sys.exit("LANGSMITH_API_KEY is not set (see .env.example).")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from langsmith import Client

    client = Client()
    project = os.environ.get("LANGSMITH_PROJECT") or "default"
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    roots = sorted(
        client.list_runs(project_name=project, is_root=True, start_time=since),
        key=lambda r: r.start_time,
    )

    runs = []
    for root in roots:
        entry = _summary(root)
        entry["stages"] = _stages(client, root)
        runs.append(entry)
        print(f"{entry['started'][:16]}  {entry['name'][:50]:50}  ${entry['cost_usd']:.2f}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "runs.json").write_text(json.dumps({"project": project, "runs": runs}, indent=2))
    with (args.out / "runs.csv").open("w", newline="") as handle:
        fields = ["started", "name", "run", "status", "seconds", "tokens", "cost_usd", "error", "id"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(runs)
    total = sum(r["cost_usd"] for r in runs)
    print(f"{len(runs)} runs, ${total:.2f} in all → {args.out}/runs.json and runs.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
