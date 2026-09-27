from pathlib import Path

from command_center import autonomy_loop, lane_budget, pipeline_settings


def test_paused_loop_does_not_arm_or_tick(tmp_path: Path) -> None:
    lane_budget.pause(tmp_path, reason="operator stop", actor="test")
    report = autonomy_loop.run_once(tmp_path, repo="ai-command-center")
    assert report["status"] == "skipped"
    assert report["guard"]["reason"] == lane_budget.REASON_PAUSED
    settings = pipeline_settings.load_settings(tmp_path)
    assert settings.enabled is False


def test_open_loop_arms_merge(tmp_path: Path) -> None:
    report = autonomy_loop.run_once(tmp_path, repo="ai-command-center")
    assert report["status"] == "ran"
    assert report["settings"]["auto_merge_active"] is True
    settings = pipeline_settings.load_settings(tmp_path)
    assert settings.enabled is True
    assert settings.auto_launch is True
    assert settings.auto_merge_after_checks is True
