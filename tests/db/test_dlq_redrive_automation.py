"""Pure-Python unit coverage for the DLQ redrive automation
(VOYN-W0-AICC-DLQ-REDRIVE-AUTOMATION): eligibility filtering, the
strictly-one-at-a-time redrive gate, and the remaining-pool classifier.

Deliberately no live PostgreSQL or GitHub here (those seams are already
proven elsewhere: ``test_work_queue_admin.py`` for ``queue_redrive`` itself,
``runtime/github.py``'s own ``FakeGitHubClient`` for PR discovery). This file
proves the orchestration this task actually adds: which candidates get
skipped and why, that a redrive never fires for an ineligible item, and that
the loop blocks on one item's resolution before it ever considers the next.
"""

from __future__ import annotations

import json
from pathlib import Path

from command_center.db.dlq_redrive_automation import (
    CloneProbe,
    DISPOSITION_NEEDS_REVIEW,
    DISPOSITION_SUPERSEDED,
    DISPOSITION_UNRECOVERABLE,
    UNCOMMITTED_CHANGES_REASON,
    build_default_clone_locator,
    check_eligibility,
    classify_remaining,
    find_uncommitted_changes_pool,
    poll_until_resolved,
    redrive_pool_one_at_a_time,
    write_disposition_report,
)
from command_center.db.work_queue_admin import DeadLetter


def _letter(work_item_id: str, task_id: str | None, dead_reason: str) -> DeadLetter:
    return DeadLetter(
        work_item_id=work_item_id,
        queue="execution",
        task_id=task_id,
        repository_id="repo-1",
        idempotency_key=f"key-{work_item_id}",
        attempt_count=1,
        max_attempts=1,
        dead_reason=dead_reason,
        dead_at="2026-09-06T00:00:00Z",
        attempts_recorded=1,
        last_attempt_reason=None,
    )


class _FakeAdmin:
    """A ``WorkQueueAdmin`` double: redrive is scripted per work_item_id."""

    def __init__(self, accept: dict[str, bool] | None = None) -> None:
        self._accept = accept or {}
        self.redrive_calls: list[tuple[str, int]] = []

    def redrive(self, work_item_id: str, *, extra_attempts: int = 1) -> bool:
        self.redrive_calls.append((work_item_id, extra_attempts))
        return self._accept.get(work_item_id, True)


# -- find_uncommitted_changes_pool ----------------------------------------


def test_pool_only_keeps_the_uncommitted_changes_reason() -> None:
    class _Admin:
        def dead_letters(self, queue, *, limit):
            return [
                _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
                _letter("wki_2", "TASK-B", "non_retryable: bad payload"),
                _letter("wki_3", "TASK-C", UNCOMMITTED_CHANGES_REASON),
            ]

    pool = find_uncommitted_changes_pool(_Admin())
    assert [item.work_item_id for item in pool] == ["wki_1", "wki_3"]


# -- check_eligibility -----------------------------------------------------


def test_ineligible_with_no_task_id() -> None:
    result = check_eligibility(
        _letter("wki_1", None, UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True),
    )
    assert result.eligible is False
    assert "task_id" in result.reason


def test_ineligible_when_a_live_duplicate_exists() -> None:
    result = check_eligibility(
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: True,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True),
    )
    assert result.eligible is False
    assert "duplicate" in result.reason


def test_ineligible_when_an_open_pr_or_branch_already_exists() -> None:
    result = check_eligibility(
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: True,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True),
    )
    assert result.eligible is False
    assert "PR" in result.reason or "branch" in result.reason


def test_ineligible_when_the_clone_is_gone() -> None:
    result = check_eligibility(
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=False, dirty=False),
    )
    assert result.eligible is False
    assert "clone" in result.reason


def test_ineligible_when_the_surviving_clone_is_clean() -> None:
    result = check_eligibility(
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=False, path="/x"),
    )
    assert result.eligible is False
    assert "uncommitted" in result.reason


def test_eligible_when_every_check_passes() -> None:
    result = check_eligibility(
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True, path="/clones/x"),
    )
    assert result.eligible is True
    assert "/clones/x" in result.reason


# -- redrive_pool_one_at_a_time --------------------------------------------


def test_ineligible_candidates_are_never_redriven_or_waited_on() -> None:
    admin = _FakeAdmin()
    waited: list[str] = []
    candidates = [_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)]

    outcomes = redrive_pool_one_at_a_time(
        admin,
        candidates,
        has_live_duplicate=lambda c: True,  # refuse
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True),
        wait_for_resolution=lambda wid: waited.append(wid) or "done",
    )
    assert admin.redrive_calls == []
    assert waited == []
    assert outcomes[0].redriven is False
    assert outcomes[0].eligibility.eligible is False


