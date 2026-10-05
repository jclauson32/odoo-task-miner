"""Tools that send work out of the machine: a branch, a pull request, an email.

The builder gates all three with `interrupt_on`, so a person approves each one
even after approving the plan. They also refuse on their own:

- module names are validated before they reach a path or a git ref;
- nothing is pushed to the base branch - only `feat/<module>`;
- the push never touches the caller's working tree: it commits in a
  temporary git worktree, so your branch, index and uncommitted work stay put;
- email only goes to `REPORT_EMAIL_TO`, which the caller cannot override.

Every attempt, successful or not, is written to the audit log.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from .. import audit
from ..config import settings

PROTECTED_BRANCHES = frozenset({"main", "master"})
MODULE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
LOCAL_SMTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
STARTTLS_PORTS = frozenset({25, 587})

MAX_ATTACHMENT_MB = 10
MAX_TOTAL_ATTACHMENTS_MB = 20
GIT_TIMEOUT = 300
SMTP_TIMEOUT = 60

# Files a build leaves in the module folder that do not belong in a commit.
NOT_COMMITTED = shutil.ignore_patterns("__pycache__", "*.pyc", "tests.log", ".DS_Store")

mimetypes.add_type("text/markdown", ".md")


# ----------------------------------------------------------------- helpers


def validate_module(module: str) -> str | None:
    """None if `module` is a safe Odoo module name, else why it is not."""
    if not MODULE_NAME_RE.fullmatch(module or ""):
        return (
            f"refusing: {module!r} is not a valid Odoo module name "
            "(lowercase letters, digits and underscores, starting with a letter)."
        )
    return None


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return 127, "git is not installed."
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s."
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _gh(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, cwd=str(repo)
        )
    except FileNotFoundError:
        return 127, "the GitHub CLI (gh) is not installed."
    except subprocess.TimeoutExpired:
        return 124, f"gh {' '.join(args[:2])} timed out after {timeout}s."
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _tail(text: str, limit: int = 800) -> str:
    return text.strip()[-limit:]


def _repo_root(repo_dir: str | None) -> Path | None:
    start = Path(repo_dir) if repo_dir else Path.cwd()
    code, out = _git(start, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if code == 0 else None


def _remote() -> str:
    return os.environ.get("GIT_REMOTE") or "origin"


def _base() -> str:
    return os.environ.get("PR_BASE") or "main"


def _remote_branch_exists(root: Path, remote: str, branch: str) -> bool:
    code, _ = _git(root, "ls-remote", "--exit-code", "--heads", remote, branch)
    return code == 0


def _github_repo(root: Path, remote: str) -> str | None:
    """`owner/name` for a GitHub remote, or None for anything else."""
    code, url = _git(root, "remote", "get-url", remote)
    match = GITHUB_REMOTE_RE.search(url.strip()) if code == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


# ----------------------------------------------------------------- push


def git_push_feature_branch(module: str, title: str, repo_dir: str | None = None) -> str:
    """Commit addons/<module> on branch feat/<module> and push it.

    The commit is made in a temporary git worktree, so the checkout you are
    working in keeps its branch, index and uncommitted changes. If the branch
    already exists on the remote, the new commit goes on top of it - a
    reviewer's commits are never overwritten; otherwise it starts from the
    base branch. Only addons/<module> is committed, and the module folder on
    the branch is made to match your local one exactly.

    Args:
        module: module directory name under addons/, e.g. "purchase_bill_date_default".
        title: commit subject.
        repo_dir: repository to push from. Defaults to the current directory.

    Returns:
        The branch and commit that were pushed, or why nothing was.
    """
    settings()                                   # make sure .env is loaded
    result = _push(module, title or f"Add {module}", repo_dir)
    audit.record(
        "git_push_feature_branch",
        "ok" if result.startswith(("pushed", "nothing to push")) else "refused",
        module=module, branch=f"feat/{module}", remote=_remote(), result=result,
    )
    return result


def _push(module: str, title: str, repo_dir: str | None) -> str:
    problem = validate_module(module)
    if problem:
        return problem
    branch, base, remote = f"feat/{module}", _base(), _remote()
    if branch in PROTECTED_BRANCHES or branch == base:
        return f"refusing: {branch} is a protected branch."

    root = _repo_root(repo_dir)
    if root is None:
        return "refusing: not inside a git repository."
    source = root / "addons" / module
    if not source.is_dir() or not any(source.iterdir()):
        return f"refusing: {source} does not exist or is empty."

    code, out = _git(root, "fetch", "--quiet", remote, base)
    if code != 0:
        return f"could not fetch {remote}/{base}: {_tail(out)}"
    on_remote = _remote_branch_exists(root, remote, branch)
    if on_remote:
        code, out = _git(root, "fetch", "--quiet", remote, branch)
        if code != 0:
            return f"could not fetch {remote}/{branch}: {_tail(out)}"
    start = f"{remote}/{branch}" if on_remote else f"{remote}/{base}"

    holder = Path(tempfile.mkdtemp(prefix="odoo-miner-push-"))
    worktree = holder / "tree"
    try:
        code, out = _git(root, "worktree", "add", "--detach", "--quiet", str(worktree), start)
        if code != 0:
            return f"could not create a worktree from {start}: {_tail(out)}"

        target = worktree / "addons" / module
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=NOT_COMMITTED)

        code, out = _git(worktree, "add", "--all", "--", f"addons/{module}")
        if code != 0:
            return f"could not stage addons/{module}: {_tail(out)}"
        code, _ = _git(worktree, "diff", "--cached", "--quiet")
        if code == 0:
            where = f"{remote}/{branch}" if on_remote else f"{remote}/{base}"
            return f"nothing to push: addons/{module} already matches {where}."

        code, out = _git(worktree, "commit", "--quiet", "-m", title)
        if code != 0:
            return f"commit failed: {_tail(out)}"
        code, commit = _git(worktree, "rev-parse", "HEAD")
        commit = commit.strip()

        code, out = _git(worktree, "push", "--quiet", remote, f"HEAD:refs/heads/{branch}")
        if code != 0:
            return f"committed {commit[:12]} but the push to {remote}/{branch} failed: {_tail(out)}"
    finally:
        _git(root, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(holder, ignore_errors=True)
        _git(root, "worktree", "prune")

    return f"pushed {branch} at {commit[:12]} to {remote}; your working tree was not touched."


# ----------------------------------------------------------------- pull request


def open_pull_request(module: str, title: str, body: str, repo_dir: str | None = None) -> str:
    """Open a pull request from feat/<module> into the base branch.

    Idempotent: if a pull request for the branch is already open, returns its
    URL instead of opening a second one. Push the branch first.

    Args:
        module: module directory name under addons/.
        title: pull request title.
        body: pull request description (Markdown): what changed, why, the test
            result and the effort before and after.
        repo_dir: repository to work from. Defaults to the current directory.

    Returns:
        The pull request URL, or why none was opened.
    """
    settings()
    result = _open_pr(module, title, body, repo_dir)
    audit.record(
        "open_pull_request",
        "ok" if result.startswith(("opened", "already open")) else "refused",
        module=module, branch=f"feat/{module}", base=_base(), result=result,
    )
    return result


def _open_pr(module: str, title: str, body: str, repo_dir: str | None) -> str:
    problem = validate_module(module)
    if problem:
        return problem
    root = _repo_root(repo_dir)
    if root is None:
        return "refusing: not inside a git repository."

    branch, base, remote = f"feat/{module}", _base(), _remote()
    repo = _github_repo(root, remote)
    if repo is None:
        return f"refusing: remote {remote!r} is not a GitHub repository."
    if not _remote_branch_exists(root, remote, branch):
        return f"no {branch} on {remote}; push it first with git_push_feature_branch."

    code, out = _gh(
        root, "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
        "--json", "url", "--jq", ".[0].url // empty",
    )
    if code != 0:
        return f"could not check for an existing pull request: {_tail(out)}"
    if out.strip():
        return f"already open: {out.strip()}"

    code, out = _gh(
        root, "pr", "create", "--repo", repo, "--base", base, "--head", branch,
        "--title", title or f"Add {module}", "--body", body or "",
    )
    if code != 0:
        return f"could not open the pull request: {_tail(out)}"
    urls = [line for line in out.split() if line.startswith("https://")]
    return f"opened {urls[-1] if urls else out.strip()}"


# ----------------------------------------------------------------- email


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def send_report_email(subject: str, body: str, attachments: list[str] | None = None) -> str:
    """Email a report to the configured address, with files attached.

    The recipient is REPORT_EMAIL_TO and cannot be set by the caller, so an
    agent cannot mail anyone else. Connects with implicit TLS when SMTP_SSL
    is true or the port is 465, otherwise upgrades with STARTTLS; it refuses
    to send over an unencrypted connection to anything but localhost.

    Args:
        subject: email subject.
        body: plain-text body.
        attachments: file paths to attach - screenshots, plan.md, test logs.

    Returns:
        What was sent and attached, or why nothing was.
    """
    settings()
    result = _send(subject, body, attachments or [])
    audit.record(
        "send_report_email",
        "ok" if result.startswith("sent") else "refused",
        to=os.environ.get("REPORT_EMAIL_TO", ""), subject=subject, result=result,
    )
    return result


def _build_message(
    sender: str, to: str, subject: str, body: str, attachments: list[str]
) -> tuple[EmailMessage, list[str], list[str]]:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    domain = parseaddr(sender)[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body)

    attached: list[str] = []
    skipped: list[str] = []
    budget = MAX_TOTAL_ATTACHMENTS_MB * 1024 * 1024
    for raw in attachments:
        path = Path(raw)
        if not path.is_file():
            skipped.append(f"{path.name} (missing)")
            continue
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_MB * 1024 * 1024:
            skipped.append(f"{path.name} (over {MAX_ATTACHMENT_MB} MB)")
            continue
        if size > budget:
            skipped.append(f"{path.name} (total over {MAX_TOTAL_ATTACHMENTS_MB} MB)")
            continue
        budget -= size
        content_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        data = path.read_bytes()
        if maintype == "text":
            message.add_attachment(
                data.decode("utf-8", errors="replace"), subtype=subtype, filename=path.name
            )
        else:
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        attached.append(path.name)
    return message, attached, skipped


def _hang_up(smtp: smtplib.SMTP) -> None:
    """End the session without letting a failed goodbye undo a delivered message.

    Once the server has accepted the message, a failure on QUIT (a dropped
    connection, a timeout) changes nothing about delivery. Using the client as
    a context manager would raise that error after the fact, report the email
    as unsent, and invite an agent to retry and send a duplicate.
    """
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        smtp.close()


def _send(subject: str, body: str, attachments: list[str]) -> str:
    to = (os.environ.get("REPORT_EMAIL_TO") or "").strip()
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not to:
        return "not sent: REPORT_EMAIL_TO is not set."
    if not host:
        return "not sent: SMTP_HOST is not set."

    use_ssl = _truthy(os.environ.get("SMTP_SSL"))
    raw_port = (os.environ.get("SMTP_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else (465 if use_ssl else 587)
    except ValueError:
        return f"not sent: SMTP_PORT {raw_port!r} is not a number."
    if use_ssl and port in STARTTLS_PORTS:
        # Implicit TLS against a STARTTLS port: the server resets the connection
        # mid-handshake, which surfaces as an opaque "connection reset by peer".
        return (
            f"not sent: SMTP_SSL is on but SMTP_PORT is {port}, a STARTTLS port. "
            "Use port 465 for implicit TLS, or set SMTP_SSL=false. (An exported "
            "SMTP_PORT in your shell takes precedence over .env.)"
        )
    use_ssl = use_ssl or port == 465

    user = os.environ.get("SMTP_USER") or ""
    password = os.environ.get("SMTP_PASSWORD") or ""
    sender = (os.environ.get("SMTP_FROM") or user or to).strip()
    if "@" not in parseaddr(sender)[1]:
        return f"not sent: the sender {sender!r} is not an email address; set SMTP_FROM."

    message, attached, skipped = _build_message(sender, to, subject, body, attachments)
    context = ssl.create_default_context()
    smtp: smtplib.SMTP | None = None
    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=context)
                smtp.ehlo()
            elif host not in LOCAL_SMTP_HOSTS:
                return (
                    f"not sent: {host} offers no TLS, and reports are not sent "
                    "over an unencrypted connection."
                )
        if user and password:
            smtp.login(user, password)
        refused = smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        return f"not sent: the SMTP server rejected the credentials ({exc.smtp_code})."
    except (smtplib.SMTPException, OSError) as exc:
        return f"not sent: {type(exc).__name__}: {exc}"
    finally:
        if smtp is not None:
            _hang_up(smtp)

    if refused:
        return f"not sent: the server refused {', '.join(refused)}."
    note = f"sent to {to} with {len(attached)} attachment(s)"
    note += f": {', '.join(attached)}" if attached else ""
    return note + (f"; skipped {', '.join(skipped)}" if skipped else "")


TOOLS = [git_push_feature_branch, open_pull_request, send_report_email]
