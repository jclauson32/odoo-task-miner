"""Tests for the tools that send work out of the machine, and the gates in front of them.

Nothing here reaches the network. Email goes to an SMTP server started on
localhost for the test, pushes go to a bare repository in a temp folder, and
the GitHub CLI is replaced by a recorder. conftest.py blanks the real SMTP
settings before every test.
"""

from __future__ import annotations

import email
import email.policy
import inspect
import smtplib
import socket
import struct
import subprocess
import zlib
from pathlib import Path
from typing import TypedDict

import pytest

from odoo_miner.agents import audit
from odoo_miner.agents.builder import GATED_TOOLS
from odoo_miner.agents.tools import delivery
from odoo_miner.pipeline.graph import is_tool_gate, record_tool_decision, resume_value


def _png() -> bytes:
    """A valid 1x1 PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        """One PNG chunk, with its length and checksum."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff\xff")) + chunk(b"IEND", b""))


def _git(*args: str, cwd=None) -> str:
    """Run git and return its output; a failure fails the test."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


# ----------------------------------------------------------------- email


def _free_port() -> int:
    """A free TCP port on localhost."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def smtp_server(monkeypatch):
    """A real SMTP server on localhost that keeps what it receives."""
    from aiosmtpd.controller import Controller

    received = []

    class Handler:
        """Keeps every message the server receives."""

        async def handle_DATA(self, server, session, envelope):
            """Accept a message and keep it."""
            received.append(envelope)
            return "250 Message accepted"

    controller = Controller(Handler(), hostname="127.0.0.1", port=_free_port())
    controller.start()
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", str(controller.port))
    monkeypatch.setenv("SMTP_SSL", "false")
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    monkeypatch.setenv("SMTP_FROM", "odoo-miner <reports@example.com>")
    yield received
    controller.stop()


def test_email_arrives_with_screenshots_attached(smtp_server, tmp_path):
    """The email arrives with its headers, body and attachments intact."""
    shot = tmp_path / "step-050.png"
    shot.write_bytes(_png())
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\nDefault the bill date to today.\n")

    result = delivery.send_report_email(
        "Build report", "Tests passed.", [str(shot), str(plan), str(tmp_path / "gone.png")]
    )

    assert result.startswith("sent to buyer@example.com with 2 attachment(s)"), result
    assert "gone.png (missing)" in result
    [envelope] = smtp_server
    assert envelope.rcpt_tos == ["buyer@example.com"]

    message = email.message_from_bytes(envelope.content, policy=email.policy.default)
    assert message["From"] == "odoo-miner <reports@example.com>"
    assert message["Subject"] == "Build report"
    assert message["Date"] and message["Message-ID"]
    assert message.get_body(("plain",)).get_content().strip() == "Tests passed."

    parts = {part.get_filename(): part for part in message.iter_attachments()}
    assert parts["step-050.png"].get_content_type() == "image/png"
    assert parts["step-050.png"].get_content() == shot.read_bytes()
    assert parts["plan.md"].get_content_type() == "text/markdown"
    assert "Default the bill date" in parts["plan.md"].get_content()


class FakeSMTP:
    """Records the conversation instead of connecting anywhere."""

    def __init__(self, log, starttls=True, fail_login=False, reset_on_quit=False):
        """Choose whether STARTTLS is offered and whether login or QUIT fails."""
        self.log, self.supports_starttls = log, starttls
        self.fail_login, self.reset_on_quit = fail_login, reset_on_quit

    def quit(self):
        """Log the goodbye, or fail like a dropped connection."""
        if self.reset_on_quit:
            raise ConnectionResetError(104, "Connection reset by peer")
        self.log.append("quit")

    def close(self):
        """Log a hard close."""
        self.log.append("close")

    def ehlo(self):
        """Log the greeting."""
        self.log.append("ehlo")

    def has_extn(self, name):
        """Offer STARTTLS when configured to."""
        return self.supports_starttls and name == "starttls"

    def starttls(self, context=None):
        """Log the TLS upgrade."""
        self.log.append("starttls")

    def login(self, user, password):
        """Log the login, or reject the credentials."""
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")
        self.log.append(("login", user))

    def send_message(self, message):
        """Log the send; no recipient is refused."""
        self.log.append("send")
        return {}


