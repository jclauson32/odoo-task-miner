"""planner_agent: decide whether to change the workflow, and plan the change.

Open-ended work - explore Odoo's source, weigh options, keep a todo list,
delegate research - so this is a Deep Agent rather than a single call. Odoo's
source is mounted read-only; the run directory is writable so the agent can
leave `plan.md` behind for a human.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import chat_model, load_prompt, settings, trace_config
from .contracts import Plan
from .tools import odoo_source

# `addons/x/y.py`, `addons/x/y.xml:56`, `odoo/addons/base/z.py:10-20`
_CITATION_RE = re.compile(
    r"(?<![\w/])((?:odoo/)?addons/[\w./-]+?\.(?:py|xml))(?::(\d+)(?:-(\d+))?)?"
)
# A backticked identifier, written plainly or as a call: `name`, `Model._post()`.
_IDENTIFIER_RE = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`")
# How far before a path to look for the identifier it is cited for.
_IDENTIFIER_WINDOW = 90


def check_citations(text: str, root: Path | None = None) -> list[str]:
    """Problems with the source citations in a plan. Empty when all of them check out.

    Checks what can be checked mechanically: the cited file exists, a cited
    line or range falls inside it, and the backticked identifier written just
    before a path (an XML id, a method) actually occurs in that file. A clean
    result does not make the reasoning right - judging that is the approver's
    job - but a fabricated or mis-filed citation does not reach them unflagged.
    """
    root = root or (odoo_source.settings().odoo_source_abs if odoo_source.source_available() else None)
    if root is None:
        return ["Odoo source checkout missing; citations were not checked."]

    problems: list[str] = []
    seen: set[tuple] = set()
    for match in _CITATION_RE.finditer(text):
        path, start, end = match.group(1), match.group(2), match.group(3)
        before = text[max(0, match.start() - _IDENTIFIER_WINDOW):match.start()]
        identifiers = _IDENTIFIER_RE.findall(before)
        claim = identifiers[-1] if identifiers else None
        key = (path, start, end, claim)
        if key in seen:
            continue
        seen.add(key)

        source = root / path
        if not source.is_file():
            problems.append(f"{path} does not exist.")
            continue
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        for number in (start, end):
            if number and not 1 <= int(number) <= len(lines):
                problems.append(f"{path}:{number} is past the end of the file ({len(lines)} lines).")
        if claim:
            symbol = claim.rsplit(".", 1)[-1]
            if symbol not in "\n".join(lines):
                problems.append(f"`{claim}` is cited in {path} but does not appear there.")
    return problems


PLAN_PROMPT = (
    "Read /run/segments.json, /run/traces.json and /run/assessment.json, decide "
    "what (if anything) is worth changing, write /run/plan.md for a human, and "
    "return the plan."
)


def build_planner(run_dir: Path, model: str | None = None):
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
        model=model or chat_model("planner"),
        tools=odoo_source.agent_tools("find_method", "find_button", "find_view_fields"),
        system_prompt=load_prompt("planner"),
        subagents=[{
            "name": "odoo-source-researcher",
            "description": (
                "Answers one specific question about how Odoo 18 implements "
                "something, citing files and lines."
            ),
            "system_prompt": load_prompt("planner_researcher"),
            "tools": odoo_source.agent_tools("find_method", "find_button", "read_source"),
            "model": chat_model("researcher"),
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

    written = run_dir / "plan.md"
    text = "\n".join([
        written.read_text(encoding="utf-8") if written.exists() else "",
        plan.summary, *plan.acceptance_criteria, *plan.risks,
        *(change.description for change in plan.changes),
    ])
    problems = check_citations(text)
    if not written.exists():
        problems.insert(0, "The planner did not write plan.md.")
    return plan.model_copy(update={"unverified_citations": problems})


def run_planner_path(run_dir: Path, out: Path, agent: Any = None, run: str = "adhoc") -> Plan:
    plan = run_planner(run_dir, agent=agent, run=run)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return plan
