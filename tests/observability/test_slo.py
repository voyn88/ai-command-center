"""Programmatic SLOs over the invariants this session watched break live."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from command_center.observability.slo import (
    DEFAULT_REVIEW_SLA_SECONDS,
    SloViolation,
    check_duplicate_mutating_attempts,
    check_missing_heartbeat,
    check_review_cycle_sla,
    evaluate_slos,
    fire_alerts,
)

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


# -- no two mutating attempts on one worktree --------------------------------


def test_two_claimed_attempts_for_the_same_task_is_a_violation() -> None:
    attempts = [
        {"task_id": "VOYN-1", "work_item_id": "wi_a", "attempt_id": "at_a", "state": "active"},
        {"task_id": "VOYN-1", "work_item_id": "wi_b", "attempt_id": "at_b", "state": "active"},
    ]
    violations = check_duplicate_mutating_attempts(attempts)
    assert len(violations) == 1
    assert violations[0].slo == "no_duplicate_mutating_attempts_per_worktree"
    assert violations[0].task_id == "VOYN-1"
    assert violations[0].context["attempt_ids"] == ["at_a", "at_b"]


def test_one_claimed_attempt_per_task_is_not_a_violation() -> None:
    attempts = [
        {"task_id": "VOYN-1", "work_item_id": "wi_a", "attempt_id": "at_a", "state": "active"},
        {"task_id": "VOYN-2", "work_item_id": "wi_b", "attempt_id": "at_b", "state": "active"},
    ]
    assert check_duplicate_mutating_attempts(attempts) == []


def test_a_succeeded_second_attempt_does_not_collide_with_a_claimed_one() -> None:
    attempts = [
        {"task_id": "VOYN-1", "work_item_id": "wi_a", "attempt_id": "at_a", "state": "active"},
        {"task_id": "VOYN-1", "work_item_id": "wi_a", "attempt_id": "at_prior", "state": "succeeded"},
    ]
    assert check_duplicate_mutating_attempts(attempts) == []


def test_a_non_backlog_work_item_with_no_task_id_is_never_flagged() -> None:
    attempts = [
        {"task_id": None, "work_item_id": "wi_a", "attempt_id": "at_a", "state": "active"},
        {"task_id": None, "work_item_id": "wi_b", "attempt_id": "at_b", "state": "active"},
    ]
    assert check_duplicate_mutating_attempts(attempts) == []


# -- no active attempt without a heartbeat -----------------------------------


def test_a_claimed_attempt_with_a_stale_heartbeat_is_a_violation() -> None:
    attempts = [
        {
            "task_id": "VOYN-1",
            "work_item_id": "wi_a",
            "attempt_id": "at_a",
            "state": "active",
            "visibility_seconds": 300,  # beat interval 100s, grace 300s
            "heartbeat_at": NOW - timedelta(seconds=301),
            "created_at": NOW - timedelta(seconds=600),
        }
    ]
    violations = check_missing_heartbeat(attempts, now=NOW)
    assert len(violations) == 1
    assert violations[0].slo == "no_active_attempt_without_heartbeat"
    assert violations[0].task_id == "VOYN-1"


def test_a_claimed_attempt_beating_on_cadence_is_healthy() -> None:
    attempts = [
        {
            "task_id": "VOYN-1",
            "work_item_id": "wi_a",
            "attempt_id": "at_a",
            "state": "active",
            "visibility_seconds": 300,
            "heartbeat_at": NOW - timedelta(seconds=50),
            "created_at": NOW - timedelta(seconds=600),
        }
    ]
    assert check_missing_heartbeat(attempts, now=NOW) == []


def test_an_attempt_never_heartbeat_since_claim_is_a_violation_past_grace() -> None:
    attempts = [
        {
            "task_id": "VOYN-1",
            "work_item_id": "wi_a",
            "attempt_id": "at_a",
            "state": "active",
            "visibility_seconds": 300,
            "heartbeat_at": None,
            "created_at": NOW - timedelta(seconds=600),
        }
    ]
    violations = check_missing_heartbeat(attempts, now=NOW)
    assert len(violations) == 1


def test_a_non_claimed_attempt_is_never_flagged() -> None:
    attempts = [
        {
            "task_id": "VOYN-1",
            "work_item_id": "wi_a",
            "attempt_id": "at_a",
            "state": "succeeded",
            "visibility_seconds": 300,
            "heartbeat_at": None,
            "created_at": NOW - timedelta(hours=5),
        }
    ]
    assert check_missing_heartbeat(attempts, now=NOW) == []


# -- no review cycle without a terminal outcome inside its SLA --------------


def test_a_review_cycle_past_its_sla_is_a_violation() -> None:
    tasks = [
        {
            "task_id": "VOYN-1",
            "status": "READY_TO_REVIEW",
            "updated_at": NOW - timedelta(seconds=DEFAULT_REVIEW_SLA_SECONDS + 1),
        }
    ]
    violations = check_review_cycle_sla(tasks, now=NOW)
    assert len(violations) == 1
    assert violations[0].slo == "no_review_cycle_beyond_sla"
    assert violations[0].task_id == "VOYN-1"


def test_a_review_cycle_inside_its_sla_is_not_flagged() -> None:
    tasks = [
        {
            "task_id": "VOYN-1",
            "status": "READY_TO_REVIEW",
            "updated_at": NOW - timedelta(minutes=5),
        }
    ]
    assert check_review_cycle_sla(tasks, now=NOW) == []


def test_a_task_not_in_review_is_never_flagged_however_old() -> None:
    tasks = [
        {"task_id": "VOYN-1", "status": "DONE", "updated_at": NOW - timedelta(days=30)}
    ]
    assert check_review_cycle_sla(tasks, now=NOW) == []


def test_review_sla_respects_an_iso_string_timestamp_not_only_a_datetime() -> None:
    tasks = [
        {
            "task_id": "VOYN-1",
            "status": "READY_TO_REVIEW",
            "updated_at": "2026-09-02T10:00:00+00:00",
        }
    ]
    violations = check_review_cycle_sla(tasks, now=NOW, sla_seconds=3600)
    assert len(violations) == 1


# -- aggregation and alerting -------------------------------------------------


def test_evaluate_slos_aggregates_all_three_checks() -> None:
    attempts = [
        {"task_id": "VOYN-1", "work_item_id": "wi_a", "attempt_id": "at_a", "state": "active"},
        {"task_id": "VOYN-1", "work_item_id": "wi_b", "attempt_id": "at_b", "state": "active"},
    ]
    tasks = [
        {
            "task_id": "VOYN-2",
            "status": "READY_TO_REVIEW",
            "updated_at": NOW - timedelta(seconds=DEFAULT_REVIEW_SLA_SECONDS + 1),
        }
    ]
    violations = evaluate_slos(attempts=attempts, tasks=tasks, now=NOW)
    slos = {v.slo for v in violations}
    assert slos == {
        "no_duplicate_mutating_attempts_per_worktree",
        "no_review_cycle_beyond_sla",
    }


def test_evaluate_slos_with_no_data_fires_nothing() -> None:
    assert evaluate_slos(now=NOW) == []


def test_fire_alerts_logs_one_error_line_per_violation(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="command_center.alerts")
    violations = [
        SloViolation(
            slo="no_review_cycle_beyond_sla",
            task_id="VOYN-1",
            detail="stuck",
            context={"age_seconds": 99999},
        )
    ]
    fire_alerts(violations)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    payload = json.loads(caplog.records[0].message)
    assert payload["alert"] == "no_review_cycle_beyond_sla"
    assert payload["task_id"] == "VOYN-1"
    assert payload["age_seconds"] == 99999
