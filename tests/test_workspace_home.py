"""Tests for `command_center/workspace_home.py` — the Workspace Home read model and
its sensitivity redaction stage (WORKSPACE_HOME_ARCHITECTURE.md §5/§5.1/§14).

Plain pytest, no Streamlit anywhere in this module or the module under test.
Imports artifact/report discovery exclusively via `command_center.artifacts`
(indirectly, through `workspace_home` itself), never `app.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import activity_log, agent_runner, models, project_config, workspace_home
from command_center.runtime.api import ExecutionCenterAPI


@pytest.fixture(autouse=True)
def _isolated_artifact_dirs(isolated_data_dir, monkeypatch):
    """Point `workspace_home.REPORTS_DIR` at the same directory the autouse
    `isolated_reports_dir` fixture already points `agent_runner.REPORTS_ROOT` /
    `runtime.reports.REPORTS_ROOT` at, so a report file discovered by
    `workspace_home` and a `report_path`/`report.path` string written by those
    modules resolve to the same relative path. `GENERATED_DIR` has no existing
    writer to match, so it just gets its own throwaway directory."""
    reports_dir = isolated_data_dir / "reports"
    generated_dir = isolated_data_dir / "generated"
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(workspace_home, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(workspace_home, "GENERATED_DIR", generated_dir)
    return reports_dir, generated_dir


def _api(tmp_path) -> ExecutionCenterAPI:
    return ExecutionCenterAPI(db_path=tmp_path / "runtime.db")


def test_duration_handles_mixed_legacy_and_aware_timestamps():
    assert workspace_home._duration_seconds(
        "2026-08-08T12:00:00",
        "2026-08-08T12:01:00+00:00",
    ) == 60.0


# --------------------------------------------------------------------------
# Empty-state / degrade-gracefully scenarios (§7.1/F3, §18)
# --------------------------------------------------------------------------


def test_snapshot_all_projects_unconfigured_renders_without_exception(tmp_path):
    """The default fresh-checkout state: every project's repository_path is None."""
    api = _api(tmp_path)
    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)

    assert {p["id"] for p in snapshot["projects"]} == set(models.PROJECT_IDS)
    assert len(snapshot["projects"]) == len(models.PROJECT_IDS)
    for project in snapshot["projects"]:
        assert project["repository_path"] is None
        assert workspace_home_state_for(snapshot, project["id"]) == "unconfigured"
    assert snapshot["active_runs"] == []
    assert snapshot["recent_runs"] == []
    assert snapshot["reports"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["recent_activity"] == []


def test_snapshot_discovers_live_artifacts_next_to_overridden_data_dir(
    tmp_path, monkeypatch
):
    """A frozen desktop bundle lives outside the operational workspace.

    ``AICC_DATA_DIR`` points it at the live runtime store, so Workspace Home must
    discover the sibling ``generated/`` and ``reports/`` trees instead of the
    empty copies (or absent paths) inside the application bundle.
    """
    live_root = tmp_path / "live-workspace"
    data_dir = live_root / "data"
    generated_dir = live_root / "generated" / "AIOS"
    reports_dir = live_root / "reports" / "AIOS"
    data_dir.mkdir(parents=True)
    generated_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    (generated_dir / "task_implementation.md").write_text("# Task\n")
    (reports_dir / "run.md").write_text("# Report\n")

    monkeypatch.setenv("AICC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("AICC_GENERATED_ROOT", raising=False)
    monkeypatch.delenv("AICC_REPORTS_ROOT", raising=False)
    monkeypatch.setattr(workspace_home, "GENERATED_DIR", tmp_path / "bundled" / "generated")
    monkeypatch.setattr(workspace_home, "REPORTS_DIR", tmp_path / "bundled" / "reports")

    snapshot = workspace_home.build_workspace_home_snapshot(
        execution_center_api=_api(tmp_path)
    )

    assert [entry["project"] for entry in snapshot["artifacts"]] == ["AIOS"]
    assert [entry["project"] for entry in snapshot["reports"]] == ["AIOS"]


def workspace_home_state_for(snapshot: dict, project_id: str) -> str:
    return snapshot["worktrees_by_project"][project_id]["state"]


def test_snapshot_degrades_gracefully_for_invalid_and_non_git_paths(tmp_path):
    api = _api(tmp_path)

    stale_path = tmp_path / "moved-away"  # never created -> "path no longer valid"
    project_config.save_repository_path("AIOS", str(stale_path))

    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    project_config.save_repository_path("BUSINESS", str(not_a_repo))

    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)

    assert workspace_home_state_for(snapshot, "AIOS") == "invalid_path"
    assert workspace_home_state_for(snapshot, "BUSINESS") == "not_git_repo"
    assert workspace_home_state_for(snapshot, "LEGAL") == "unconfigured"


def test_discover_worktrees_direct_unit_cases(tmp_path, git_repo):
    assert workspace_home._discover_worktrees(None) == ("unconfigured", [])
    assert workspace_home._discover_worktrees(str(tmp_path / "missing")) == ("invalid_path", [])
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    assert workspace_home._discover_worktrees(str(not_a_repo)) == ("not_git_repo", [])
    state, worktrees = workspace_home._discover_worktrees(str(git_repo))
    assert state == "ok"
    assert len(worktrees) == 1


# --------------------------------------------------------------------------
# Populated-state scenario
# --------------------------------------------------------------------------


def test_snapshot_populated_state_end_to_end(tmp_path, git_repo, configure_project_repo, fake_claude, _isolated_artifact_dirs):
    configure_project_repo("AIOS", git_repo)
    project_config.save_repository_path("AIOS", str(git_repo))
    api = _api(tmp_path)

    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation",
        instruction="do work", confirmed=True,
    )
    api.supervisor.wait_for_run(run["id"], timeout=10)

    activity_log.log_event("run_completed", project="AIOS", run_id=run["id"], message="ok")

    reports_dir, generated_dir = _isolated_artifact_dirs
    (generated_dir / "AIOS").mkdir(parents=True, exist_ok=True)
    (generated_dir / "AIOS" / "abc123_implementation.md").write_text("# Task\n")

    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)

    aios_card = next(p for p in snapshot["projects"] if p["id"] == "AIOS")
    assert aios_card["repository_state"] == "ok"

    assert workspace_home_state_for(snapshot, "AIOS") == "ok"
    assert len(snapshot["worktrees_by_project"]["AIOS"]["worktrees"]) == 1

    recent_ids = {(r["source"], r["run_id"]) for r in snapshot["recent_runs"]}
    assert ("v2", run["id"]) in recent_ids

    assert any(a["project"] == "AIOS" for a in snapshot["artifacts"])
    assert any(a["project"] == "AIOS" for a in snapshot["recent_activity"])


