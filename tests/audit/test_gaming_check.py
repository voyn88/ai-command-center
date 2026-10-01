"""Tests for `GamingDetectionCheck` — the check-plumbing layer over
`command_center.audit.gaming_score`.

`runs_read.list_unified_runs` is monkeypatched (the module-level name
`command_center.audit.checks.gaming.runs_read` is the testability seam) so
these run without a real runtime db, the same seam `audit_service` documents
for its own dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import models
from command_center.audit.checks.gaming import GamingDetectionCheck
from command_center.audit.types import CheckContext, default_owner_for


def _ctx(tmp_path: Path, **options) -> CheckContext:
    return CheckContext(
        root=tmp_path, target=tmp_path, project="AICC", db_path=tmp_path / "runtime.db", options=options,
    )


def _clean_run(run_id: str = "run-clean") -> dict:
    return {
        "id": run_id,
        "project": "AICC",
        "duration_seconds": 900.0,
        "parsed": {
            "files_modified": ["a.py"],
            "files_created": [],
            "files_deleted": [],
            "verdict": models.VERDICT_APPROVED_FOR_COMMIT,
            "verdict_contradictory": False,
            "confidence": "high",
            "validation_result": "pytest -q: 42 passed",
        },
    }


def _fast_dirty_run(run_id: str = "run-gamed") -> dict:
    return {
        "id": run_id,
        "project": "AICC",
        "duration_seconds": 20.0,
        "parsed": {
            "files_modified": [f"f{i}.py" for i in range(25)],
            "files_created": [],
            "files_deleted": [],
            "verdict": models.VERDICT_APPROVED_FOR_COMMIT,
            "verdict_contradictory": False,
            "confidence": "none",
            "validation_result": None,
        },
    }


def test_flags_fast_dirty_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "command_center.audit.checks.gaming.runs_read.list_unified_runs",
        lambda db_path, *, root, limit=None: [_fast_dirty_run(), _clean_run()],
    )
    findings = GamingDetectionCheck().run(_ctx(tmp_path))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.category == "gaming"
    assert finding.owner == default_owner_for("gaming")
    assert "run-gam" in finding.summary
    assert finding.severity in ("medium", "high")


def test_clean_run_raises_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "command_center.audit.checks.gaming.runs_read.list_unified_runs",
        lambda db_path, *, root, limit=None: [_clean_run()],
    )
    assert GamingDetectionCheck().run(_ctx(tmp_path)) == []


def test_ignores_runs_from_other_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _fast_dirty_run()
    other["project"] = "OTHER"
    monkeypatch.setattr(
        "command_center.audit.checks.gaming.runs_read.list_unified_runs",
        lambda db_path, *, root, limit=None: [other],
    )
    assert GamingDetectionCheck().run(_ctx(tmp_path)) == []


def test_threshold_and_limit_are_tunable_via_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen_limit = {}

    def fake_list_unified_runs(db_path, *, root, limit=None):
        seen_limit["limit"] = limit
        return [_fast_dirty_run()]

    monkeypatch.setattr(
        "command_center.audit.checks.gaming.runs_read.list_unified_runs", fake_list_unified_runs
    )
    findings = GamingDetectionCheck().run(_ctx(tmp_path, gaming_run_limit=5, gaming_risk_threshold=0.99))
    assert seen_limit["limit"] == 5
    assert findings == []  # threshold raised above the run's actual risk


def test_missing_runtime_db_yields_info_finding_not_a_raise(tmp_path: Path) -> None:
    findings = GamingDetectionCheck().run(_ctx(tmp_path))
    assert len(findings) == 1
    assert findings[0].severity == "info"
    assert findings[0].category == "gaming"
