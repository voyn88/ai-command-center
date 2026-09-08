"""The pre-dispatch reuse gate, without a live database or a real git repo.

Live case 2026-09-06: a REM (remediation) task's branch (PR 636)
re-implemented a function its parent task had already delivered via a merged
PR (624) -- the two implementations collided and broke CI once both landed.
`Planner._reuse_anchor` + the injectable `reuse_lookup` (VOYN-W0-AICC-
DISPATCH-REUSE-GATE) are what stop that: before dispatching a REM/RETRY
candidate, check whether the source task's acceptance criteria are already
on the target branch, and close the candidate DONE instead of dispatching a
duplicate.

`Planner._rows` is monkeypatched per-instance (the `test_review_cascade.py`
style, adapted from a module-level function to a bound method) so the whole
tick runs against fabricated rows keyed by a distinguishing SQL substring --
no PostgreSQL, no git subprocess.
"""

from __future__ import annotations

from command_center.orchestrator import planner


class _FakeRows:
    def __init__(
        self,
        eligible: list[tuple],
        remediation_parent: str | None = None,
        resumed_before: bool = False,
    ) -> None:
        self.eligible = eligible
        self.remediation_parent = remediation_parent
        self.resumed_before = resumed_before
        self.close_calls: list[tuple] = []
        self.dispatch_calls: list[tuple] = []

    def __call__(self, sql: str, params: tuple = ()) -> list[tuple]:
        if "backlog_lease_acquire" in sql:
            return [(True, "granted", "aicc-planner", None)]
        if "backlog_ingest_results" in sql:
            return []
        if "backlog_task_remediation" in sql:
            return [(self.remediation_parent,)] if self.remediation_parent else []
        if "resume_deferred" in sql and "backlog_event" in sql:
            return [(1,)] if self.resumed_before else []
        if "FROM backlog_eligible" in sql:
            return self.eligible
        if "backlog_close_superseded" in sql:
            self.close_calls.append(params)
            return [(True, "DONE", 2)]
        if "backlog_dispatch" in sql:
            self.dispatch_calls.append(params)
            return [(True, None, "work-item-1", 2)]
        if "backlog_lease_release" in sql:
            return [(True, "released")]
        raise AssertionError(f"unexpected SQL in reuse-gate test: {sql!r}")


def _planner(fake_rows: _FakeRows, reuse_lookup) -> planner.Planner:
    p = planner.Planner(connection_factory=None, reuse_lookup=reuse_lookup)
    p._rows = fake_rows  # type: ignore[method-assign]
    return p


_LIMITS = planner.PlanLimits(max_resumes_per_tick=0, review_backlog_limit=0)


def test_rem_task_whose_parent_landed_on_main_closes_without_dispatch(monkeypatch):
    monkeypatch.setattr(planner, "repo_route", lambda repo: ("AICC", "/srv/aicc"))
    fake = _FakeRows(
        eligible=[
            (
                "VOYN-W0-X-REM", "0", "P1", "Remediation: fix thing",
                "do the fix", "repo-x", True,
            ),
        ],
        remediation_parent="VOYN-W0-X",
    )
    seen_lookups = []

    def reuse_lookup(repository_path, source_task_id, branch):
        seen_lookups.append((repository_path, source_task_id, branch))
        if source_task_id == "VOYN-W0-X":
            return ("f" * 40, "VOYN-W0-X: autonomous delivery (#624)")
        return None

    report = _planner(fake, reuse_lookup).plan_once(_LIMITS)

    assert report.dispatched == []
    assert fake.dispatch_calls == []
    assert report.superseded == [("VOYN-W0-X-REM", "VOYN-W0-X", "f" * 40)]
    # Telemetry: this tick's count of prevented duplicate dispatches.
    assert len(report.superseded) == 1
    assert seen_lookups == [("/srv/aicc", "VOYN-W0-X", "main")]
    assert fake.close_calls == [
        ("VOYN-W0-X-REM", "VOYN-W0-X", "f" * 40, "VOYN-W0-X: autonomous delivery (#624)")
    ]


def test_rem_task_whose_parent_has_not_landed_dispatches_normally(monkeypatch):
    monkeypatch.setattr(planner, "repo_route", lambda repo: ("AICC", "/srv/aicc"))
    fake = _FakeRows(
        eligible=[
            (
                "VOYN-W0-X-REM", "0", "P1", "Remediation: fix thing",
                "do the fix", "repo-x", True,
            ),
        ],
        remediation_parent="VOYN-W0-X",
    )

    report = _planner(fake, lambda *_args: None).plan_once(_LIMITS)

    assert report.superseded == []
    assert fake.close_calls == []
    assert report.dispatched == [("VOYN-W0-X-REM", "work-item-1")]
    assert len(fake.dispatch_calls) == 1


def test_retry_task_checks_its_own_history_not_a_parent(monkeypatch):
    monkeypatch.setattr(planner, "repo_route", lambda repo: ("AICC", "/srv/aicc"))
    fake = _FakeRows(
        eligible=[
            ("VOYN-W0-Y", "0", "P1", "Do the thing", "body", "repo-y", True),
        ],
        remediation_parent=None,
        resumed_before=True,
    )

    def reuse_lookup(repository_path, source_task_id, branch):
        assert source_task_id == "VOYN-W0-Y"  # its own id, no parent exists
        return ("a" * 40, "VOYN-W0-Y: autonomous delivery (#701)")

    report = _planner(fake, reuse_lookup).plan_once(_LIMITS)

    assert report.dispatched == []
    assert report.superseded == [("VOYN-W0-Y", "VOYN-W0-Y", "a" * 40)]


def test_plain_open_task_never_invokes_the_reuse_lookup(monkeypatch):
    monkeypatch.setattr(planner, "repo_route", lambda repo: ("AICC", "/srv/aicc"))
    fake = _FakeRows(
        eligible=[
            ("VOYN-W0-Z", "0", "P1", "Plain task", "body", "repo-z", True),
        ],
        remediation_parent=None,
        resumed_before=False,
    )

    def reuse_lookup(*_args):
        raise AssertionError("the reuse lookup must not run for a first-time OPEN task")

    report = _planner(fake, reuse_lookup).plan_once(_LIMITS)

    assert report.superseded == []
    assert report.dispatched == [("VOYN-W0-Z", "work-item-1")]
