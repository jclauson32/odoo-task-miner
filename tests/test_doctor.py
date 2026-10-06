"""Tests for `odoo-miner doctor`'s checks that do not need a running service."""

from __future__ import annotations

from odoo_miner import doctor


def test_email_on_ssl_with_a_starttls_port_fails(monkeypatch):
    """SSL on a STARTTLS port is reported as a failure."""
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_SSL", "true")
    monkeypatch.setenv("SMTP_PORT", "587")
    check = doctor.check_email()
    assert check.status == "fail" and "STARTTLS port" in check.detail


def test_email_on_465_passes(monkeypatch):
    """SSL on port 465 passes."""
    monkeypatch.setenv("REPORT_EMAIL_TO", "buyer@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_SSL", "true")
    monkeypatch.setenv("SMTP_PORT", "465")
    assert doctor.check_email().status == "ok"


def test_unconfigured_email_is_a_warning_not_a_failure():
    """Email is optional, so missing settings only warn."""
    assert doctor.check_email().status == "warn"   # conftest blanks the SMTP settings


def test_a_shell_variable_shadowing_env_is_reported_without_leaking_secrets(monkeypatch, tmp_path):
    """Shadowed variables are named; secret values are never printed."""
    (tmp_path / ".env").write_text("SMTP_PORT=465\nSMTP_PASSWORD=from-file\n")
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_PASSWORD", "from-shell")
    check = doctor.check_shadowed()
    assert check.status == "warn"
    assert "SMTP_PORT=587 (.env says 465)" in check.detail
    assert "SMTP_PASSWORD" in check.detail
    assert "from-shell" not in check.detail and "from-file" not in check.detail


def test_missing_api_key_fails(monkeypatch):
    """A missing Anthropic key fails the check."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert doctor.check_anthropic().status == "fail"


def test_a_crashing_check_does_not_hide_the_others(monkeypatch):
    """A check that raises is reported, and the rest still run."""
    def broken():
        """A check that always raises."""
        raise RuntimeError("boom")
    broken.__name__ = "check_broken"
    monkeypatch.setattr(doctor, "CHECKS", [broken, doctor.check_python])
    results = doctor.run_checks()
    assert [r.status for r in results] == ["fail", "ok"]
    assert "boom" in results[0].detail
