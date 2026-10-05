"""Tools that send work out of the machine.

Both of these are gated by `interrupt_on` in the builder, so a human approves
each one even after the plan was approved. They also refuse on their own:
never `main`, and never an email address other than the configured one.
"""

from __future__ import annotations

import os
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

PROTECTED_BRANCHES = {"main", "master"}
MAX_ATTACHMENT_MB = 10


def _git(*args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def git_push_feature_branch(module: str, title: str) -> str:
    """Commit a module on a feature branch and push it to the configured remote.

    Refuses to touch main/master, and only ever stages the module's own folder.

    Args:
        module: module directory name under addons/.
        title: commit subject.

    Returns:
        The branch and commit, or why it did not push.
    """
    branch = f"feat/{module}"
    if branch in PROTECTED_BRANCHES:
        return f"refusing: {branch} is a protected branch."

    code, current = _git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return f"not a git repository: {current}"

    path = Path("addons") / module
    if not path.exists():
        return f"refusing: {path} does not exist."

    code, output = _git("checkout", "-B", branch)
    if code != 0:
        return f"could not create {branch}: {output}"

    code, output = _git("add", "--", str(path))
    if code != 0:
        return f"could not stage {path}: {output}"

    code, status = _git("diff", "--cached", "--name-only")
    if not status.strip():
        return f"nothing to commit under {path}; branch {branch} left in place."

    code, output = _git("commit", "-m", title)
    if code != 0:
        return f"commit failed: {output}"
    code, commit = _git("rev-parse", "HEAD")
    commit = commit.strip()

    remote = os.environ.get("GIT_REMOTE", "origin")
    code, output = _git("push", "-u", remote, branch, timeout=300)
    if code != 0:
        return f"committed {commit[:12]} on {branch} but push failed:\n{output[-1500:]}"
    return f"pushed {branch} at {commit[:12]} to {remote}."


def send_report_email(subject: str, body: str, attachments: Optional[list[str]] = None) -> str:
    """Email the run's report to the configured address.

    The recipient comes from REPORT_EMAIL_TO and cannot be overridden by the
    caller, so an agent cannot mail anyone else.

    Args:
        subject: email subject.
        body: plain-text body.
        attachments: file paths to attach (screenshots, plan.md).

    Returns:
        What was sent, or why it was not.
    """
    to = os.environ.get("REPORT_EMAIL_TO", "").strip()
    host = os.environ.get("SMTP_HOST", "").strip()
    if not to:
        return "not sent: REPORT_EMAIL_TO is not set."
    if not host:
        return "not sent: SMTP_HOST is not set."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("SMTP_USER", to)
    message["To"] = to
    message.set_content(body)

    attached, skipped = [], []
    for raw in attachments or []:
        path = Path(raw)
        if not path.is_file():
            skipped.append(f"{raw} (missing)")
            continue
        if path.stat().st_size > MAX_ATTACHMENT_MB * 1024 * 1024:
            skipped.append(f"{raw} (over {MAX_ATTACHMENT_MB}MB)")
            continue
        subtype = path.suffix.lstrip(".") or "octet-stream"
        maintype = "image" if subtype in {"png", "jpg", "jpeg", "gif"} else "application"
        message.add_attachment(
            path.read_bytes(), maintype=maintype,
            subtype="octet-stream" if maintype == "application" else subtype,
            filename=path.name,
        )
        attached.append(path.name)

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    try:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        return f"not sent: {type(exc).__name__}: {exc}"

    note = f"sent to {to} with {len(attached)} attachment(s): {', '.join(attached) or 'none'}"
    return note + (f"; skipped {', '.join(skipped)}" if skipped else "")


TOOLS = [git_push_feature_branch, send_report_email]
