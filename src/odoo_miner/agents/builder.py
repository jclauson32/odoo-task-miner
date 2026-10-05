"""builder_agent: implement the approved plan, prove it works, deliver it.

Long multi-step work with file edits, so a Deep Agent. Its filesystem is
scoped to the new module and the run directory - it cannot write anywhere
else - and every tool that sends work out of the machine pauses for a person.

The agent sees virtual paths (`/run/plan.md`, `/addons/<module>/...`), the
same ones its filesystem tools use. Its Odoo and delivery tools are bound to
this run: they translate those paths to real ones, and they only ever act on
the module the person approved.
"""

from __future__ import annotations

from collections.abc import Callable
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


def path_resolver(run_dir: Path, module_dir: Path, module: str) -> Callable[[str], str]:
    """Map the agent's virtual paths to real ones, refusing anything that escapes.

    `/run/...` is the run directory and `/addons/<module>/...` the module;
    any other path is returned unchanged for the tool to judge.
    """
    routes = {"/run/": Path(run_dir).resolve(), f"/addons/{module}/": Path(module_dir).resolve()}

    def resolve(path: str) -> str:
        for prefix, root in routes.items():
            if path == prefix.rstrip("/") or path.startswith(prefix):
                target = (root / path[len(prefix):]).resolve() if path.startswith(prefix) else root
                if target != root and root not in target.parents:
                    raise ValueError(f"{path} points outside {prefix}")
                return str(target)
        return path

    return resolve


def bound_tools(run_dir: Path, module_dir: Path, module: str) -> list[Callable[..., str]]:
    """The builder's Odoo and delivery tools, bound to one run and one module."""
    resolve = path_resolver(run_dir, module_dir, module)

    def _same_module(requested: str) -> str | None:
        if requested != module:
            return f"refusing: the approved plan is for {module!r}, not {requested!r}."
        return None

    def install_module() -> str:
        """Install (or update) the module in the demo database and restart Odoo."""
        return odoo_ops.install_module(module)

    def run_module_tests() -> str:
        """Run the module's Odoo tests; the full log is saved as /run/tests.log."""
        return odoo_ops.run_module_tests(module, run_dir=str(run_dir))

    def replay_workflow(recording: str = "/run/after_recording.json", output: str = "/run/after") -> str:
        """Restore the database, install the module, and replay a recording.

        Args:
            recording: the Recorder JSON to replay, e.g. /run/after_recording.json.
            output: folder for the replay's artifacts, e.g. /run/after.
        """
        return odoo_ops.replay_workflow(resolve(recording), resolve(output), module=module)

    def measure_effort(output: str = "/run/after") -> str:
        """Segment and score a replayed run; returns its total effort.

        Args:
            output: the replay's folder, e.g. /run/after.
        """
        return odoo_ops.measure_effort(resolve(output))

    def git_push_feature_branch(module_name: str, title: str) -> str:
        """Commit /addons/<module> on feat/<module> and push it. Pauses for approval.

        Args:
            module_name: the module being delivered; must be the approved one.
            title: commit subject.
        """
        return _same_module(module_name) or delivery.git_push_feature_branch(module, title)

    def open_pull_request(module_name: str, title: str, body: str) -> str:
        """Open a pull request for feat/<module>. Pauses for approval.

        Args:
            module_name: the module being delivered; must be the approved one.
            title: pull request title.
            body: what changed and why, the test result, and effort before and after.
        """
        return _same_module(module_name) or delivery.open_pull_request(module, title, body)

    def send_report_email(subject: str, body: str, attachments: list[str] | None = None) -> str:
        """Email the report to the configured address. Pauses for approval.

        Args:
            subject: email subject.
            body: plain-text body, including the pull request link.
            attachments: paths such as /run/plan.md or /run/after/screenshots/step-012.png.
        """
        try:
            files = [resolve(path) for path in attachments or []]
        except ValueError as exc:
            return f"not sent: {exc}"
        return delivery.send_report_email(subject, body, files)

    return [
        install_module, run_module_tests, replay_workflow, measure_effort,
        git_push_feature_branch, open_pull_request, send_report_email,
    ]


def build_builder(run_dir: Path, plan: Plan, model: Any = None):
    """The deep agent, scoped to one module."""
    from deepagents import FilesystemPermission, create_deep_agent
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    if not plan.module_name:
        raise ValueError("The plan has no module_name, so there is nothing to build.")
    problem = delivery.validate_module(plan.module_name)
    if problem:
        raise ValueError(problem)

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
        tools=[odoo_source.read_source, *bound_tools(Path(run_dir), module_dir, plan.module_name)],
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