def test_active_runs_only_contains_v2_prepared_queued_running(tmp_path, git_repo, configure_project_repo, fake_claude):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    configure_project_repo("AIOS", git_repo)
    project_config.save_repository_path("AIOS", str(git_repo))
    api = _api(tmp_path)

    run = api.start_run(
        project="AIOS", repository_path=str(git_repo), task_type="implementation",
        instruction="do work", confirmed=True,
    )
    try:
        snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)
        active_ids = {(r["source"], r["run_id"]) for r in snapshot["active_runs"]}
        assert ("v2", run["id"]) in active_ids
        recent_ids = {(r["source"], r["run_id"]) for r in snapshot["recent_runs"]}
        assert ("v2", run["id"]) not in recent_ids
    finally:
        api.request_cancel(run["id"], confirmed=True, grace_seconds=1)


def test_limits_are_respected(tmp_path):
    api = _api(tmp_path)
    for i in range(5):
        run = models.new_run_record(
            project="AIOS", task_id=None, agent="claude_code", task_type="implementation",
            repository_path="/tmp/x", prompt="p", timeout_seconds=60,
        )
        run["status"] = "completed"
        run["created_at"] = f"2026-01-01T00:0{i}:00"
        agent_runner.append_run(run)

    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api, recent_runs_limit=2)
    assert len(snapshot["recent_runs"]) == 2


# --------------------------------------------------------------------------
# §5.1 — sensitivity redaction
# --------------------------------------------------------------------------


