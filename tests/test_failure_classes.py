"""Unit tests for `command_center.orchestrator.failure_classes`.

Acceptance 4 of VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR
is a measurement: the share of `task_status_failed` caused by missing
authority falls to zero, and the rest are classified separately. These tests
lock the classification that measurement is read from — including the
retroactive attribution that makes the "before" number knowable at all.
"""

from __future__ import annotations

from command_center.orchestrator import failure_classes as fc
from command_center.orchestrator.failure_classes import FailureClass


# --------------------------------------------------------------------------
# The reason alone, wherever it ends up wrapped.
# --------------------------------------------------------------------------


def test_an_authority_reason_classifies_bare_and_wrapped():
    """The planner's preflight writes it bare; the worker gate's refusal
    reaches the store wrapped by ingest in `cascade_exhausted:`. One cause,
    one class, regardless of which path produced it."""
    for reason in (
        "requires_privileged_authority: root",
        "cascade_exhausted: requires_privileged_authority: postgres_role:postgres,root",
        # The shape the live queue actually produces: `queue_fail` wraps a
        # non-retryable refusal, then ingest wraps that.
        "cascade_exhausted: non_retryable: requires_privileged_authority: root",
        "requires_privileged_authority: no_single_executor_grants: root",
    ):
        assert fc.classify_reason(reason) == FailureClass.AUTHORITY, reason


def test_a_spent_account_window_is_not_a_task_defect():
    """The 2026-08-23 finding: 142 of 167 parked `task_status_failed` were
    session limits. Time or another account fixes those; nothing about the
    task does."""
    assert fc.classify_reason("cascade_exhausted: You've hit your session limit") == (
        FailureClass.QUOTA
    )


def test_infrastructure_and_publish_keep_their_own_names():
    assert fc.classify_reason(
        "cascade_exhausted: executor infrastructure failure"
    ) == FailureClass.INFRASTRUCTURE
    assert fc.classify_reason("cascade_exhausted: no_pr_published") == FailureClass.PUBLISH
    assert fc.classify_reason("cascade_exhausted: publish_refused") == FailureClass.PUBLISH


def test_task_status_failed_alone_is_a_symptom_not_a_cause():
    """The undifferentiated bucket: the reason says the agent did not report
    success, which is the absence of a cause. The task's own text decides."""
    assert fc.classify_reason("cascade_exhausted: task_status_failed") is None


def test_an_unrecognized_reason_stays_visibly_unknown():
    assert fc.classify_reason("something nobody has seen") == FailureClass.UNKNOWN
    assert fc.classify_reason(None) == FailureClass.UNKNOWN


# --------------------------------------------------------------------------
# Splitting `task_status_failed` — the acceptance measurement.
# --------------------------------------------------------------------------


def test_the_dispatched_contract_attributes_the_failure():
    failure_class, evidence = fc.classify_park(
        "cascade_exhausted: task_status_failed",
        body="anything",
        required_authority=["root"],
    )
    assert failure_class == FailureClass.AUTHORITY
    assert "payload.required_authority=root" in evidence


def test_a_task_that_orders_a_privileged_command_is_attributed_retroactively():
    """Every park in the measured 2026-08-30 window predates the payload
    contract, so the "before" number is only knowable from the task text."""
    failure_class, evidence = fc.classify_park(
        "cascade_exhausted: task_status_failed",
        title="control plane resilience",
        body="Verify resilience: run `sudo -u postgres /usr/bin/psql -c 'select 1'`.",
    )
    assert failure_class == FailureClass.AUTHORITY
    assert "postgres_role:postgres" in evidence and "root" in evidence


def test_a_task_that_only_quotes_the_command_is_attributed_as_suspected():
    """It never should have been parked pre-dispatch (that is the false
    positive this task's second commit removed), but once a run DID fail, the
    agent read the same text and tried the same command — so the honest
    attribution of that failure is still authority, marked suspected."""
    failure_class, evidence = fc.classify_park(
        "cascade_exhausted: task_status_failed",
        body="The agent tried to run `sudo /usr/bin/true` and was refused.",
    )
    assert failure_class == FailureClass.AUTHORITY
    assert "suspected" in evidence


