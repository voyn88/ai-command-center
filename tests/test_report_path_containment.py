"""Regression for SEC-2: the report *write* path must stay under REPORTS_ROOT.

`report_path_for` builds `REPORTS_ROOT / project / f"{ts}_{task_id[:12]}_{agent}.md"`
from a run's `task_id`/`project`/`agent`. A hand-authored or imported `task_id`
such as "../../../../" contains path separators once truncated, so `Path`
joining walks out of REPORTS_ROOT on write. The read side
(`resolve_report_path`) already enforces containment; the write side did not.
"""

from __future__ import annotations

from command_center import agent_runner


def _resolved(run: dict, root):
    return agent_runner.report_path_for(run).resolve()


def test_malicious_task_id_cannot_escape_reports_root(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "REPORTS_ROOT", tmp_path)
    run = {
        "task_id": "../../../../pwn",
        "project": "AICC",
        "agent": "claude",
        "started_at": "2026-07-29T00:00:00",
    }
    resolved = _resolved(run, tmp_path)
    assert tmp_path.resolve() in resolved.parents, f"escaped REPORTS_ROOT: {resolved}"


def test_malicious_project_and_agent_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "REPORTS_ROOT", tmp_path)
    run = {
        "task_id": "AICC-1",
        "project": "../../root",
        "agent": "a/../../b",
        "started_at": "2026-07-29T00:00:00",
    }
    resolved = _resolved(run, tmp_path)
    assert tmp_path.resolve() in resolved.parents, f"escaped REPORTS_ROOT: {resolved}"


def test_legit_ids_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "REPORTS_ROOT", tmp_path)
    run = {
        "task_id": "AICC-AUDIT-001",
        "project": "AICC",
        "agent": "claude",
        "started_at": "2026-07-29T00:00:00",
    }
    path = agent_runner.report_path_for(run)
    assert path.parent == tmp_path / "AICC"
    assert path.name.endswith("_claude.md")
    assert "AICC-AUDIT-0" in path.name  # task_id truncated to 12 chars, unmodified


def test_known_project_name_uses_canonical_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_runner, "REPORTS_ROOT", tmp_path)
    run = {
        "task_id": "AICC-AUDIT-001",
        "project": "AI Command Center",
        "agent": "claude",
        "started_at": "2026-07-29T00:00:00",
    }

    assert agent_runner.report_path_for(run).parent == tmp_path / "AICC"
