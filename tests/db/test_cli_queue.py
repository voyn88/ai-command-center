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


def test_mirror_status_takes_no_arguments() -> None:
    args = build_parser().parse_args(["mirror-status"])
    assert args.command == "mirror-status"


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


def test_backlog_merge_reconcile_defaults_repo_path_to_cwd() -> None:
    args = build_parser().parse_args(["backlog-merge-reconcile"])
    assert args.command == "backlog-merge-reconcile"
    assert args.repo_path == "."
    scoped = build_parser().parse_args(
        ["backlog-merge-reconcile", "--repo-path", "/srv/aicc"]
    )
    assert scoped.repo_path == "/srv/aicc"


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