@pytest.fixture
def remote_smtp(monkeypatch):
    """Settings for a remote server, with both connection classes faked."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "api_token")
    monkeypatch.setenv("SMTP_PASSWORD", "s3cret-password")
    monkeypatch.setenv("SMTP_FROM", "Reports <reports@example.com>")
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    log: list = []
    options: dict = {}
    connections: list = []

    def make(kind):
        """A stand-in for one of smtplib's connection classes."""
        def connect(host, port, timeout=None, context=None):
            """Record the connection and return a fake server."""
            connections.append((kind, host, port, context is not None))
            return FakeSMTP(log, **options)
        return connect

    monkeypatch.setattr(smtplib, "SMTP_SSL", make("ssl"))
    monkeypatch.setattr(smtplib, "SMTP", make("plain"))
    return log, options, connections


def test_port_465_uses_implicit_tls(monkeypatch, remote_smtp):
    """Port 465 connects with implicit TLS."""
    log, _, connections = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "465")
    assert delivery.send_report_email("s", "b").startswith("sent")
    assert connections == [("ssl", "smtp.example.com", 465, True)]
    assert log == [("login", "api_token"), "send", "quit"]


def test_ssl_flag_uses_implicit_tls_on_any_port(monkeypatch, remote_smtp):
    """SMTP_SSL=true uses implicit TLS on any port."""
    _, _, connections = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "2465")
    monkeypatch.setenv("SMTP_SSL", "true")
    delivery.send_report_email("s", "b")
    assert connections[0][0] == "ssl"


def test_port_587_upgrades_with_starttls_before_logging_in(monkeypatch, remote_smtp):
    """Port 587 upgrades with STARTTLS before logging in."""
    log, _, connections = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "587")
    assert delivery.send_report_email("s", "b").startswith("sent")
    assert connections[0][0] == "plain"
    assert log == ["ehlo", "starttls", "ehlo", ("login", "api_token"), "send", "quit"]


def test_refuses_an_unencrypted_remote_server(monkeypatch, remote_smtp):
    """A remote server without TLS gets neither the password nor the email."""
    log, options, _ = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "25")
    options["starttls"] = False
    result = delivery.send_report_email("s", "b")
    assert "offers no TLS" in result
    assert ("login", "api_token") not in log and "send" not in log


def test_a_failed_goodbye_does_not_undo_a_delivery(monkeypatch, remote_smtp):
    """A failure on QUIT after the server accepted the message still counts as sent."""
    log, options, _ = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "465")
    options["reset_on_quit"] = True
    result = delivery.send_report_email("s", "b")
    assert result.startswith("sent to buyer@example.com"), result
    assert log[-2:] == ["send", "close"]


@pytest.mark.parametrize("port", ["587", "25"])
def test_implicit_tls_on_a_starttls_port_is_caught_before_connecting(monkeypatch, remote_smtp, port):
    """SSL on a STARTTLS port is refused before connecting, with a clear reason."""
    log, _, connections = remote_smtp
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.setenv("SMTP_SSL", "true")
    result = delivery.send_report_email("s", "b")
    assert f"SMTP_PORT is {port}, a STARTTLS port" in result
    assert connections == [] and log == []


def test_rejected_credentials_do_not_leak_the_password(monkeypatch, remote_smtp):
    """A failed login is reported without the password."""
    _, options, _ = remote_smtp
    monkeypatch.setenv("SMTP_PORT", "465")
    options["fail_login"] = True
    result = delivery.send_report_email("s", "b")
    assert "rejected the credentials (535)" in result
    assert "s3cret-password" not in result


def test_sender_must_be_an_address(monkeypatch, remote_smtp):
    """A token-style SMTP user such as "api_token" is not used as the sender."""
    monkeypatch.setenv("SMTP_FROM", "")
    assert "set SMTP_FROM" in delivery.send_report_email("s", "b")


