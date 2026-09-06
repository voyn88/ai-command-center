"""The DEFER_TO_USER evidence-and-sweep CLI surface: parsing and the pure
seams, no database.

``backlog_resume_deferred`` (0014) itself is proved against real PostgreSQL
in ``test_backlog_planner.py``; what this file pins is the operator contract
built on top of it -- ``_defer_evidence`` reading the same two facts the gate
grants on, ``_defer_sweep`` collecting what the gate said per task, and the
argparse contract the commands expose.
"""

from __future__ import annotations

import pytest

# The CLI module reaches the pool adapter at import, and the adapter needs the
# vendored `aios_db` wheel -- present in CI, optional in a bare local checkout.
pytest.importorskip("aios_db")

from command_center.db.cli import (  # noqa: E402
    _defer_evidence,
    _defer_sweep,
    build_parser,
)


def _event(event: str, outcome: str, reason: str | None = None, detail: dict | None = None) -> dict:
    return {"event": event, "outcome": outcome, "reason": reason, "detail": detail or {}}


def test_defer_evidence_reads_the_latest_technical_park_reason() -> None:
    # Newest first, as BacklogStore.list_events returns it: a later,
    # unrelated event must not shadow the park reason a human wants to see.
    events = [
        _event("dispatch", "granted", "eligible"),
        _event(
            "return_to_pool",
            "granted",
            "cascade_exhausted: writer lease unavailable: VOYN_LEASE_REFUSED",
            {"target": "DEFER_TO_USER", "prior_returns": 2, "technical": True},
        ),
        _event("dispatch", "granted", "eligible"),
    ]
    park_reason, resumes = _defer_evidence(events)
    assert park_reason == (
        "cascade_exhausted: writer lease unavailable: VOYN_LEASE_REFUSED"
    )
    assert resumes == 0


def test_defer_evidence_counts_only_granted_resumes() -> None:
    events = [
        _event("resume_deferred", "granted", "cascade_exhausted: x"),
        _event("resume_deferred", "rejected", "resume_budget_exhausted"),
        _event("resume_deferred", "granted", "cascade_exhausted: x"),
        _event(
            "return_to_pool",
            "granted",
            "cascade_exhausted: x",
            {"target": "DEFER_TO_USER"},
        ),
    ]
    park_reason, resumes = _defer_evidence(events)
    assert park_reason == "cascade_exhausted: x"
    assert resumes == 2  # the rejected attempt does not count


def test_defer_evidence_ignores_a_return_to_pool_that_targeted_open() -> None:
    # A technical return_to_pool that landed the task back in OPEN (0012's
    # allow-listed reasons) is not a park -- reading it as one would show a
    # reason for a task that was never actually deferred by it.
    events = [
        _event("return_to_pool", "granted", "cascade_exhausted: no_pr_published",
               {"target": "OPEN"}),
    ]
    assert _defer_evidence(events) == (None, 0)


def test_defer_evidence_on_no_history_reports_nothing_recorded() -> None:
    assert _defer_evidence([]) == (None, 0)


def test_defer_sweep_reports_one_outcome_per_task_in_order() -> None:
    class _FakeStore:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def resume_deferred(self, task_id: str):
            self.calls.append(task_id)
            if task_id == "VOYN-W0-OK":
                return True, "cascade_exhausted: x", 4
            return False, "resume_budget_exhausted", 3

    store = _FakeStore()
    results = _defer_sweep(store, ["VOYN-W0-OK", "VOYN-W0-STUCK"])

    assert store.calls == ["VOYN-W0-OK", "VOYN-W0-STUCK"]
    assert results == [
        ("VOYN-W0-OK", True, "cascade_exhausted: x"),
        ("VOYN-W0-STUCK", False, "resume_budget_exhausted"),
    ]


def test_defer_sweep_of_no_tasks_makes_no_calls() -> None:
    class _FakeStore:
        def resume_deferred(self, task_id: str):  # pragma: no cover - must not run
            raise AssertionError("resume_deferred called with nothing to sweep")

    assert _defer_sweep(_FakeStore(), []) == []


def test_backlog_defer_status_defaults_to_fifty_rows() -> None:
    args = build_parser().parse_args(["backlog-defer-status"])
    assert args.command == "backlog-defer-status"
    assert args.limit == 50
    scoped = build_parser().parse_args(["backlog-defer-status", "--limit", "5"])
    assert scoped.limit == 5


def test_backlog_defer_sweep_requires_exactly_one_target() -> None:
    one = build_parser().parse_args(["backlog-defer-sweep", "--task-id", "VOYN-W0-X"])
    assert one.task_id == "VOYN-W0-X" and one.all is False

    every = build_parser().parse_args(["backlog-defer-sweep", "--all"])
    assert every.all is True and every.task_id is None

    with pytest.raises(SystemExit):
        build_parser().parse_args(["backlog-defer-sweep"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["backlog-defer-sweep", "--task-id", "VOYN-W0-X", "--all"]
        )
