"""The queue recovery CLI surface: parsing only, no database.

The semantics behind each command are proven elsewhere — the SQL protocol in
``test_queue_claim.py``, the Python seam in ``test_work_queue_admin.py``. What
a parser test pins is the operator contract itself: the commands the reaper
timer and the runbooks invoke exist, with the defaults they document.
"""

from __future__ import annotations

import pytest

# The CLI module reaches the pool adapter at import, and the adapter needs the
# vendored `aios_db` wheel — present in CI, optional in a bare local checkout.
pytest.importorskip("aios_db")

from command_center.db.cli import _review_enqueue, build_parser  # noqa: E402


def test_queue_reap_takes_no_arguments() -> None:
    args = build_parser().parse_args(["queue-reap"])
    assert args.command == "queue-reap"


def test_backlog_review_can_target_one_exact_task() -> None:
    args = build_parser().parse_args(
        ["backlog-review", "--repo-path", "/srv/aicc", "--task-id", "VOYN-W0-X"]
    )
    assert args.command == "backlog-review"
    assert args.repo_path == "/srv/aicc"
    assert args.task_id == "VOYN-W0-X"
    assert build_parser().parse_args(["backlog-review"]).task_id is None


def test_queue_dlq_defaults_to_every_queue_fifty_rows() -> None:
    args = build_parser().parse_args(["queue-dlq"])
    assert args.command == "queue-dlq"
    assert args.queue is None and args.limit == 50
    scoped = build_parser().parse_args(
        ["queue-dlq", "--queue", "execution", "--limit", "5"]
    )
    assert scoped.queue == "execution" and scoped.limit == 5


def test_queue_redrive_requires_the_item_id() -> None:
    args = build_parser().parse_args(
        ["queue-redrive", "wki_1", "--extra-attempts", "2"]
    )
    assert args.work_item_id == "wki_1" and args.extra_attempts == 2
    assert build_parser().parse_args(["queue-redrive", "wki_1"]).extra_attempts == 1
    with pytest.raises(SystemExit):
        build_parser().parse_args(["queue-redrive"])


def test_queue_dlq_redrive_batch_requires_clones_root_and_defaults_to_dry_run() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["queue-dlq-redrive-batch"])
    args = build_parser().parse_args(
        ["queue-dlq-redrive-batch", "--clones-root", "/srv/clones"]
    )
    assert args.command == "queue-dlq-redrive-batch"
    assert args.clones_root == "/srv/clones"
    assert args.apply is False
    assert args.repo_path == "."
    assert args.limit == 500
    assert args.extra_attempts == 1
    assert args.poll_interval == 5.0
    assert args.wait_timeout == 1800.0
    applied = build_parser().parse_args(
        [
            "queue-dlq-redrive-batch",
            "--clones-root",
            "/srv/clones",
            "--apply",
            "--queue",
            "execution",
            "--limit",
            "10",
        ]
    )
    assert applied.apply is True
    assert applied.queue == "execution"
    assert applied.limit == 10


def test_queue_dlq_classify_defaults_output_path() -> None:
    args = build_parser().parse_args(["queue-dlq-classify"])
    assert args.command == "queue-dlq-classify"
    assert args.output == "reports/dlq/disposition.jsonl"
    assert args.limit == 1000
    scoped = build_parser().parse_args(
        ["queue-dlq-classify", "--output", "/tmp/out.jsonl", "--limit", "5"]
    )
    assert scoped.output == "/tmp/out.jsonl"
    assert scoped.limit == 5


def test_backlog_merge_reconcile_defaults_repo_path_to_cwd() -> None:
    args = build_parser().parse_args(["backlog-merge-reconcile"])
    assert args.command == "backlog-merge-reconcile"
    assert args.repo_path == "."
    scoped = build_parser().parse_args(
        ["backlog-merge-reconcile", "--repo-path", "/srv/aicc"]
    )
    assert scoped.repo_path == "/srv/aicc"