def test_sanitize_identity_transform_for_non_sensitive_project(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("x")
    run = {"run_id": "r1", "source": "v2", "project": "AIOS", "prompt": "secret-ish but AIOS is fine"}
    report = {"run_id": "r1", "source": "v2", "project": "AIOS", "verdict": "APPROVED_FOR_COMMIT", "report_path": path}
    activity = {"project": "AIOS", "event_type": "run_completed", "message": "full message body"}

    for project_id in ["AIOS", "AICOS", "BUSINESS", "PERSONAL"]:
        result = workspace_home.sanitize_workspace_project_entry(
            project_id, runs=[run], reports=[report], artifacts=[path], activity=[activity]
        )
        assert result["runs"] == [run]
        assert result["reports"] == [report]
        assert result["activity"] == [activity]
        assert result["artifacts"][0]["path"] == path


@pytest.mark.parametrize("project_id", ["BANK", "LEGAL"])
def test_sanitize_strips_banned_fields_for_sensitive_project(project_id, tmp_path):
    path = tmp_path / "secret_report.md"
    path.write_text("# Secret findings\nTOP SECRET")

    run = {
        "run_id": "r1", "source": "v2", "project": project_id, "task_type": "implementation",
        "state": "COMPLETED", "created_at": "2026-01-01T00:00:00", "started_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00", "exit_code": 0, "duration_seconds": 60.0,
        "prompt": "TOP SECRET instruction", "command_json": {"argv": ["claude"]},
        "stdout": "secret output", "stderr": "", "pid": 123,
    }
    report = {
        "run_id": "r1", "source": "v2", "project": project_id,
        "verdict": "APPROVED_FOR_COMMIT", "severity_counts": {"Blocker": 0, "High": 0, "Medium": 0, "Low": 0},
        "created_at": 123.0, "report_path": path,
    }
    activity = {
        "project": project_id, "event_type": "run_completed", "ts": "2026-01-01T00:01:00",
        "run_id": "r1", "task_id": "t1", "message": "full sensitive message body", "type": "run_completed",
    }

    result = workspace_home.sanitize_workspace_project_entry(
        project_id, runs=[run], reports=[report], artifacts=[path], activity=[activity]
    )

    sanitized_run = result["runs"][0]
    for banned in ("prompt", "command_json", "stdout", "stderr", "pid"):
        assert banned not in sanitized_run
    assert sanitized_run["run_id"] == "r1"
    assert sanitized_run["source"] == "v2"
    assert sanitized_run["state"] == "COMPLETED"
    assert sanitized_run["exit_code"] == 0

    sanitized_report = result["reports"][0]
    assert "report_path" not in sanitized_report
    assert sanitized_report["verdict"] == "APPROVED_FOR_COMMIT"

    sanitized_artifact = result["artifacts"][0]
    assert "path" not in sanitized_artifact
    assert sanitized_artifact["nav_target"] == {"project": project_id, "section": "generated"}

    sanitized_activity = result["activity"][0]
    assert "message" not in sanitized_activity
    assert "type" not in sanitized_activity
    assert sanitized_activity["event_type"] == "run_completed"


@pytest.mark.parametrize("project_id", ["BANK", "LEGAL"])
@pytest.mark.parametrize("failure_reason", [None, "timeout"])
def test_sanitize_allows_failure_reason_for_sensitive_project(project_id, failure_reason, tmp_path):
    """`failure_reason` is safe to allowlist because the Supervisor (the only writer,
    `runtime/supervisor.py:_supervise`) constrains it to exactly `None` or the literal
    string `"timeout"` — never raw exception text. This pins that the field survives
    redaction with those two documented values."""
    run = {
        "run_id": "r1", "source": "v2", "project": project_id, "state": "FAILED",
        "failure_reason": failure_reason,
    }

    result = workspace_home.sanitize_workspace_project_entry(
        project_id, runs=[run], reports=[], artifacts=[], activity=[]
    )

    sanitized_run = result["runs"][0]
    assert sanitized_run["failure_reason"] == failure_reason


_BANNED_VALUES = ["TOP SECRET", "secret output", "secret-financial-data"]


def _assert_no_banned_values(node, banned_strings: list[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _assert_no_banned_values(value, banned_strings)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _assert_no_banned_values(item, banned_strings)
    elif isinstance(node, (str, Path)):
        text = str(node)
        for banned in banned_strings:
            assert banned not in text, f"banned value {banned!r} leaked into snapshot: {text!r}"


def test_snapshot_never_contains_banned_fields_for_sensitive_project(
    tmp_path, git_repo, configure_project_repo, fake_claude, _isolated_artifact_dirs
):
    """Both halves: BANK is redacted *and* a non-sensitive project is not.

    Every assertion this test makes is inside a loop over entries filtered out
    of the snapshot by project, so an empty section satisfies all of them --
    and the "non-sensitive control project, same shape, must retain
    everything" the setup announced was never once asserted, nor even created.
    A snapshot builder that dropped every BANK entry, or renamed the
    ``project`` key, or one whose redaction stripped ``prompt`` from *every*
    project, all passed. So: prove each section can be seen on both sides
    first, then check redaction against the control that says the redaction is
    aimed rather than universal.
    """
    configure_project_repo("BANK", git_repo)
    project_config.save_repository_path("BANK", str(git_repo))
    configure_project_repo("AIOS", git_repo)
    project_config.save_repository_path("AIOS", str(git_repo))
    api = _api(tmp_path)
    reports_dir, generated_dir = _isolated_artifact_dirs

    # The same four artefact kinds for the sensitive project and for the
    # control, so any asymmetry below is redaction and not fixture shape.
    for project, marker in (("BANK", "TOP SECRET"), ("AIOS", "RETAINED")):
        run = api.start_run(
            project=project, repository_path=str(git_repo), task_type="implementation",
            instruction=f"{marker} financial instruction", confirmed=True,
        )
        api.supervisor.wait_for_run(run["id"], timeout=10)
        activity_log.log_event(
            "run_completed", project=project, run_id=run["id"], message=f"{marker} details"
        )
        (reports_dir / project).mkdir(parents=True, exist_ok=True)
        (reports_dir / project / "report.md").write_text(f"{marker} report body")
        (generated_dir / project).mkdir(parents=True, exist_ok=True)
        (generated_dir / project / "task_implementation.md").write_text(f"{marker} output")

    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)

    def entries(section: str, project: str) -> list[dict]:
        return [e for e in snapshot[section] if e.get("project") == project]

    # A test that cannot see the entries cannot see their leak. Nothing below
    # means anything until every section is non-empty on both sides.
    # `active_runs` is deliberately not in this list: both runs are awaited to
    # completion, so it is legitimately empty and asserting otherwise would be
    # a false alarm.
    sections = ("recent_runs", "reports", "artifacts", "recent_activity")
    for section in sections:
        assert entries(section, "BANK"), f"no BANK entry in snapshot[{section!r}] to inspect"
        assert entries(section, "AIOS"), f"no control entry in snapshot[{section!r}] to compare against"

    bank_entries = [e for section in (*sections, "active_runs") for e in entries(section, "BANK")]
    _assert_no_banned_values(bank_entries, _BANNED_VALUES)

    # The redacted key in each section, and the control that proves the
    # redaction is aimed at the sensitive project rather than at everyone.
    for section, field in (
        ("recent_runs", "prompt"),
        ("reports", "report_path"),
        ("artifacts", "path"),
        ("recent_activity", "message"),
    ):
        for entry in entries(section, "BANK"):
            assert field not in entry, f"{field!r} survived redaction in {section}: {entry}"
        assert any(field in entry for entry in entries(section, "AIOS")), (
            f"{field!r} is absent from the non-sensitive control's {section} too -- "
            "this test cannot tell redaction from a field nothing populates"
        )


# --------------------------------------------------------------------------
# §8/F5 — merged run identity is (source, run_id), never bare run_id
# --------------------------------------------------------------------------


def test_merged_run_identity_uses_source_and_run_id(tmp_path):
    collide_id = "a" * 32  # 32-hex-char, shape-identical to models.new_id()

    v1_run = models.new_run_record(
        project="AIOS", task_id=None, agent="claude_code", task_type="implementation",
        repository_path="/tmp/x", prompt="p", timeout_seconds=60,
    )
    v1_run["id"] = collide_id
    v1_run["status"] = "completed"
    v1_run["created_at"] = "2026-01-01T00:00:00"
    agent_runner.append_run(v1_run)

    v2_run_raw = {
        "id": collide_id, "project": "AIOS", "task_type": "implementation", "state": "COMPLETED",
        "created_at": "2026-01-01T00:00:01", "started_at": None, "completed_at": None, "exit_code": 0,
        "task_id": None,
    }

    tagged_v1 = workspace_home._tag_v1_run(v1_run)
    tagged_v2 = workspace_home._tag_v2_run(v2_run_raw)

    assert tagged_v1["run_id"] == collide_id
    assert tagged_v2["run_id"] == collide_id
    assert tagged_v1["source"] == "v1.2"
    assert tagged_v2["source"] == "v2"

    identities = {(tagged_v1["source"], tagged_v1["run_id"]), (tagged_v2["source"], tagged_v2["run_id"])}
    assert len(identities) == 2, "colliding run_id values across sources must remain distinct entries"
