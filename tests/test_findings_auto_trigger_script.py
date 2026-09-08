"""`scripts/findings_auto_trigger.py` — the short-interval runtime trigger for
the Wave-2 Audit engine (VOYN-MIN-RTD). Loaded and exercised in-process (like
`tests/test_delivery_tooling.py` loads `scripts/evidence.py`) with
`audit_service.auto_trigger` monkeypatched, so these tests assert the CLI's
own wiring — argument handling, per-project iteration, exit codes — without
depending on ruff output or a real database.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "findings_auto_trigger.py"


@pytest.fixture
def script(monkeypatch):
    spec = importlib.util.spec_from_file_location("findings_auto_trigger_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(module, *, project: str, ran: bool, reason: str | None = None, findings=None):
    return module.a.AutoTriggerResult(
        project=project, ran=ran, reason=reason, findings=findings or [], deduped=0
    )


def test_run_once_reports_one_summary_per_project(script, monkeypatch):
    calls: list[str] = []

    def fake_auto_trigger(payload):
        calls.append(payload.project)
        return _result(script, project=payload.project, ran=True)

    monkeypatch.setattr(script.audit_service, "auto_trigger", fake_auto_trigger)
    summaries = script.run_once(["AICC", "AIOS"], min_interval_seconds=None)

    assert calls == ["AICC", "AIOS"]
    assert [s["project"] for s in summaries] == ["AICC", "AIOS"]
    assert all(s["ran"] for s in summaries)


def test_run_once_surfaces_skip_reason(script, monkeypatch):
    monkeypatch.setattr(
        script.audit_service,
        "auto_trigger",
        lambda payload: _result(script, project=payload.project, ran=False, reason="not_due"),
    )
    summaries = script.run_once(["AICC"], min_interval_seconds=None)
    assert summaries == [
        {"project": "AICC", "ran": False, "reason": "not_due", "finding_count": 0, "deduped": 0}
    ]


def test_run_once_passes_min_interval_override_through(script, monkeypatch):
    seen: list[int | None] = []

    def fake_auto_trigger(payload):
        seen.append(payload.min_interval_seconds)
        return _result(script, project=payload.project, ran=True)

    monkeypatch.setattr(script.audit_service, "auto_trigger", fake_auto_trigger)
    script.run_once(["AICC"], min_interval_seconds=42)
    assert seen == [42]


def test_main_defaults_to_every_non_sensitive_project(script, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        script.audit_service,
        "auto_trigger",
        lambda payload: seen.append(payload.project) or _result(script, project=payload.project, ran=True),
    )
    exit_code = script.main([])
    assert exit_code == 0
    assert "BANK" not in seen
    assert "LEGAL" not in seen
    assert "AICC" in seen


def test_main_respects_explicit_project_flags(script, monkeypatch, capsys):
    monkeypatch.setattr(
        script.audit_service,
        "auto_trigger",
        lambda payload: _result(script, project=payload.project, ran=True),
    )
    exit_code = script.main(["--project", "AICC", "--project", "ESF"])
    assert exit_code == 0
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [p["project"] for p in printed] == ["AICC", "ESF"]


def test_main_reports_unknown_check_as_failure(script, monkeypatch, capsys):
    def boom(payload):
        raise KeyError(f"unknown check {payload.checks!r}")

    monkeypatch.setattr(script.audit_service, "auto_trigger", boom)
    exit_code = script.main(["--project", "AICC"])
    assert exit_code == 1
    assert "error" in capsys.readouterr().err