def test_refuses_without_a_recipient_or_host(monkeypatch):
    """Nothing is sent without REPORT_EMAIL_TO and SMTP_HOST."""
    assert "REPORT_EMAIL_TO" in delivery.send_report_email("s", "b")
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    assert "SMTP_HOST" in delivery.send_report_email("s", "b")


def test_bad_port_is_reported(monkeypatch):
    """A port that is not a number is reported."""
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    assert "not a number" in delivery.send_report_email("s", "b")


def test_the_caller_cannot_choose_the_recipient():
    """send_report_email takes no recipient."""
    assert "to" not in inspect.signature(delivery.send_report_email).parameters


def test_every_send_attempt_is_audited():
    """Every send attempt is audited, refused ones included."""
    delivery.send_report_email("Weekly report", "b")
    [entry] = audit.read()
    assert entry["action"] == "send_report_email"
    assert entry["outcome"] == "refused"
    assert entry["subject"] == "Weekly report"


# ----------------------------------------------------------------- push


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working repository whose `origin` is a bare repository in tmp_path."""
    remote = tmp_path / "remote.git"
    _git("init", "--quiet", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git("init", "--quiet", "-b", "main", str(work))
    for key, value in [("user.name", "Test Builder"), ("user.email", "builder@example.com"),
                       ("commit.gpgsign", "false")]:
        _git("-C", str(work), "config", key, value)
    (work / "README.md").write_text("base\n")
    _git("-C", str(work), "add", "README.md")
    _git("-C", str(work), "commit", "--quiet", "-m", "base")
    _git("-C", str(work), "remote", "add", "origin", str(remote))
    _git("-C", str(work), "push", "--quiet", "origin", "main")
    monkeypatch.setenv("GIT_REMOTE", "origin")
    return work, remote


def _module(work, name="demo_mod", version="1"):
    """Write a minimal module, with a build log that must not be committed."""
    folder = work / "addons" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").write_text("")
    (folder / "__manifest__.py").write_text(
        f"{{'name': 'Demo', 'version': '{version}', 'depends': ['base']}}\n"
    )
    (folder / "tests.log").write_text("build noise\n")
    return folder


def test_push_commits_only_the_module_and_leaves_your_tree_alone(repo):
    """The push commits only the module and leaves the checkout as it was."""
    work, remote = repo
    _module(work)
    (work / "README.md").write_text("an edit you have not committed\n")
    (work / "scratch.txt").write_text("an untracked file\n")
    status = _git("-C", str(work), "status", "--porcelain")
    head = _git("-C", str(work), "rev-parse", "HEAD")

    result = delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))

    assert result.startswith("pushed feat/demo_mod"), result
    assert _git("-C", str(work), "branch", "--show-current") == "main"
    assert _git("-C", str(work), "rev-parse", "HEAD") == head
    assert _git("-C", str(work), "status", "--porcelain") == status
    assert len(_git("-C", str(work), "worktree", "list").splitlines()) == 1

    files = _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "feat/demo_mod").splitlines()
    assert "addons/demo_mod/__manifest__.py" in files
    assert "addons/demo_mod/tests.log" not in files
    assert "scratch.txt" not in files
    assert _git("--git-dir", str(remote), "show", "feat/demo_mod:README.md") == "base"


def test_a_second_push_builds_on_the_branch_instead_of_overwriting_it(repo, tmp_path):
    """A second push goes on top of the branch and keeps a reviewer's commit."""
    work, remote = repo
    _module(work)
    delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))

    review = tmp_path / "review"
    _git("clone", "--quiet", "-b", "feat/demo_mod", str(remote), str(review))
    for key, value in [("user.name", "Reviewer"), ("user.email", "reviewer@example.com"),
                       ("commit.gpgsign", "false")]:
        _git("-C", str(review), "config", key, value)
    (review / "REVIEW.md").write_text("looks good\n")
    _git("-C", str(review), "add", "REVIEW.md")
    _git("-C", str(review), "commit", "--quiet", "-m", "review notes")
    _git("-C", str(review), "push", "--quiet", "origin", "feat/demo_mod")
    reviewer_commit = _git("-C", str(review), "rev-parse", "HEAD")

    _module(work, version="2")
    result = delivery.git_push_feature_branch("demo_mod", "Bump demo module", repo_dir=str(work))

    assert result.startswith("pushed"), result
    _git("--git-dir", str(remote), "merge-base", "--is-ancestor", reviewer_commit, "feat/demo_mod")
    manifest = _git("--git-dir", str(remote), "show", "feat/demo_mod:addons/demo_mod/__manifest__.py")
    assert "'version': '2'" in manifest