def test_redrive_loop_waits_for_each_item_before_the_next() -> None:
    admin = _FakeAdmin()
    order: list[str] = []
    candidates = [
        _letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON),
        _letter("wki_2", "TASK-B", UNCOMMITTED_CHANGES_REASON),
    ]

    def _wait(work_item_id: str) -> str:
        order.append(f"wait:{work_item_id}")
        return "completed"

    def _redrive_and_record(work_item_id, *, extra_attempts=1):
        order.append(f"redrive:{work_item_id}")
        return True

    admin.redrive = _redrive_and_record  # type: ignore[method-assign]

    outcomes = redrive_pool_one_at_a_time(
        admin,
        candidates,
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True, path="/x"),
        wait_for_resolution=_wait,
        extra_attempts=1,
    )
    # Strict interleaving: item 2 is never redriven before item 1 resolves.
    assert order == ["redrive:wki_1", "wait:wki_1", "redrive:wki_2", "wait:wki_2"]
    assert [o.redriven for o in outcomes] == [True, True]
    assert [o.resolved_state for o in outcomes] == ["completed", "completed"]


def test_a_server_side_redrive_refusal_skips_the_wait() -> None:
    admin = _FakeAdmin(accept={"wki_1": False})
    waited: list[str] = []
    outcomes = redrive_pool_one_at_a_time(
        admin,
        [_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)],
        has_live_duplicate=lambda c: False,
        has_open_pr_or_branch=lambda c: False,
        locate_clone=lambda t: CloneProbe(exists=True, dirty=True, path="/x"),
        wait_for_resolution=lambda wid: waited.append(wid) or "n/a",
    )
    assert outcomes[0].redriven is False
    assert outcomes[0].resolved_state is None
    assert waited == []


# -- poll_until_resolved ----------------------------------------------------


def test_poll_until_resolved_returns_the_terminal_state() -> None:
    states = iter(["ready", "claimed", "done"])
    slept: list[float] = []
    clock = iter([0.0, 1.0, 2.0, 3.0])
    result = poll_until_resolved(
        lambda wid: next(states),
        "wki_1",
        poll_interval=1.0,
        timeout=100.0,
        sleep=slept.append,
        clock=lambda: next(clock),
    )
    assert result == "done"
    assert slept == [1.0, 1.0]


def test_poll_until_resolved_gives_up_at_the_timeout() -> None:
    clock_values = iter([0.0, 1.0, 2.0, 11.0])
    result = poll_until_resolved(
        lambda wid: "ready",  # never resolves
        "wki_1",
        poll_interval=1.0,
        timeout=10.0,
        sleep=lambda s: None,
        clock=lambda: next(clock_values),
    )
    assert result is None


# -- classify_remaining ------------------------------------------------------


def test_classify_remaining_excludes_the_uncommitted_changes_pool() -> None:
    dispositions = classify_remaining(
        [_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)],
        superseded_check=lambda letter: False,
    )
    assert dispositions == []


def test_classify_remaining_marks_superseded_first() -> None:
    dispositions = classify_remaining(
        [_letter("wki_1", "TASK-A", "non_retryable: whatever")],
        superseded_check=lambda letter: True,
    )
    assert len(dispositions) == 1
    assert dispositions[0].disposition == DISPOSITION_SUPERSEDED


def test_classify_remaining_marks_unrecoverable_reason_markers() -> None:
    dispositions = classify_remaining(
        [_letter("wki_1", "TASK-A", "non_retryable: payload rejected")],
        superseded_check=lambda letter: False,
    )
    assert dispositions[0].disposition == DISPOSITION_UNRECOVERABLE


def test_classify_remaining_falls_back_to_needs_review() -> None:
    dispositions = classify_remaining(
        [_letter("wki_1", "TASK-A", "some_unrecognized_reason")],
        superseded_check=lambda letter: False,
    )
    assert dispositions[0].disposition == DISPOSITION_NEEDS_REVIEW


# -- write_disposition_report -------------------------------------------


def test_write_disposition_report_appends_without_truncating(tmp_path: Path) -> None:
    report = tmp_path / "dlq" / "disposition.jsonl"
    dispositions = classify_remaining(
        [_letter("wki_1", "TASK-A", "some_unrecognized_reason")],
        superseded_check=lambda letter: False,
    )
    first = write_disposition_report(report, dispositions, generated_at="t0")
    second = write_disposition_report(report, dispositions, generated_at="t1")
    assert first == 1 and second == 1
    lines = report.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    recorded = [json.loads(line) for line in lines]
    assert [r["generated_at"] for r in recorded] == ["t0", "t1"]
    assert all(r["work_item_id"] == "wki_1" for r in recorded)


# -- build_default_clone_locator (real git, no network) ----------------------


