"""Reviews must not starve behind implementation work in the shared queue.

Measured live 2026-08-23: PR #366's review work item sat `ready` with
`attempt_count = 0` across five consecutive orchestrator ticks while the
queue held 45 ready items and the fleet claimed two at a time. Nothing had
failed -- the review was simply last in a FIFO lane, and no merge in the
programme could happen until it ran.
"""

from __future__ import annotations

from command_center.orchestrator import review_merge


def test_review_is_enqueued_above_implementation_work(monkeypatch):
    """The regression this guards is silent: dropping the priority argument
    leaves every call still working, every test still green, and the merge
    train still stopped. So assert on the argument the queue orders by."""
    monkeypatch.setattr(
        review_merge, "_rows",
        lambda *a, **k: [("VOYN-T", "https://github.com/o/r/pull/7")],
    )
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head", lambda *a, **k: ("diff", "abc123")
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: {"project_id": "AIOS", "repository_path": "/tmp/r"},
    )
    calls: list[tuple] = []
    review_merge.review_once(object(), lambda *a: calls.append(a), "/tmp/r")

    assert len(calls) == 1, calls
    priority = calls[0][4]
    assert priority > 0, (
        "a review enqueued at the default priority is ordered behind every "
        "implementation item already waiting, which is exactly the starvation "
        "that stopped the merge train"
    )
