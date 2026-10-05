"""planner_agent: decide whether to change the workflow, and plan the change.

Open-ended work - explore Odoo's source, weigh options, keep a todo list,
delegate research - so this is a Deep Agent rather than a single call. Odoo's
source is mounted read-only; the run directory is writable so the agent can
leave `plan.md` behind for a human.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .config import load_prompt, model_for, settings, trace_config
from .contracts import Plan
from .tools import odoo_source

PLAN_PROMPT = (
    "Read /run/segments.json, /run/traces.json and /run/assessment.json, decide "
    "what (if anything) is worth changing, write /run/plan.md for a human, and "
    "return the plan."
)


def build_planner(run_dir: Path, model: Optional[str] = None):
    """The deep agent. `/odoo/` is read-only; `/run/` is the run's artifacts."""
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    s = settings()
    routes = {"/run/": FilesystemBackend(root_dir=str(run_dir), virtual_mode=True)}
    permissions = []
    if odoo_source.source_available():
        routes["/odoo/"] = FilesystemBackend(root_dir=str(s.odoo_source_abs), virtual_mode=True)
        permissions.append(
            FilesystemPermission(operations=["write"], paths=["/odoo/**"], mode="deny")
        )

    return create_deep_agent(
        model=model or model_for("planner"),
        tools=[odoo_source.find_method, odoo_source.find_button, odoo_source.find_view_fields],
        system_prompt=load_prompt("planner"),
        subagents=[{
            "name": "odoo-source-researcher",
            "description": (
                "Answers one specific question about how Odoo 18 implements "
                "something, citing files and lines."
            ),
            "system_prompt": load_prompt("planner_researcher"),
            "tools": [odoo_source.find_method, odoo_source.find_button, odoo_source.read_source],
            "model": model_for("researcher"),
        }],
        backend=CompositeBackend(default=StateBackend(), routes=routes),
        permissions=permissions or None,
        response_format=Plan,
        name="planner_agent",
    )


def run_planner(run_dir: Path, agent: Any = None, run: str = "adhoc") -> Plan:
    """Plan a run. Expects segments/traces/assessment to be in `run_dir`."""
    run_dir = Path(run_dir)
    missing = [
        name for name in ("segments.json", "traces.json", "assessment.json")
        if not (run_dir / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"{run_dir} is missing {', '.join(missing)}; run the earlier stages first."
        )

    agent = agent or build_planner(run_dir)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": PLAN_PROMPT}]},
        config=trace_config(run, "planner"),
    )
    plan = result["structured_response"]
    if not isinstance(plan, Plan):
        plan = Plan.model_validate(plan)
    return plan


def run_planner_path(run_dir: Path, out: Path, agent: Any = None, run: str = "adhoc") -> Plan:
    plan = run_planner(run_dir, agent=agent, run=run)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return plan