def test_fleet_status_defaults_to_the_whole_fleet() -> None:
    args = build_parser().parse_args(["fleet-status"])
    assert args.command == "fleet-status"
    assert args.state is None and args.limit == 100
    scoped = build_parser().parse_args(
        ["fleet-status", "--state", "suspended", "--limit", "5"]
    )
    assert scoped.state == "suspended" and scoped.limit == 5
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fleet-status", "--state", "bogus"])


def test_fleet_suspend_requires_the_principal_id_and_a_reason() -> None:
    args = build_parser().parse_args(
        ["fleet-suspend", "worker:edge-00", "--reason", "incident"]
    )
    assert args.principal_id == "worker:edge-00" and args.reason == "incident"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fleet-suspend", "worker:edge-00"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fleet-suspend"])


def test_backlog_review_enqueues_ahead_of_implementation_dispatch() -> None:
    """A review-class enqueue must outrank the priority=0 implementation
    dispatch enqueues (`backlog_dispatch`), or it queues FIFO behind runs
    already occupying a worker slot (VOYN-OPS-AICC-REVIEW-QUEUE-PRIORITY)."""

    calls: list[dict] = []

    class _FakeStore:
        def enqueue(self, queue, *, idempotency_key, payload, task_id,
                    max_attempts, priority):
            calls.append({
                "queue": queue,
                "idempotency_key": idempotency_key,
                "payload": payload,
                "task_id": task_id,
                "max_attempts": max_attempts,
                "priority": priority,
            })
            return "wki_1"

    enqueue = _review_enqueue(_FakeStore())
    work_item_id = enqueue("execution", "key-1", {"kind": "review"}, "VOYN-W0-X", 1)

    assert work_item_id == "wki_1"
    assert calls == [{
        "queue": "execution",
        "idempotency_key": "key-1",
        "payload": {"kind": "review"},
        "task_id": "VOYN-W0-X",
        "max_attempts": 1,
        "priority": 100,
    }]
    assert calls[0]["priority"] > 0


def test_backlog_pr_window_labels_without_opening_a_database(monkeypatch, capsys) -> None:
    """VOYN-W0-AICC-PR-WINDOW-RECONCILER-NOT-DEPLOYED-ON-CONTROL: the tick
    reads and writes GitHub only. Requiring a database credential to run it
    bought nothing and cost it a host -- it is what tied the labeller to the
    one unit layout that had one, on a host the control plane is not, so the
    tick was never deployed and every fleet PR opened with no CI. This runs
    before `load_config`, and the deploy-managed unit therefore needs no
    EnvironmentFile at all."""
    from command_center.db import cli
    from command_center.orchestrator import review_merge

    def refuse(*_args, **_kwargs):
        raise AssertionError("the PR-window tick must not reach the database")

    monkeypatch.setattr(cli, "load_config", refuse)
    monkeypatch.setattr(cli.pool, "open_pool", refuse)
    monkeypatch.setattr(
        review_merge,
        "reconcile_pr_window",
        lambda repo_path: review_merge.PrWindowReport(
            active=[(907, "deadbeef")], blocked=[(906, "checks_missing")]
        ),
    )

    assert cli.main(["backlog-pr-window", "--repo-path", "/opt/aicc/current"]) == 0
    out = capsys.readouterr().out
    assert "ACTIVE    #907 -> deadbeef" in out
    assert "BLOCKED   #906: checks_missing" in out


def test_backlog_pr_window_reports_a_failed_listing_as_a_failed_tick(monkeypatch) -> None:
    """A listing that failed labelled nothing; systemd must see a failed tick
    rather than an empty, successful-looking report (the 500,000-node GraphQL
    refusal did exactly that for days)."""
    from command_center.db import cli
    from command_center.orchestrator import review_merge

    monkeypatch.setattr(
        review_merge,
        "reconcile_pr_window",
        lambda repo_path: review_merge.PrWindowReport(error="pr_list_failed: 403"),
    )

    assert cli.main(["backlog-pr-window"]) == 1
