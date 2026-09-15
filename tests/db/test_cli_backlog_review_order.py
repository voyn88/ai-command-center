"""The `backlog-review` tick's call order is itself a correctness property.

``reconcile_review_once`` only helps if it runs BEFORE
``publish_review_verdicts`` reads the review rows: the retry it enqueues is
what turns an unreadable identity into a readable one. Run it after the
publisher and every malformed review waits a whole extra tick -- run it never
and #606's exact-head acceptance stalls forever, which is the bug this exists
to clear. Nothing else in the tick pins that order, so this does.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

# Same guard as test_cli_queue.py: the CLI module reaches the pool adapter at
# import, and the adapter needs the vendored `aios_db` wheel.
pytest.importorskip("aios_db")

from command_center.db import cli  # noqa: E402
from command_center.orchestrator import review_merge  # noqa: E402


class _Report:
    """Empty stand-in for both LoopReport and ReconcileReport: the tick
    prints every list on whichever it gets back."""

    def __init__(self) -> None:
        self.reviewed: list = []
        self.merged: list = []
        self.skipped: list = []
        self.remediated: list = []
        self.retried: list = []
        self.recorded: list = []


def test_reconciliation_runs_between_review_and_marker_publication(monkeypatch):
    calls: list[tuple[str, str | None, str | None]] = []

    def record(name):
        def _call(*args, **kwargs):
            # Every one of these takes (factory, ..., repo_path) positionally
            # and the targeted task_id by keyword.
            repo_path = next((a for a in args if a == "/srv/aicc"), None)
            calls.append((name, repo_path, kwargs.get("task_id")))
            return _Report()

        return _call

    for name in (
        "reconcile_pr_evidence",
        "review_once",
        "reconcile_review_once",
        "publish_review_verdicts",
    ):
        monkeypatch.setattr(review_merge, name, record(name))

    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(redacted=lambda: ""))
    monkeypatch.setattr(cli.pool, "open_pool", lambda _config: None)
    monkeypatch.setattr(cli.pool, "close_pool", lambda: None)
    monkeypatch.setattr(cli.pool, "connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "command_center.db.work_queue_store.WorkQueueStore", lambda _factory: object()
    )

    exit_code = cli.main(
        ["backlog-review", "--repo-path", "/srv/aicc", "--task-id", "VOYN-W0-X"]
    )

    assert exit_code == 0
    assert [name for name, _repo, _task in calls] == [
        "reconcile_pr_evidence",
        "review_once",
        "reconcile_review_once",
        "publish_review_verdicts",
    ]
    # The retry pass sees the same repo and the same targeting as the rest of
    # the tick -- a targeted invocation must not silently reconcile the world.
    assert all(repo == "/srv/aicc" for _name, repo, _task in calls)
    assert all(task == "VOYN-W0-X" for _name, _repo, task in calls)
