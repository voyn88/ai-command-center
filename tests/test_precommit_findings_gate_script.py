"""`scripts/precommit_findings_gate.py` — the pre-commit early-tracking gate
for the Wave-2 Audit engine (VOYN-MIN-RTD). Loaded and exercised in-process
with `audit_service.run_audit` monkeypatched, so these tests assert the
gate's own contract (which severities block, exit codes, the sensitive-project
and unknown-check paths) without depending on ruff output or a real database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from command_center.api import audit_schemas as a
from command_center.api import audit_service
from command_center.api.models import AuditFinding, AuditRun

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "precommit_findings_gate.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("precommit_findings_gate_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finding(**overrides) -> AuditFinding:
    base = dict(
        id="f1", run_id="r1", category="security", severity="high",
        summary="hardcoded secret", file_path="a.py", loc="10:4", owner="security",
    )
    base.update(overrides)
    return AuditFinding(**base)


def _run_result(findings: list[AuditFinding]) -> a.AuditRunResult:
    run = AuditRun(id="r1", project_ref="AICC", status="completed", finding_count=len(findings))
    return a.AuditRunResult(run=run, findings=findings, deduped=0)


def test_blocking_findings_filters_by_severity(script):
    findings = [_finding(severity="high"), _finding(severity="low"), _finding(severity="critical")]
    blocked = script.blocking_findings(findings, frozenset({"high", "critical"}))
    assert {f.severity for f in blocked} == {"high", "critical"}


def test_main_blocks_commit_on_high_severity_finding(script, monkeypatch):
    monkeypatch.setattr(
        script.audit_service, "run_audit", lambda payload: _run_result([_finding(severity="high")])
    )
    exit_code = script.main([])
    assert exit_code == 1


def test_main_allows_commit_when_nothing_blocking(script, monkeypatch):
    monkeypatch.setattr(
        script.audit_service, "run_audit", lambda payload: _run_result([_finding(severity="low")])
    )
    exit_code = script.main([])
    assert exit_code == 0


def test_main_allows_commit_when_no_findings_at_all(script, monkeypatch):
    monkeypatch.setattr(script.audit_service, "run_audit", lambda payload: _run_result([]))
    assert script.main([]) == 0


def test_main_respects_custom_fail_on_severities(script, monkeypatch):
    monkeypatch.setattr(
        script.audit_service, "run_audit", lambda payload: _run_result([_finding(severity="medium")])
    )
    assert script.main([]) == 0
    assert script.main(["--fail-on", "medium"]) == 1


def test_main_skips_sensitive_project_cleanly(script, monkeypatch, capsys):
    def boom(payload):
        raise audit_service.SensitiveProjectRefError(f"audit for sensitive project {payload.project!r}")

    monkeypatch.setattr(script.audit_service, "run_audit", boom)
    exit_code = script.main(["--project", "BANK"])
    assert exit_code == 0
    assert "skipped" in capsys.readouterr().out


def test_main_reports_unknown_check_as_failure(script, monkeypatch, capsys):
    def boom(payload):
        raise KeyError(f"unknown check {payload.checks!r}")

    monkeypatch.setattr(script.audit_service, "run_audit", boom)
    exit_code = script.main(["--checks", "nope"])
    assert exit_code == 1
    assert capsys.readouterr().err


def test_main_forwards_project_and_checks_flags(script, monkeypatch):
    seen: dict = {}

    def fake_run_audit(payload):
        seen["project"] = payload.project
        seen["checks"] = payload.checks
        return _run_result([])

    monkeypatch.setattr(script.audit_service, "run_audit", fake_run_audit)
    script.main(["--project", "AIOS", "--checks", "lint", "security"])
    assert seen == {"project": "AIOS", "checks": ["lint", "security"]}
