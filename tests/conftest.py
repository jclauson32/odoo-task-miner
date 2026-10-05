"""Shared test setup.

The project's `.env` holds real SMTP credentials, and `config.settings()`
loads it into the process. This fixture makes sure no test can send a real
email, push to a real remote, or write to the real audit log.
"""

from __future__ import annotations

import pytest

DELIVERY_ENV = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SSL", "SMTP_FROM",
    "REPORT_EMAIL_TO",
)


@pytest.fixture(autouse=True)
def no_outward_side_effects(monkeypatch, tmp_path):
    # Blank rather than delete: python-dotenv never overrides a variable that
    # is already set, so a blank one stays blank even if .env loads later.
    for name in DELIVERY_ENV:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("GIT_REMOTE", "odoo-miner-test-no-such-remote")
    monkeypatch.setenv("PR_BASE", "main")
    monkeypatch.setenv("ODOO_MINER_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
