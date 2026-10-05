"""Pre-flight checks: is this machine ready to record, replay and analyze?

`odoo-miner doctor` runs every check and says what to do about each failure.
Nothing here changes anything; it only looks. Secrets are reported as set or
missing, never printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_WORDS = ("KEY", "PASSWORD", "TOKEN", "SECRET")

Status = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    status: Status
    name: str
    detail: str
    fix: str = ""


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO_ROOT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def check_python() -> Check:
    # pyproject's requires-python already refuses anything older than 3.11.
    return Check("ok", "Python", ".".join(map(str, sys.version_info[:3])))


def check_anthropic() -> Check:
    from .agents.config import settings

    s = settings()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return Check("fail", "Anthropic API key", "ANTHROPIC_API_KEY is not set",
                     "Copy .env.example to .env and fill in ANTHROPIC_API_KEY.")
    return Check("ok", "Anthropic API key", f"set; models {s.model} / {s.fast_model}")


def check_langsmith() -> Check:
    if not _truthy(os.environ.get("LANGSMITH_TRACING")):
        return Check("warn", "LangSmith tracing", "off", "Set LANGSMITH_TRACING=true to trace runs.")
    if not os.environ.get("LANGSMITH_API_KEY"):
        return Check("fail", "LangSmith tracing", "on, but LANGSMITH_API_KEY is not set",
                     "Set LANGSMITH_API_KEY, or LANGSMITH_TRACING=false.")
    return Check("ok", "LangSmith tracing", f"project {os.environ.get('LANGSMITH_PROJECT') or 'default'}")


def check_odoo() -> Check:
    from .agents.config import settings

    url = f"{settings().odoo_url}/web/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                return Check("ok", "Odoo", f"healthy at {settings().odoo_url}")
    except (urllib.error.URLError, OSError):
        pass
    return Check("fail", "Odoo", f"no answer at {url}",
                 "Start Docker (systemctl --user start docker-desktop), then docker compose up -d.")


def check_databases() -> Check:
    from .agents.config import settings

    db = settings().odoo_db
    code, out = _run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "odoo", "-d", "postgres",
                      "-tAc", f"SELECT datname FROM pg_database WHERE datname IN ('{db}', '{db}_snapshot')"])
    if code != 0:
        return Check("fail", "Databases", "could not reach Postgres in docker compose",
                     "docker compose up -d")
    names = set(out.split())
    missing = [name for name in (db, f"{db}_snapshot") if name not in names]
    if not missing:
        return Check("ok", "Databases", f"{db} and {db}_snapshot present")
    fix = ("./scripts/init_db.sh && python scripts/seed.py && ./scripts/snapshot_db.sh"
           if db in missing else "./scripts/snapshot_db.sh")
    return Check("fail", "Databases", f"missing: {', '.join(missing)}", fix)


def check_source() -> Check:
    from .agents.config import settings

    root = settings().odoo_source_abs
    if (root / "addons" / "purchase").is_dir():
        return Check("ok", "Odoo source", str(settings().odoo_source))
    return Check("warn", "Odoo source", f"not found at {settings().odoo_source}",
                 f"git clone --depth 1 -b 18.0 https://github.com/odoo/odoo {settings().odoo_source}  "
                 "(without it the tracer cannot cite code)")


def check_replay() -> Check:
    if shutil.which("node") is None:
        return Check("fail", "Replay", "node is not on PATH", "Install Node.js 18 or newer.")
    if not (REPO_ROOT / "replay" / "node_modules").is_dir():
        return Check("fail", "Replay", "replay dependencies missing", "cd replay && npm install")
    _, version = _run(["node", "--version"])
    return Check("ok", "Replay", f"node {version.strip()}, dependencies installed")


def check_email() -> Check:
    to, host = os.environ.get("REPORT_EMAIL_TO"), os.environ.get("SMTP_HOST")
    if not to or not host:
        return Check("warn", "Email", "REPORT_EMAIL_TO or SMTP_HOST not set; reports will not send",
                     "Fill the SMTP_* settings in .env.")
    port = (os.environ.get("SMTP_PORT") or "").strip()
    ssl_on = _truthy(os.environ.get("SMTP_SSL"))
    if ssl_on and port in {"25", "587"}:
        return Check("fail", "Email", f"SMTP_SSL is on but SMTP_PORT is {port}, a STARTTLS port",
                     "Use port 465 with SMTP_SSL=true, or SMTP_SSL=false.")
    return Check("ok", "Email", f"to {to} via {host}:{port or 'default'}{' (TLS)' if ssl_on or port == '465' else ''}")


def check_github() -> Check:
    if shutil.which("gh") is None:
        return Check("warn", "GitHub CLI", "gh is not installed; pull requests cannot be opened",
                     "Install the GitHub CLI and run gh auth login.")
    code, out = _run(["gh", "auth", "status"])
    if code != 0:
        return Check("warn", "GitHub CLI", "not logged in", "gh auth login")
    account = next((line.split("account")[-1].split("(")[0].strip()
                    for line in out.splitlines() if "Logged in" in line), "")
    return Check("ok", "GitHub CLI", f"logged in{(' as ' + account) if account else ''}")


def check_shadowed() -> Check:
    """Variables exported in the shell beat .env; say so when they differ."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return Check("ok", "Environment", ".env not read (python-dotenv missing)")
    path = REPO_ROOT / ".env"
    if not path.exists():
        return Check("warn", "Environment", "no .env file", "cp .env.example .env")

    shadowed = []
    for key, value in dotenv_values(path).items():
        live = os.environ.get(key)
        if value and live is not None and live != value:
            secret = any(word in key for word in SECRET_WORDS)
            shadowed.append(f"{key}" if secret else f"{key}={live} (.env says {value})")
    if not shadowed:
        return Check("ok", "Environment", ".env values are in effect")
    return Check("warn", "Environment", "shell overrides .env: " + "; ".join(shadowed),
                 "unset " + " ".join(item.split("=")[0] for item in shadowed) + "  (or open a new terminal)")


CHECKS = [
    check_python, check_shadowed, check_anthropic, check_langsmith, check_odoo, check_databases,
    check_source, check_replay, check_email, check_github,
]


def run_checks() -> list[Check]:
    from .agents.config import settings

    settings()  # load .env first, exactly as the stages do
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # a broken check must not hide the others
            results.append(Check("fail", check.__name__.removeprefix("check_"), f"check crashed: {exc}"))
    return results