def test_an_unchanged_module_is_not_pushed_again(repo):
    """Pushing an unchanged module does nothing."""
    work, _ = repo
    _module(work)
    delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))
    assert delivery.git_push_feature_branch(
        "demo_mod", "Add demo module", repo_dir=str(work)
    ).startswith("nothing to push")


@pytest.mark.parametrize("name", ["../etc", "Bad-Name", "", "a/b", "_x", "x" * 70])
def test_push_refuses_unsafe_module_names(repo, name):
    """Unsafe module names are refused."""
    work, _ = repo
    assert delivery.git_push_feature_branch(name, "t", repo_dir=str(work)).startswith("refusing")


def test_push_refuses_a_missing_module(repo):
    """A module folder that does not exist is refused."""
    work, _ = repo
    assert "does not exist" in delivery.git_push_feature_branch("absent", "t", repo_dir=str(work))


def test_push_never_targets_the_base_branch(repo, monkeypatch):
    """The base branch is never pushed to."""
    work, _ = repo
    _module(work)
    monkeypatch.setenv("PR_BASE", "feat/demo_mod")
    assert "protected" in delivery.git_push_feature_branch("demo_mod", "t", repo_dir=str(work))


def test_every_push_attempt_is_audited(repo):
    """Every push attempt is audited."""
    work, _ = repo
    _module(work)
    delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))
    [entry] = audit.read()
    assert (entry["action"], entry["outcome"], entry["branch"]) == (
        "git_push_feature_branch", "ok", "feat/demo_mod"
    )


# ----------------------------------------------------------------- pull request


@pytest.fixture
def github(monkeypatch, repo):
    """`gh` replaced by a recorder; the remote treated as a GitHub repository."""
    work, _ = repo
    calls: list = []
    replies = {"list": (0, ""), "create": (0, "https://github.com/acme/odoo-addons/pull/7\n")}

    def fake_gh(root, *args, timeout=0):
        """Record a gh call and return the scripted reply."""
        calls.append(args)
        return replies["list"] if args[:2] == ("pr", "list") else replies["create"]

    monkeypatch.setattr(delivery, "_gh", fake_gh)
    monkeypatch.setattr(delivery, "_github_repo", lambda root, remote: "acme/odoo-addons")
    return work, calls, replies


def _flag(args, name):
    """The value given for a command-line flag."""
    return args[args.index(name) + 1]


def test_opens_a_pull_request_into_the_base_branch(github):
    """The pull request goes from feat/<module> into the base branch."""
    work, calls, _ = github
    _module(work)
    delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))

    result = delivery.open_pull_request("demo_mod", "Default the bill date", "Why: ...", repo_dir=str(work))

    assert result == "opened https://github.com/acme/odoo-addons/pull/7"
    [create] = [c for c in calls if c[:2] == ("pr", "create")]
    assert _flag(create, "--repo") == "acme/odoo-addons"
    assert _flag(create, "--base") == "main"
    assert _flag(create, "--head") == "feat/demo_mod"
    assert _flag(create, "--title") == "Default the bill date"
    assert _flag(create, "--body") == "Why: ..."


def test_does_not_open_a_second_pull_request(github):
    """An open pull request is reused, not duplicated."""
    work, calls, replies = github
    _module(work)
    delivery.git_push_feature_branch("demo_mod", "Add demo module", repo_dir=str(work))
    replies["list"] = (0, "https://github.com/acme/odoo-addons/pull/7\n")

    result = delivery.open_pull_request("demo_mod", "t", "b", repo_dir=str(work))

    assert result == "already open: https://github.com/acme/odoo-addons/pull/7"
    assert not [c for c in calls if c[:2] == ("pr", "create")]


