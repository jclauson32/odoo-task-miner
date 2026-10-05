"""builder_agent: implement the approved plan, prove it works, deliver it.

Long multi-step work with file edits, so a Deep Agent. Its filesystem is
scoped to the new module and the run directory - it cannot write anywhere
else - and the two tools that send work out of the machine pause for a human.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_prompt, model_for, settings, trace_config
from .contracts import BuildResult, Plan
from .tools import delivery, odoo_ops, odoo_source

# Every tool that sends work out of the machine pauses for a person first.
GATED_TOOLS = ("git_push_feature_branch", "open_pull_request", "send_report_email")

BUILD_PROMPT = (
    "Read /run/plan.json and implement it. Write the module, install it, run "
    "its tests until they pass, write /run/after_recording.json and replay it, "
    "measure the effort before and after, then push the branch, open the pull "
    "request and send the report. Return the build result."
)


def build_builder(run_dir: Path, plan: Plan, model: str | None = None):
    """The deep agent, scoped to one module."""
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    if not plan.module_name:
        raise ValueError("The plan has no module_name, so there is nothing to build.")

    s = settings()
    module_dir = Path.cwd() / s.addons_dir / plan.module_name
    module_dir.mkdir(parents=True, exist_ok=True)

    routes = {
        f"/addons/{plan.module_name}/": FilesystemBackend(
            root_dir=str(module_dir), virtual_mode=True
        ),
        "/run/": FilesystemBackend(root_dir=str(run_dir), virtual_mode=True),
    }

    return create_deep_agent(
        model=model or model_for("builder"),
        tools=[
            odoo_source.read_source,
            *odoo_ops.TOOLS,
            *delivery.TOOLS,
        ],
        system_prompt=load_prompt("builder"),
        backend=CompositeBackend(default=StateBackend(), routes=routes),
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/odoo/**"], mode="deny")
        ],
        # A person approves each push, pull request and email, even after
        # approving the plan.
        interrupt_on={name: True for name in GATED_TOOLS},
        response_format=BuildResult,
        name="builder_agent",
    )


def run_builder(
    run_dir: Path, plan: Plan, agent: Any = None, run: str = "adhoc"
) -> BuildResult:
    run_dir = Path(run_dir)
    agent = agent or build_builder(run_dir, plan)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": BUILD_PROMPT}]},
        config=trace_config(run, "builder", module=plan.module_name or ""),
    )
    built = result["structured_response"]
    if not isinstance(built, BuildResult):
        built = BuildResult.model_validate(built)
    return built


def run_builder_path(
    run_dir: Path, plan_path: Path, out: Path, agent: Any = None, run: str = "adhoc"
) -> BuildResult:
    plan = Plan.model_validate_json(Path(plan_path).read_text(encoding="utf-8"))
    built = run_builder(run_dir, plan, agent=agent, run=run)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(built.model_dump_json(indent=2), encoding="utf-8")
    return built
