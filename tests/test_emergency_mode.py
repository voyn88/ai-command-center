"""Unit tests for `command_center.emergency_mode` — the SRE Emergency
Conservative Mode fail-safe (VOYN-MIN-EMO).

Acceptance: every critical (high-risk/write-capable) scenario has a safe,
read-only-compatible fallback registered — `test_every_write_task_type_has_a_
fallback` locks that as a regression, not a hope.
"""

from __future__ import annotations

import pytest

from command_center import capabilities as c
from command_center import emergency_mode as e


# --------------------------------------------------------------------------
# Acceptance: all critical scenarios have a safe fallback.
# --------------------------------------------------------------------------


def test_every_write_task_type_has_a_fallback():
    assert c.WRITE_TASK_TYPES <= e.CRITICAL_FALLBACKS.keys()


@pytest.mark.parametrize("task_type", sorted(c.WRITE_TASK_TYPES))
def test_fallback_text_is_non_empty_for_every_critical_scenario(task_type):
    fallback = e.fallback_for(task_type)
    assert isinstance(fallback, str)
    assert fallback.strip()


def test_unknown_task_type_still_gets_a_fallback():
    # Fail-safe: never None, never a crash, for any task type whatsoever.
    assert e.fallback_for("some_future_task_type") == e.DEFAULT_FALLBACK


# --------------------------------------------------------------------------
# is_active — explicit env mapping, no monkeypatching os.environ.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On"])
def test_is_active_true_for_truthy_values(value):
    assert e.is_active({e.EMERGENCY_MODE_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_is_active_false_for_falsy_or_blank_values(value):
    assert e.is_active({e.EMERGENCY_MODE_ENV: value}) is False


def test_is_active_false_when_unset():
    assert e.is_active({}) is False


# --------------------------------------------------------------------------
# decide() — inactive is a pure pass-through.
# --------------------------------------------------------------------------


def test_inactive_is_pass_through_to_capabilities_decide():
    decision = e.decide("implementation", "implement the feature", None, active=False)
    assert decision.selected_profile == c.PROFILE_WORKSPACE_WRITE
    assert decision.ok


# --------------------------------------------------------------------------
# decide() — active forces read-only regardless of override.
# --------------------------------------------------------------------------


def test_active_forces_read_only_profile_even_with_write_override():
    decision = e.decide("implementation", "investigate the failing module", "workspace_write", active=True)
    assert decision.selected_profile == c.PROFILE_READ_ONLY


def test_active_with_benign_prompt_still_succeeds_read_only():
    # No write-intent phrases -> read-only can still explore/report; this is
    # the "preparation of alternatives" path, not a block.
    decision = e.decide("implementation", "investigate the failing module and summarize", None, active=True)
    assert decision.selected_profile == c.PROFILE_READ_ONLY
    assert decision.ok


@pytest.mark.parametrize("task_type", sorted(c.WRITE_TASK_TYPES))
def test_active_with_write_prompt_blocks_and_names_the_fallback(task_type):
    decision = e.decide(task_type, "edit the files and commit the fix", None, active=True)
    assert decision.selected_profile == c.PROFILE_READ_ONLY
    assert not decision.ok
    assert "Emergency Conservative Mode is active" in decision.reason
    assert e.fallback_for(task_type) in decision.reason


def test_active_read_only_task_type_is_unaffected():
    decision = e.decide("review", "summarize the findings", None, active=True)
    assert decision.selected_profile == c.PROFILE_READ_ONLY
    assert decision.ok


def test_default_active_reads_environment(monkeypatch):
    monkeypatch.setenv(e.EMERGENCY_MODE_ENV, "1")
    decision = e.decide("implementation", "implement the feature", None)
    assert decision.selected_profile == c.PROFILE_READ_ONLY


def test_default_inactive_when_env_unset(monkeypatch):
    monkeypatch.delenv(e.EMERGENCY_MODE_ENV, raising=False)
    decision = e.decide("implementation", "implement the feature", None)
    assert decision.selected_profile == c.PROFILE_WORKSPACE_WRITE