def test_pull_request_needs_the_branch_pushed_first(github):
    """No pull request is opened before the branch is pushed."""
    work, calls, _ = github
    _module(work)
    assert "push it first" in delivery.open_pull_request("demo_mod", "t", "b", repo_dir=str(work))
    assert calls == []


def test_pull_request_refuses_a_remote_that_is_not_github(repo):
    """A remote that is not on GitHub is refused."""
    work, _ = repo
    assert "not a GitHub repository" in delivery.open_pull_request(
        "demo_mod", "t", "b", repo_dir=str(work)
    )


@pytest.mark.parametrize("url", [
    "https://github.com/acme/odoo-addons.git",
    "https://github.com/acme/odoo-addons",
    "git@github.com:acme/odoo-addons.git",
    "ssh://git@github.com/acme/odoo-addons.git",
])
def test_github_remote_urls_are_recognised(url):
    """HTTPS and SSH GitHub remotes are recognised."""
    match = delivery.GITHUB_REMOTE_RE.search(url)
    assert match and f"{match.group(1)}/{match.group(2)}" == "acme/odoo-addons"


# ----------------------------------------------------------------- the gates


def test_every_outward_tool_is_gated():
    """Every delivery tool is in the builder's gate list."""
    assert {tool.__name__ for tool in delivery.TOOLS} <= set(GATED_TOOLS)


def test_each_gate_gets_an_answer_in_its_own_shape():
    """Tool gates and the plan gate each get the answer shape they expect."""
    tool_gate = {"action_requests": [{"name": "git_push_feature_branch", "args": {}},
                                     {"name": "send_report_email", "args": {}}],
                 "review_configs": []}
    assert is_tool_gate(tool_gate)
    assert resume_value(tool_gate, True) == {"decisions": [{"type": "approve"}] * 2}
    assert resume_value(tool_gate, False, "not yet") == {
        "decisions": [{"type": "reject", "message": "not yet"}] * 2
    }
    plan_gate = {"plan_summary": "Default the bill date", "decision": "customize"}
    assert not is_tool_gate(plan_gate)
    assert resume_value(plan_gate, True, "ok") == {"approved": True, "notes": "ok"}


class _State(TypedDict, total=False):
    """State for the one-node test pipeline."""

    out: str


def _gated_pipeline(script, executed):
    """A one-node pipeline that runs an agent with the real gate list, like build_node."""
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class ToolCallingFake(GenericFakeChatModel):
        """A scripted chat model that accepts tools."""

        def bind_tools(self, tools, **kwargs):
            """Ignore the tools; the script decides the calls."""
            return self

    def stub(name):
        """A stand-in for the gated tool `name` that records its run."""
        def tool(module: str) -> str:
            """Record that the gated tool ran."""
            executed.append(name)
            return f"{name} ran"
        tool.__name__ = name
        tool.__doc__ = f"Stand-in for {name}."
        return tool

    def build(state):
        """Run the agent until it finishes or pauses."""
        agent = create_agent(
            model=ToolCallingFake(messages=script),
            tools=[stub(name) for name in GATED_TOOLS],
            middleware=[HumanInTheLoopMiddleware(interrupt_on={n: True for n in GATED_TOOLS})],
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "deliver"}]},
                              config={"metadata": {"stage": "builder"}, "tags": ["odoo-miner"]})
        return {"out": result["messages"][-1].content}

    graph = StateGraph(_State)
    graph.add_node("build", build)
    graph.add_edge(START, "build")
    graph.add_edge("build", END)
    return graph.compile(checkpointer=InMemorySaver())


def _push_request():
    """A model message asking to push the module."""
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "name": "git_push_feature_branch", "args": {"module": "demo_mod"}, "id": "call-1",
    }])


