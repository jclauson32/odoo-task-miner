"""Append-only audit log for human decisions and outward actions.

Every plan approval, every tool approval, and every action that leaves the
machine (a push, a pull request, an email) is appended here as one JSON line:
when, who, what, and how it turned out. Nothing in the code rewrites or
deletes the file.

The location is `ODOO_MINER_AUDIT_LOG`, default `out/audit.jsonl`.
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
        **{key: value for key, value in details.items() if value is not None},
    }
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
