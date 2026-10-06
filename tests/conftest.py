"""Shared test setup.

The project's `.env` holds real SMTP credentials and turns LangSmith tracing
on. The fixture below keeps every test from sending email, pushing, writing the
real audit log or sending a trace.
"""

from __future__ import annotations

import pytest

DELIVERY_ENV = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SSL", "SMTP_FROM",
    "REPORT_EMAIL_TO",
)


@pytest.fixture(autouse=True)
def no_outward_side_effects(monkeypatch, tmp_path):
    """Blank the email settings; point tracing, git and the audit log somewhere harmless."""
    # Blank rather than delete: python-dotenv never overrides a variable that
    # is already set, so a blank one stays blank even if .env loads later.
    for name in DELIVERY_ENV:
        monkeypatch.setenv(name, "")
    # .env turns LangSmith tracing on; a test run must not send traces.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("GIT_REMOTE", "odoo-miner-test-no-such-remote")
    monkeypatch.setenv("PR_BASE", "main")
    monkeypatch.setenv("ODOO_MINER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