@pytest.mark.parametrize("approved", [True, False])
def test_an_outward_action_waits_for_a_person(approved):
    """A push pauses the pipeline and runs only if a person approves."""
    from langchain_core.messages import AIMessage
    from langgraph.types import Command

    executed: list = []
    script = iter([_push_request(), AIMessage(content="finished")])
    pipeline = _gated_pipeline(script, executed)
    config = {"configurable": {"thread_id": f"gate-{approved}"}}

    paused = pipeline.invoke({}, config)

    assert "__interrupt__" in paused, "the push must pause the pipeline"
    assert executed == [], "nothing may run before a person answers"
    payload = pipeline.get_state(config).interrupts[0].value
    assert is_tool_gate(payload)
    assert payload["action_requests"][0]["name"] == "git_push_feature_branch"

    record_tool_decision(payload, approved, "reviewed", "test-run")
    finished = pipeline.invoke(Command(resume=resume_value(payload, approved, "reviewed")), config)

    assert finished["out"] == "finished"
    assert executed == (["git_push_feature_branch"] if approved else [])
    [entry] = audit.read()
    assert entry["action"] == "approve_git_push_feature_branch"
    assert entry["outcome"] == ("approved" if approved else "rejected")


# ----------------------------------------------------------------- the builder's bound tools


@pytest.fixture
def bound(tmp_path):
    """The builder's tools, bound to a temporary run and module."""
    from odoo_miner.agents.builder import bound_tools

    run_dir, module_dir = tmp_path / "run", tmp_path / "addons" / "demo_mod"
    run_dir.mkdir()
    module_dir.mkdir(parents=True)
    tools = {tool.__name__: tool for tool in bound_tools(run_dir, module_dir, "demo_mod")}
    return tools, run_dir, module_dir


def test_every_gate_names_a_tool_the_builder_really_has(bound):
    """interrupt_on matches by name; a renamed tool would silently lose its gate."""
    tools, _, _ = bound
    assert set(GATED_TOOLS) <= set(tools)


def test_virtual_paths_resolve_inside_the_run_and_module(tmp_path):
    """Virtual paths map into the run and module folders and cannot escape them."""
    from odoo_miner.agents.builder import path_resolver

    run_dir, module_dir = tmp_path / "run", tmp_path / "addons" / "demo_mod"
    resolve = path_resolver(run_dir, module_dir, "demo_mod")
    assert resolve("/run/plan.md") == str((run_dir / "plan.md").resolve())
    assert resolve("/run/after/screenshots/step-012.png") == str(
        (run_dir / "after/screenshots/step-012.png").resolve()
    )
    assert resolve("/addons/demo_mod/__manifest__.py") == str(
        (module_dir / "__manifest__.py").resolve()
    )
    assert resolve("/run") == str(run_dir.resolve())
    with pytest.raises(ValueError, match="outside"):
        resolve("/run/../../etc/passwd")


def test_the_builder_can_only_deliver_the_approved_module(bound, monkeypatch):
    """The delivery tools refuse any module but the approved one."""
    tools, _, _ = bound
    called = []
    monkeypatch.setattr(delivery, "git_push_feature_branch", lambda *a, **k: called.append(a) or "pushed")
    monkeypatch.setattr(delivery, "open_pull_request", lambda *a, **k: called.append(a) or "opened")

    assert "approved plan is for 'demo_mod'" in tools["git_push_feature_branch"]("other_mod", "t")
    assert "approved plan is for 'demo_mod'" in tools["open_pull_request"]("other_mod", "t", "b")
    assert called == []
    assert tools["git_push_feature_branch"]("demo_mod", "Add demo") == "pushed"


def test_email_attachments_are_translated_from_virtual_paths(bound, monkeypatch):
    """Attachment paths are mapped from virtual to real and cannot escape."""
    tools, run_dir, _ = bound
    sent = {}
    monkeypatch.setattr(
        delivery, "send_report_email",
        lambda subject, body, files: sent.update(files=files) or "sent",
    )
    tools["send_report_email"]("s", "b", ["/run/plan.md", "/run/after/screenshots/step-001.png"])
    assert sent["files"] == [
        str((run_dir / "plan.md").resolve()),
        str((run_dir / "after/screenshots/step-001.png").resolve()),
    ]
    assert "outside" in tools["send_report_email"]("s", "b", ["/run/../../../etc/passwd"])


