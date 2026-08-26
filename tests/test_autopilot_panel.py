"""Streamlit `AppTest` coverage for the Desktop Autopilot surface
(AICC-DESKTOP-013/014/015/016/017).

The properties worth testing in the UI layer are the safety-relevant ones: the
controls are off by default, flipping one *persists* to disk (not to session
state), the rendered wave actually shows a decision's agent/workspace/rationale,
and a decision that did not launch shows its machine-readable code together with
the operator remediation. The planning and launching behaviour itself is covered
in `test_task_pipeline*.py`; nothing here launches a process.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import pipeline_settings, task_pipeline
from command_center.pipeline_settings import PipelineSettings
from command_center.runtime import scheduler
from command_center.ui import autopilot_panel

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
ROOT = Path(APP_PATH).parent


def _decision(**overrides):
    base = dict(
        entry_id="q1",
        task_id="t1",
        action=scheduler.ACTION_ASSIGN,
        reason_code=scheduler.REASON_ASSIGNED,
        explanation="assigned to agent 'claude_code' as attempt 1",
        title="Первая задача",
        project="AIOS",
        priority="High",
        workspace="/tmp/wt/alpha",
        agent_id="claude_code",
        executor_id="claude_code",
        attempt=1,
    )
    base.update(overrides)
    return task_pipeline.EntryDecision(**base)


def _tick_result(**overrides):
    base = dict(
        started_at="2026-07-24T10:00:00",
        finished_at="2026-07-24T10:00:01",
        settings=PipelineSettings(enabled=True),
        ran=True,
        status=task_pipeline.TICK_RAN,
    )
    base.update(overrides)
    return task_pipeline.PipelineTickResult(**base)


def _at(**session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.session_state["nav_page"] = "execution_center"
    for key, value in session_state.items():
        at.session_state[key] = value
    at.run()
    return at


def _captions(at) -> str:
    return "\n".join(c.value for c in at.caption)


# --------------------------------------------------------------------------
# AICC-DESKTOP-015 — explicit persistent controls, default off
# --------------------------------------------------------------------------


def test_autopilot_controls_are_off_by_default(isolated_data_dir):
    at = _at()
    assert at.toggle(key="autopilot_enabled").value is False
    assert at.toggle(key="autopilot_auto_launch").value is False
    assert at.toggle(key="autopilot_auto_merge").value is False


def test_enabling_autopilot_persists_to_disk_not_session_state(isolated_data_dir):
    at = _at()
    at.toggle(key="autopilot_enabled").set_value(True).run()
    assert pipeline_settings.load_settings(ROOT).enabled is True


def test_auto_launch_is_disabled_until_the_master_switch_is_on(isolated_data_dir):
    at = _at()
    assert at.toggle(key="autopilot_auto_launch").disabled is True
    at.toggle(key="autopilot_enabled").set_value(True).run()
    assert at.toggle(key="autopilot_auto_launch").disabled is False


def test_active_auto_launch_is_announced_as_a_warning(isolated_data_dir):
    pipeline_settings.save_settings(ROOT, PipelineSettings(enabled=True, auto_launch=True))
    at = _at()
    assert any("Автозапуск активен" in warning.value for warning in at.warning)


def test_persisted_settings_are_reflected_on_load(isolated_data_dir):
    pipeline_settings.save_settings(
        ROOT, PipelineSettings(enabled=True, auto_merge_after_checks=True, max_global_concurrency=5)
    )
    at = _at()
    assert at.toggle(key="autopilot_enabled").value is True
    assert at.toggle(key="autopilot_auto_merge").value is True
    assert at.number_input(key="autopilot_max_global").value == 5


def test_external_settings_change_is_not_reverted_by_a_rerender(isolated_data_dir):
    """Regression: the panel used to echo its own stale toggle state back to disk
    on every rerun, silently reverting a change made anywhere else (the pipeline,
    another browser tab, a script). Since a Streamlit widget ignores its `value=`
    after first render and reports its own session_state, the panel would keep
    re-writing the old value. It must instead adopt the on-disk truth: reflect the
    external change *and* leave it on disk."""
    pipeline_settings.save_settings(
        ROOT, PipelineSettings(enabled=True, require_independent_review=True)
    )
    at = _at()
    assert at.toggle(key="autopilot_require_review").value is True

    # Something outside this tab flips the gate off (the pipeline / a script).
    pipeline_settings.update_settings(ROOT, actor="pipeline", require_independent_review=False)

    at.run()  # a plain refresh of the same session must not clobber the change
    assert pipeline_settings.load_settings(ROOT).require_independent_review is False
    assert at.toggle(key="autopilot_require_review").value is False


# --------------------------------------------------------------------------
# AICC-DESKTOP-013 — the wave: priority, agent, workspace, rationale
# --------------------------------------------------------------------------


def test_wave_shows_priority_agent_workspace_and_rationale(isolated_data_dir):
    result = _tick_result(decisions=(_decision(),))
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    body = _captions(at)
    assert "приоритет High" in body
    assert "claude_code" in body
    assert "wt/alpha" in body
    assert "assigned to agent" in body


def test_launched_decision_reports_its_run(isolated_data_dir):
    result = _tick_result(
        decisions=(_decision(launched=True, run_id="abcdef1234567890"),),
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    assert "abcdef12" in _captions(at)


# --------------------------------------------------------------------------
# AICC-DESKTOP-014 — reason codes and remediation for anything not launched
# --------------------------------------------------------------------------


def test_deferred_decision_shows_reason_code_and_remediation(isolated_data_dir):
    result = _tick_result(
        decisions=(
            _decision(
                action=scheduler.ACTION_DEFER,
                reason_code=scheduler.REASON_GLOBAL_AT_CAPACITY,
                explanation="global concurrency limit reached (2/2 running)",
            ),
        )
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    body = _captions(at)
    assert scheduler.REASON_GLOBAL_AT_CAPACITY in body
    assert task_pipeline.REMEDIATION_BY_REASON[scheduler.REASON_GLOBAL_AT_CAPACITY] in body


def test_refused_launch_shows_the_launch_reason_code_over_the_scheduling_one(isolated_data_dir):
    result = _tick_result(
        decisions=(
            _decision(
                launched=False,
                launch_reason_code="workspace_verification_failed",
                launch_message="workspace не прошёл проверку изоляции",
            ),
        )
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    body = _captions(at)
    assert "workspace_verification_failed" in body
    assert "проверьте изоляцию worktree" in body


def test_needs_confirmation_skip_lists_its_warnings(isolated_data_dir):
    result = _tick_result(
        decisions=(
            _decision(
                action=task_pipeline.ACTION_SKIPPED,
                reason_code=task_pipeline.REASON_NEEDS_CONFIRMATION,
                explanation="рабочее дерево не чистое",
                warnings=("рабочее дерево не чистое",),
                agent_id=None,
                attempt=None,
            ),
        )
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    body = _captions(at)
    assert task_pipeline.REASON_NEEDS_CONFIRMATION in body
    assert "запустите вручную" in body


# --------------------------------------------------------------------------
# AICC-DESKTOP-017 — durable outcome: launches, skips, completion, merge
# --------------------------------------------------------------------------


def test_completed_tasks_are_reported_as_a_verified_merge(isolated_data_dir):
    result = _tick_result(completed_task_ids=("t1", "t2"))
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    assert any("Merge подтверждён" in success.value for success in at.success)


def test_auto_launch_disabled_is_explained_rather_than_looking_broken(isolated_data_dir):
    result = _tick_result(
        decisions=(_decision(),), launch_status=task_pipeline.LAUNCH_DISABLED
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    assert "Автозапуск выключен" in _captions(at)


def test_spend_unknown_is_explained_and_not_shown_as_budget_exhausted(isolated_data_dir):
    """VOYN-W0-AICC-REPORT-319-REM: an operator must see that spend could not
    be computed, distinct from every other reason nothing launched."""
    result = _tick_result(
        decisions=(_decision(),), launch_status=task_pipeline.LAUNCH_SPEND_UNKNOWN
    )
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    warnings = "\n".join(w.value for w in at.warning)
    assert "не удалось достоверно посчитать" in warnings


def test_busy_tick_is_reported_as_information_not_failure(isolated_data_dir):
    result = _tick_result(ran=False, status=task_pipeline.TICK_BUSY)
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    assert any("занят" in info.value for info in at.info)


def test_tick_errors_are_surfaced(isolated_data_dir):
    result = _tick_result(errors=("advance_pre: gh unavailable",))
    at = _at(**{autopilot_panel.TICK_RESULT_KEY: result})
    assert "gh unavailable" in _captions(at)


def test_failed_tick_is_surfaced_as_an_error(isolated_data_dir):
    at = _at(**{autopilot_panel.TICK_ERROR_KEY: "boom"})
    assert any("boom" in error.value for error in at.error)


# --------------------------------------------------------------------------
# AICC-DESKTOP-016 — the tick runs from the existing refresh path only
# --------------------------------------------------------------------------


def test_disabled_autopilot_leaves_no_tick_result(isolated_data_dir):
    """With autopilot off the refresh path still calls `tick`, but the tick does
    nothing and nothing is stashed — so the panel never claims a wave was
    planned when none was."""
    at = _at()
    assert autopilot_panel.TICK_RESULT_KEY not in at.session_state
    assert "волна появится после первого обновления" in _captions(at)