def _init_repo(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


def test_clone_locator_reports_missing_clone(tmp_path: Path) -> None:
    locate = build_default_clone_locator(tmp_path)
    probe = locate("TASK-NOWHERE")
    assert probe.exists is False
    assert probe.dirty is False


def test_clone_locator_reports_clean_clone(tmp_path: Path) -> None:
    _init_repo(tmp_path / "backlog-TASK-A-deadbeef")
    locate = build_default_clone_locator(tmp_path)
    probe = locate("TASK-A")
    assert probe.exists is True
    assert probe.dirty is False


def test_clone_locator_reports_dirty_clone(tmp_path: Path) -> None:
    clone_dir = tmp_path / "backlog-TASK-A-deadbeef"
    _init_repo(clone_dir)
    (clone_dir / "f.txt").write_text("changed\n")
    locate = build_default_clone_locator(tmp_path)
    probe = locate("TASK-A")
    assert probe.exists is True
    assert probe.dirty is True
    assert probe.path == str(clone_dir)


# -- build_default_duplicate_checker (fake DB connection) --------------------


class _FakeCursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row
        self.executed: tuple[str, tuple] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed = (sql, params)

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)


def test_duplicate_checker_true_when_a_row_is_found() -> None:
    from command_center.db.dlq_redrive_automation import build_default_duplicate_checker

    checker = build_default_duplicate_checker(_FakeConnection(row=(1,)))
    assert checker(_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)) is True


def test_duplicate_checker_false_when_no_row_is_found() -> None:
    from command_center.db.dlq_redrive_automation import build_default_duplicate_checker

    checker = build_default_duplicate_checker(_FakeConnection(row=None))
    assert checker(_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)) is False


def test_duplicate_checker_short_circuits_with_no_task_id() -> None:
    from command_center.db.dlq_redrive_automation import build_default_duplicate_checker

    checker = build_default_duplicate_checker(_FakeConnection(row=(1,)))
    assert checker(_letter("wki_1", None, UNCOMMITTED_CHANGES_REASON)) is False


# -- build_default_pr_or_branch_checker / build_default_superseded_checker ---


def test_pr_or_branch_checker_blocks_on_an_open_pr(tmp_path: Path, monkeypatch) -> None:
    from command_center.db.dlq_redrive_automation import build_default_pr_or_branch_checker
    from command_center.runtime.github import FakeGitHubClient, PullRequestState, STATE_OPEN

    gh = FakeGitHubClient()
    gh.seed(PullRequestState(state=STATE_OPEN, head_ref="backlog/TASK-A"))
    monkeypatch.setattr(
        "command_center.runtime.repo_state.remote_branch_exists",
        lambda repo, remote, branch, **kw: False,
    )
    checker = build_default_pr_or_branch_checker(tmp_path, github_client=gh)
    assert checker(_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)) is True


def test_pr_or_branch_checker_blocks_on_a_surviving_remote_branch(
    tmp_path: Path, monkeypatch
) -> None:
    from command_center.db.dlq_redrive_automation import build_default_pr_or_branch_checker
    from command_center.runtime.github import FakeGitHubClient

    monkeypatch.setattr(
        "command_center.runtime.repo_state.remote_branch_exists",
        lambda repo, remote, branch, **kw: True,
    )
    checker = build_default_pr_or_branch_checker(tmp_path, github_client=FakeGitHubClient())
    assert checker(_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)) is True


def test_pr_or_branch_checker_clear_when_nothing_survives(
    tmp_path: Path, monkeypatch
) -> None:
    from command_center.db.dlq_redrive_automation import build_default_pr_or_branch_checker
    from command_center.runtime.github import FakeGitHubClient

    monkeypatch.setattr(
        "command_center.runtime.repo_state.remote_branch_exists",
        lambda repo, remote, branch, **kw: False,
    )
    checker = build_default_pr_or_branch_checker(tmp_path, github_client=FakeGitHubClient())
    assert checker(_letter("wki_1", "TASK-A", UNCOMMITTED_CHANGES_REASON)) is False


def test_superseded_checker_true_for_merged_pr(tmp_path: Path) -> None:
    from command_center.db.dlq_redrive_automation import build_default_superseded_checker
    from command_center.runtime.github import FakeGitHubClient, PullRequestState, STATE_MERGED

    gh = FakeGitHubClient()
    gh.seed(
        PullRequestState(
            state=STATE_MERGED, merged_at="2026-09-06T00:00:00Z", head_ref="backlog/TASK-A"
        )
    )
    checker = build_default_superseded_checker(tmp_path, github_client=gh)
    assert checker(_letter("wki_1", "TASK-A", "some_reason")) is True


def test_superseded_checker_false_with_no_pr(tmp_path: Path) -> None:
    from command_center.db.dlq_redrive_automation import build_default_superseded_checker
    from command_center.runtime.github import FakeGitHubClient

    checker = build_default_superseded_checker(tmp_path, github_client=FakeGitHubClient())
    assert checker(_letter("wki_1", "TASK-A", "some_reason")) is False
