"""Repo-route / PR-evidence hygiene without a live database.

Covers the Python side of VOYN-W0-AICC-REPO-ROUTE-AND-EVIDENCE-HYGIENE:
malformed PR URLs are cleared (not retried as pr_view_failed forever),
shape-valid unrouted repos stay as operator skips, and transient lookup
failures remain retryable. SQL bodies are exercised externally when PG is
available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.db.backlog_store import BacklogStore
from command_center.orchestrator import planner, review_merge

BAD_PR = "not-a-github-pr-url"
GOOD_PR = "https://github.com/voyn88/ai-command-center/pull/969"
MERGED_PR = "https://github.com/voyn88/ai-command-center/pull/1019"


def test_malformed_pr_url_is_cleared_by_review_once(monkeypatch):
    cleared: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        review_merge,
        "_model_only_review_cascade",
        lambda: [{"executor": "codex", "task_type": "independent_review"}],
    )
    monkeypatch.setattr(
        review_merge, "_scan_tasks", lambda *a, **k: ([("VOYN-W0-BAD", BAD_PR)], None)
    )
    monkeypatch.setattr(
        review_merge,
        "_clear_pr_evidence",
        lambda _f, task_id, pr_url, reason: (
            cleared.append((task_id, pr_url, reason)) or "cleared_and_open"
        ),
    )
    enqueued: list[object] = []
    report = review_merge.review_once(
        object(), lambda *args: enqueued.append(args), "/srv/aicc"
    )

    assert enqueued == []
    assert cleared == [("VOYN-W0-BAD", BAD_PR, "malformed_pr_url")]
    assert report.skipped == [
        ("VOYN-W0-BAD", f"malformed_pr_url:cleared_and_open: {BAD_PR!r}")
    ]


def test_publish_and_merge_clear_malformed_without_calling_gh(monkeypatch):
    cleared: list[str] = []

    def clear(_f, task_id, pr_url, reason):
        cleared.append(f"{task_id}:{reason}")
        return "cleared"

    monkeypatch.setattr(review_merge, "_clear_pr_evidence", clear)
    monkeypatch.setattr(
        review_merge,
        "_scan_tasks",
        lambda *a, **k: ([("VOYN-W0-BAD", BAD_PR)], None),
    )
    monkeypatch.setattr(review_merge, "_scan_commit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        review_merge,
        "_pr_window_expensive_read_decision",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("malformed URLs must not spend a window lookup")
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_merged_target_sha",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("malformed URLs must not call gh pr view")
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_merge_window_open_pr_snapshot",
        lambda *_a, **_k: (None, "unused"),
    )

    pub = review_merge.publish_review_verdicts(object(), "/srv/aicc")
    merge = review_merge.merge_once(object(), "/srv/aicc")

    assert cleared == [
        "VOYN-W0-BAD:malformed_pr_url",
        "VOYN-W0-BAD:malformed_pr_url",
    ]
    assert pub.skipped[0][1].startswith("malformed_pr_url:cleared:")
    assert merge.skipped[0][1].startswith("malformed_pr_url:cleared:")


def test_shape_valid_unrouted_pr_is_not_cleared(monkeypatch):
    """A real-shaped URL for a repo missing from the route table is an
    operator fact (add the route), not bogus evidence -- leave it alone."""
    cleared: list[object] = []
    monkeypatch.setattr(
        review_merge,
        "_model_only_review_cascade",
        lambda: [{"executor": "codex", "task_type": "independent_review"}],
    )
    monkeypatch.setattr(
        review_merge,
        "_scan_tasks",
        lambda *a, **k: ([("VOYN-W0-UR", GOOD_PR)], None),
    )
    monkeypatch.setattr(
        review_merge,
        "_clear_pr_evidence",
        lambda *_a, **_k: cleared.append(True) or "cleared",
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda _repo: None,
    )
    report = review_merge.review_once(object(), lambda *a: None, "/srv/aicc")
    assert cleared == []
    assert report.skipped == [("VOYN-W0-UR", f"no_repo_route: {GOOD_PR!r}")]


def test_transient_pr_view_failed_is_not_cleared(monkeypatch):
    """A shape-valid URL whose gh lookup failed this tick stays retryable."""
    cleared: list[object] = []
    monkeypatch.setattr(
        review_merge,
        "_scan_tasks",
        lambda *a, **k: ([("VOYN-W0-TV", GOOD_PR)], None),
    )
    monkeypatch.setattr(
        review_merge,
        "_clear_pr_evidence",
        lambda *_a, **_k: cleared.append(True) or "cleared",
    )
    monkeypatch.setattr(
        review_merge,
        "_pr_window_expensive_read_decision",
        lambda *_a, **_k: review_merge._PrWindowReadDecision(
            True, "review_window_active", {"state": "open", "merged_at": None}
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_has_accept_marker_with_pull",
        lambda *_a, **_k: (False, ""),
    )
    report = review_merge.publish_review_verdicts(object(), "/srv/aicc")
    assert cleared == []
    assert report.skipped == [("VOYN-W0-TV", "pr_view_failed")]


def test_merged_without_acceptance_is_not_done_and_not_cleared(monkeypatch):
    """Live evidence: merged PR1019 chunks were refused manually; never mark
    DONE merely because GitHub says MERGED — retain verification semantics."""
    cleared: list[object] = []
    monkeypatch.setattr(
        review_merge,
        "_scan_tasks",
        lambda *a, **k: ([("VOYN-W0-M", MERGED_PR)], None),
    )
    monkeypatch.setattr(
        review_merge,
        "_clear_pr_evidence",
        lambda *_a, **_k: cleared.append(True) or "cleared",
    )
    monkeypatch.setattr(
        review_merge,
        "_merge_window_open_pr_snapshot",
        lambda *_a, **_k: (None, "unused"),
    )
    monkeypatch.setattr(
        review_merge,
        "_pr_window_expensive_read_decision",
        lambda *_a, **_k: review_merge._PrWindowReadDecision(
            True,
            "pr_not_open",
            {"state": "closed", "merged_at": "2026-10-01T00:00:00Z"},
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_merged_target_sha",
        lambda *_a, **_k: (None, "merged_without_acceptance_evidence"),
    )
    monkeypatch.setattr(review_merge, "_scan_commit", lambda *_a, **_k: None)
    report = review_merge.merge_once(object(), "/srv/aicc")
    assert cleared == []
    assert report.merged == []
    assert report.skipped == [("VOYN-W0-M", "merged_without_acceptance_evidence")]


def test_audit_repo_routes_is_report_only(monkeypatch):
    rows = {
        "OPEN": [("A", "ai-command-center"), ("B", "missing-repo"), ("C", "")],
        "READY_TO_REVIEW": [("D", "ghost"), ("E", "ai-command-center")],
    }

    class Cur:
        def __init__(self) -> None:
            self._status = ""

        def execute(self, sql, params=()):
            if "status = 'OPEN'" in sql:
                self._status = "OPEN"
            else:
                self._status = "READY_TO_REVIEW"

        def fetchall(self):
            return rows[self._status]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.delenv("AICC_PLANNER_REPO_ROUTES", raising=False)
    audit = planner.audit_repo_routes(lambda: Conn())
    assert audit.open_unrouted == [("B", "missing-repo"), ("C", "")]
    assert audit.ready_to_review_unrouted == [("D", "ghost")]


def test_migration_0030_keeps_shape_gate_and_does_not_reuse_0017():
    sql_dir = Path(planner.__file__).resolve().parents[1] / "db" / "sql"
    assert (sql_dir / "0030_repo_route_and_evidence_hygiene.up.sql").is_file()
    assert (sql_dir / "0030_repo_route_and_evidence_hygiene.down.sql").is_file()
    # 0017 already means resume_window on current main — never overwrite it.
    resume = (sql_dir / "0017_backlog_resume_window.up.sql").read_text(encoding="utf-8")
    assert "backlog_resume" in resume
    up = (sql_dir / "0030_repo_route_and_evidence_hygiene.up.sql").read_text(
        encoding="utf-8"
    )
    assert "backlog_clear_evidence" in up
    assert "malformed_pr_url" in up
    assert "SPLIT_TASKS_JSON" in up  # preserves 0021 split-aware ingest
    assert "v_malformed AND v_bad_returns >= 1" in up


@pytest.mark.parametrize("url", [
    "https://github.com/owner?x/ai-command-center/pull/1",
    "https://github.com/owner/ai command center/pull/1",
    "https://github.com/owner/repo/pull/0",
    "https://github.com/owner/repo/pull/\u0661",
    "https://github.com/owner/repo/pull/1\n",
])
def test_pr_shape_rejects_unresolvable_urls(url):
    assert review_merge._repo_from_pr_url(url) is None
    assert review_merge._owner_repo_number_from_pr_url(url) is None


# recover_evidence_free_ready_to_review: VOYN-W0-AICC-SRV-08-CLEAN-HOST-STALL.
#
# `backlog_recover_stuck_ready_to_review` (0018) had been the sanctioned way
# out of "READY_TO_REVIEW with no `pr` evidence" since it shipped, but no
# tick ever called it -- only its own tests did. A task that reached that
# state (residue from before 0011, or any future bug reintroducing it) sat
# reporting `no_open_pr_on_task_branch` identically forever, which is what
# the tick-stall watchdog caught live on VOYN-W0-AICC-SRV-08-CLEAN-HOST.


def test_recovers_only_the_no_open_pr_on_task_branch_reason(monkeypatch):
    calls: list[str] = []

    def fake_recover(self, task_id):
        calls.append(task_id)
        return True, "OPEN", 3

    monkeypatch.setattr(BacklogStore, "recover_stuck_ready_to_review", fake_recover)

    evidence = review_merge.PrEvidenceReport(
        recorded=[],
        skipped=[
            ("VOYN-W0-AICC-SRV-08-CLEAN-HOST", "no_open_pr_on_task_branch"),
            ("VOYN-W0-FLAKY", "pr_list_failed"),
            ("VOYN-W0-NOROUTE", "no_repo_route: 'ghost'"),
            ("VOYN-W0-AMBIGUOUS", "ambiguous_open_prs_on_branch"),
        ],
    )
    report = review_merge.recover_evidence_free_ready_to_review(object(), evidence)

    assert calls == ["VOYN-W0-AICC-SRV-08-CLEAN-HOST"]
    assert report.recovered == [("VOYN-W0-AICC-SRV-08-CLEAN-HOST", 3)]
    assert report.refused == []


def test_a_refusal_from_the_sanctioned_recovery_function_is_surfaced(monkeypatch):
    """A task that gained `pr` evidence between the two reads (a real PR
    attached by this same tick, or by a concurrent one) is left alone --
    the 0018 function's own `has_pr_evidence` refusal, not silently eaten."""
    monkeypatch.setattr(
        BacklogStore,
        "recover_stuck_ready_to_review",
        lambda self, task_id: (False, "has_pr_evidence", 5),
    )
    evidence = review_merge.PrEvidenceReport(
        skipped=[("VOYN-W0-RACY", "no_open_pr_on_task_branch")]
    )
    report = review_merge.recover_evidence_free_ready_to_review(object(), evidence)

    assert report.recovered == []
    assert report.refused == [("VOYN-W0-RACY", "has_pr_evidence")]


def test_no_candidates_never_touches_the_store(monkeypatch):
    def fail(self, task_id):
        raise AssertionError("must not be called with no matching skip reason")

    monkeypatch.setattr(BacklogStore, "recover_stuck_ready_to_review", fail)

    evidence = review_merge.PrEvidenceReport(
        skipped=[("VOYN-W0-FLAKY", "pr_list_failed")]
    )
    report = review_merge.recover_evidence_free_ready_to_review(object(), evidence)

    assert report.recovered == []
    assert report.refused == []
