"""Append-only audit log of approvals and outward actions.

Each approval, push, pull request and email is one JSON line: when, who, what
and the outcome. The file is `ODOO_MINER_AUDIT_LOG`, `out/audit.jsonl` by default.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("out") / "audit.jsonl"


def audit_path() -> Path:
    """Where the audit log is written."""
    return Path(os.environ.get("ODOO_MINER_AUDIT_LOG") or DEFAULT_PATH)


@lru_cache(maxsize=1)
def actor() -> str:
    """Who is acting: the git identity if there is one, else the OS user."""
    try:
        name = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    return name or getpass.getuser()


def record(action: str, outcome: str, **details: Any) -> None:
    """Append one entry. Never raises: an audit failure must not hide the result."""
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "actor": actor(),
        "action": action,
        "outcome": outcome,
        # The run it happened in, so one run's entries can be read on their own.
        "run": os.environ.get("ODOO_MINER_RUN") or None,
        **{key: value for key, value in details.items() if value is not None},
    }
    entry = {key: value for key, value in entry.items() if value is not None}
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


def read(path: Path | None = None) -> list[dict]:
    """All entries, oldest first."""
    target = path or audit_path()
    if not target.exists():
        return []
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