def test_an_ordinary_failure_is_a_task_defect_not_an_authority_excuse():
    failure_class, evidence = fc.classify_park(
        "cascade_exhausted: task_status_failed", body="Fix the parser and add tests."
    )
    assert failure_class == FailureClass.TASK_DEFECT
    assert "task_status_failed" in evidence


def test_result_text_rescues_a_quota_failure_the_reason_hid():
    failure_class, evidence = fc.classify_park(
        "cascade_exhausted: task_status_failed",
        body="Fix the parser.",
        result_text="You've hit your session limit, resets at 4pm",
    )
    assert failure_class == FailureClass.QUOTA
    assert "account window" in evidence


# --------------------------------------------------------------------------
# The summary the audit prints.
# --------------------------------------------------------------------------


def _window():
    return [
        {
            "task_id": "VOYN-W0-A",
            "reason": "cascade_exhausted: task_status_failed",
            "body": "Verify: run `sudo /usr/bin/true`.",
        },
        {
            "task_id": "VOYN-W0-B",
            "reason": "cascade_exhausted: task_status_failed",
            "body": "Fix the parser.",
        },
        {"task_id": "VOYN-W0-C", "reason": "requires_privileged_authority: root"},
        {"task_id": "VOYN-W0-D", "reason": "cascade_exhausted: no_pr_published"},
    ]


def test_summarize_counts_every_class_and_the_acceptance_numbers():
    counts, rows = fc.summarize(_window())
    assert counts.total == 4
    assert counts.by_class == {
        FailureClass.AUTHORITY: 2,
        FailureClass.TASK_DEFECT: 1,
        FailureClass.PUBLISH: 1,
    }
    # The bar: of the `task_status_failed` parks, how many were authority.
    assert counts.task_status_failed == 2
    assert counts.task_status_failed_authority == 1
    assert counts.authority_share == 0.5
    assert [row[0] for row in rows] == ["VOYN-W0-A", "VOYN-W0-B", "VOYN-W0-C", "VOYN-W0-D"]


def test_a_preflight_park_is_not_counted_against_the_task_status_failed_bar():
    """The whole point of the fix: an authority-blocked task is parked with
    its own reason and never becomes a `task_status_failed` at all. It must
    lower the bar's numerator by leaving the denominator, not by hiding in
    it."""
    counts, _rows = fc.summarize(
        [{"task_id": "VOYN-W0-C", "reason": "requires_privileged_authority: root"}]
    )
    assert counts.by_class == {FailureClass.AUTHORITY: 1}
    assert counts.task_status_failed == 0
    assert counts.authority_share == 0.0


def test_an_empty_window_reports_zero_without_claiming_success():
    counts, rows = fc.summarize([])
    assert (counts.total, counts.task_status_failed, counts.authority_share) == (0, 0, 0.0)
    assert rows == []


def test_the_fixed_world_measures_zero():
    """After the fix, the authority population is parked before dispatch, so
    what remains under `task_status_failed` is genuinely something else."""
    counts, _rows = fc.summarize(
        [
            {"task_id": "A", "reason": "requires_privileged_authority: root"},
            {
                "task_id": "B",
                "reason": "cascade_exhausted: task_status_failed",
                "body": "Fix the parser.",
            },
            {
                "task_id": "C",
                "reason": "cascade_exhausted: task_status_failed",
                "body": "Refactor the store.",
                "result_text": "You've hit your session limit",
            },
        ]
    )
    assert counts.task_status_failed == 2
    assert counts.task_status_failed_authority == 0
    assert counts.authority_share == 0.0
    assert counts.by_class[FailureClass.TASK_DEFECT] == 1
    assert counts.by_class[FailureClass.QUOTA] == 1


def test_the_authority_token_is_anchored_exactly_like_the_sql_predicate():
    """`backlog_reason_requires_authority` (0018) decides whether a park is
    terminal; this module decides whether it is COUNTED as authority. If the
    two anchors drift, the audited number and the parking decision measure
    different populations."""
    assert fc.classify_reason("requires_privileged_authority: root") == (
        FailureClass.AUTHORITY
    )
    assert fc.classify_reason(
        "cascade_exhausted: requires_privileged_authority: root"
    ) == FailureClass.AUTHORITY
    # A word-continuation is not the token — matching the SQL `[^a-z_]` guard.
    assert fc.classify_reason("xrequires_privileged_authority: root") != (
        FailureClass.AUTHORITY
    )
