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


def test_migration_lock_checks_without_a_database(capsys) -> None:
    """The one migration command that runs where the edit is made. It is
    deliberately handled before `load_config()`: a laptop with no DSN must
    still get the verdict, or the guard is only reachable from the host that
    is already broken (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH)."""
    from command_center.db import migrations
    from command_center.db.cli import main

    assert main(["migration-lock"]) == 0
    assert f"{len(migrations.discover())} migrations unchanged" in capsys.readouterr().out


def test_migration_lock_reports_an_edited_file_as_a_refusal(
    capsys, monkeypatch, tmp_path
) -> None:
    """Exit 2 and a message, never a traceback: this command exists to be
    read."""
    from command_center.db import migrations
    from command_center.db.cli import main

    def refuse(sql_dir=None):
        raise migrations.MigrationError("0022_queue_fail_lease_wait.up.sql changed")

    monkeypatch.setattr(migrations, "verify_released_checksums", refuse)
    assert main(["migration-lock"]) == 2
    assert "migration lock: 0022_queue_fail_lease_wait.up.sql changed" in (
        capsys.readouterr().err
    )


def test_upgrade_reports_a_refusing_ledger_as_a_message(capsys, monkeypatch) -> None:
    """`self-deploy --migrate` runs this as a subprocess and puts a bounded
    slice of its stderr in the deploy report. An uncaught `MigrationError`
    made that slice a traceback header, so the one failure that never clears
    by itself -- an already-applied file edited since -- was also the one the
    operator could not read (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH)."""
    from contextlib import nullcontext

    from command_center.db import cli, migrations

    monkeypatch.setattr(cli, "load_config", lambda: _StubConfig())
    monkeypatch.setattr(cli.pool, "open_pool", lambda config: None)
    monkeypatch.setattr(cli.pool, "close_pool", lambda: None)
    monkeypatch.setattr(cli.pool, "connection", lambda: nullcontext(object()))

    def refuse(conn, **kwargs):
        raise migrations.MigrationError(
            "migration 0022_queue_fail_lease_wait was modified after it was applied"
        )

    monkeypatch.setattr(cli.migrations, "upgrade", refuse)

    assert cli.main(["upgrade"]) == 2
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "migration refused: migration 0022_queue_fail_lease_wait was modified "
        "after it was applied"
    ]
    # The grant re-assertion must not run behind a refused migration.
    assert "table grants" not in captured.out


class _StubConfig:
    def redacted(self) -> str:
        return "postgresql://stub"