# ----------------------------------------------------------------- the findings report


def test_report_is_built_from_the_run_and_attaches_the_friction_screenshots(tmp_path):
    """The report comes from the run's files and attaches the error screenshots."""
    from odoo_miner.agents.contracts import Assessment, Plan, SegmentAssessment, StepDifficulty
    from odoo_miner.agents.reporting import compose_report

    (tmp_path / "plan.json").write_text(Plan(
        decision="no_change", summary="The bill date is a deliberate control.",
        risks=["Defaulting the date would weaken an audit control."],
    ).model_dump_json())
    (tmp_path / "plan.md").write_text("# Plan\n")
    (tmp_path / "assessment.json").write_text(Assessment(session="s", total_effort=8, segments=[
        SegmentAssessment(segment_id="s10", effort=5, friction=["error: bill date required"], steps=[
            StepDifficulty(step_index=50, score=4, signals={"error": 1.0}),
            StepDifficulty(step_index=51, score=1, signals={}),
        ]),
    ]).model_dump_json())
    shots = tmp_path / "screenshots"
    shots.mkdir()
    for index in (50, 51):
        (shots / f"step-{index:03d}.png").write_bytes(_png())

    subject, body, attachments = compose_report(tmp_path)

    assert subject.startswith("odoo-miner: no change")
    assert "deliberate control" in body and "bill date required" in body
    assert "checked and found" in body
    assert [Path(a).name for a in attachments] == ["plan.md", "step-050.png"]


def test_report_needs_a_plan(tmp_path):
    """No report without plan.json."""
    from odoo_miner.agents.reporting import compose_report

    with pytest.raises(FileNotFoundError, match="plan.json"):
        compose_report(tmp_path)


def test_before_and_after_screenshots_get_distinct_names(smtp_server, tmp_path):
    """Screenshots with the same name get distinct attachment names."""
    before = tmp_path / "run" / "screenshots" / "step-006.png"
    after = tmp_path / "run" / "after" / "screenshots" / "step-006.png"
    for path in (before, after):
        path.parent.mkdir(parents=True)
        path.write_bytes(_png())

    result = delivery.send_report_email("s", "b", [str(before), str(after)])

    message = email.message_from_bytes(smtp_server[0].content, policy=email.policy.default)
    names = [part.get_filename() for part in message.iter_attachments()]
    assert names == ["run-screenshots-step-006.png", "after-screenshots-step-006.png"], names
    assert "run-screenshots-step-006.png" in result


def test_audit_entries_carry_the_run_and_the_command_filters_by_it(monkeypatch):
    """Entries record their run, and audit --run filters by it."""
    from typer.testing import CliRunner

    from odoo_miner.cli import app

    monkeypatch.setenv("ODOO_MINER_RUN", "bills")
    audit.record("approve_plan", "approved", notes="looks right")
    monkeypatch.setenv("ODOO_MINER_RUN", "other")
    audit.record("approve_plan", "rejected", notes="not this one")

    assert [e["run"] for e in audit.read()] == ["bills", "other"]
    result = CliRunner().invoke(app, ["audit", "--run", "bills"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "looks right" in result.output and "not this one" not in result.output


def test_audit_shows_the_reasons_whole_when_asked(monkeypatch):
    """--full prints a decision's notes whole."""
    from typer.testing import CliRunner

    from odoo_miner.cli import app

    reason = "Sent back: " + "the payables policy decides this. " * 8 + "END-OF-NOTES"
    audit.record("approve_plan", "sent back", notes=reason)
    short = CliRunner().invoke(app, ["audit"], env={"COLUMNS": "400"})
    whole = CliRunner().invoke(app, ["audit", "--full"], env={"COLUMNS": "400"})
    assert "END-OF-NOTES" not in short.output
    assert "END-OF-NOTES" in whole.output, whole.output
