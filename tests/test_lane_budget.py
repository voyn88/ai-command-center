from pathlib import Path

from command_center import lane_budget, pipeline_settings
from command_center.pipeline_settings import PipelineSettings


def test_update_settings_accepts_daily_spend(tmp_path: Path) -> None:
    pipeline_settings.save_settings(tmp_path, PipelineSettings(enabled=True))
    updated = pipeline_settings.update_settings(
        tmp_path, actor="test", max_daily_spend_usd=25.0
    )
    assert updated.max_daily_spend_usd == 25.0
    assert pipeline_settings.load_settings(tmp_path).max_daily_spend_usd == 25.0


def test_pause_blocks_dispatch(tmp_path: Path) -> None:
    lane_budget.pause(tmp_path, reason="red_smoke", actor="test", sha="abc123")
    decision = lane_budget.check_dispatch(tmp_path, "ai-command-center", "pr")
    assert decision.allowed is False
    assert decision.reason == lane_budget.REASON_PAUSED
    assert decision.detail["pause_sha"] == "abc123"
    lane_budget.resume(tmp_path, actor="test")
    decision = lane_budget.check_dispatch(tmp_path, "ai-command-center", "pr")
    assert decision.allowed is True


def test_per_repo_pr_budget(tmp_path: Path) -> None:
    for _ in range(lane_budget.DEFAULT_MAX_PR_PER_WINDOW):
        allowed = lane_budget.check_dispatch(tmp_path, "voyn-logistics-crm", "pr")
        assert allowed.allowed is True
        lane_budget.record_event(tmp_path, "voyn-logistics-crm", "pr")
    blocked = lane_budget.check_dispatch(tmp_path, "voyn-logistics-crm", "pr")
    assert blocked.allowed is False
    assert blocked.reason == lane_budget.REASON_BUDGET_PR
    other = lane_budget.check_dispatch(tmp_path, "ai-command-center", "pr")
    assert other.allowed is True


def test_consecutive_failures_stop_lane(tmp_path: Path) -> None:
    for _ in range(lane_budget.DEFAULT_MAX_CONSECUTIVE_FAILURES):
        lane_budget.record_event(tmp_path, "aios", "rerun", failed=True)
    decision = lane_budget.check_dispatch(tmp_path, "aios", "rerun")
    assert decision.allowed is False
    assert decision.reason == lane_budget.REASON_BUDGET_FAILURES


def test_red_smoke_helper_sets_sha(tmp_path: Path) -> None:
    state = lane_budget.pause_for_red_smoke(tmp_path, sha="deadbeef")
    assert state["paused"] is True
    assert state["pause_sha"] == "deadbeef"
