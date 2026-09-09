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

from command_center.db import legacy_migration as lm  # noqa: E402
from command_center.db.cli import _review_enqueue, build_parser, main  # noqa: E402


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


def test_legacy_migrate_defaults_every_path(tmp_path) -> None:
    args = build_parser().parse_args(["legacy-migrate"])
    assert args.command == "legacy-migrate"
    assert args.sqlite_path is None
    assert args.queue_path is None
    assert args.snapshot_dir is None
    assert args.report_dir is None

    scoped = build_parser().parse_args(
        [
            "legacy-migrate",
            "--sqlite-path", str(tmp_path / "runtime.db"),
            "--queue-path", str(tmp_path / "execution_queue.json"),
            "--snapshot-dir", str(tmp_path / "snapshots"),
            "--report-dir", str(tmp_path / "reports"),
        ]
    )
    assert scoped.sqlite_path == str(tmp_path / "runtime.db")
    assert scoped.report_dir == str(tmp_path / "reports")


def test_legacy_reconcile_takes_the_same_flags_as_legacy_migrate() -> None:
    args = build_parser().parse_args(["legacy-reconcile"])
    assert args.command == "legacy-reconcile"
    assert args.sqlite_path is None and args.snapshot_dir is None


def test_legacy_lock_requires_a_reconciliation_report() -> None:
    args = build_parser().parse_args(["legacy-lock", "--report", "reports/reconciliation-report.json"])
    assert args.command == "legacy-lock"
    assert args.report == "reports/reconciliation-report.json"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["legacy-lock"])


def test_legacy_unlock_takes_no_required_arguments() -> None:
    args = build_parser().parse_args(["legacy-unlock"])
    assert args.command == "legacy-unlock"
    assert args.sqlite_path is None and args.queue_path is None


def test_every_legacy_command_can_address_the_jsonl_leg(tmp_path) -> None:
    """`runs.jsonl` is the third legacy source, and rollback has to reach the
    same set the cutover locked -- a `--runs-path` on migrate but not on unlock
    would leave a redirected install with no supported way to undo it."""
    for command in ("legacy-migrate", "legacy-reconcile", "legacy-unlock"):
        assert build_parser().parse_args([command]).runs_path is None
        scoped = build_parser().parse_args([command, "--runs-path", str(tmp_path / "runs.jsonl")])
        assert scoped.runs_path == str(tmp_path / "runs.jsonl")

    locked = build_parser().parse_args(
        ["legacy-lock", "--report", "r.json", "--runs-path", str(tmp_path / "runs.jsonl")]
    )
    assert locked.runs_path == str(tmp_path / "runs.jsonl")


def test_legacy_migrate_drain_is_opt_in() -> None:
    """Draining writes v2 rows into the legacy SQLite store. A migration that
    did that unasked would mutate the very authority it is about to freeze."""
    assert build_parser().parse_args(["legacy-migrate"]).drain_legacy_runs is False
    assert build_parser().parse_args(["legacy-migrate", "--drain-legacy-runs"]).drain_legacy_runs is True


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


# --- the cutover commands, dispatched for real -------------------------------
#
# These two are the exception to this module's "parsing only, no database"
# rule, and deliberately so: `legacy-lock`/`legacy-unlock` are pure filesystem
# operations that must run with no database at all, so dispatching them here
# costs nothing and covers what a parser test structurally cannot. It is also
# the gap that hid a real defect -- `main()` carried a function-local
# `from pathlib import Path`, which made `Path` local to the whole function and
# left `legacy-lock --report ...` raising `UnboundLocalError` before it did
# anything. Every existing test stopped at `parse_args`, so the cutover command
# was broken end to end while the suite stayed green.


def _legacy_sources(tmp_path):
    sqlite_path = tmp_path / "runtime.db"
    sqlite_path.write_bytes(b"legacy sqlite")
    queue_path = tmp_path / "execution_queue.json"
    queue_path.write_text("[]", encoding="utf-8")
    runs_path = tmp_path / "runs.jsonl"
    runs_path.write_text("", encoding="utf-8")
    return sqlite_path, queue_path, runs_path


def _report_file(tmp_path, report) -> str:
    out = tmp_path / "reconciliation-report.json"
    lm.write_report(report, out)
    return str(out)


def _argv(command, paths, *extra):
    sqlite_path, queue_path, runs_path = paths
    return [
        command,
        "--sqlite-path", str(sqlite_path),
        "--queue-path", str(queue_path),
        "--runs-path", str(runs_path),
        *extra,
    ]


def test_legacy_lock_and_unlock_run_without_any_database_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    """Rollback must not need a reachable PostgreSQL -- a broken database is
    exactly when an operator reaches for it."""
    for variable in ("AICC_PG_HOST", "AICC_PG_PORT", "AICC_PG_DB", "AICC_PG_USER",
                     "AICC_PG_PASSWORD", "AICC_PG_SSLMODE"):
        monkeypatch.delenv(variable, raising=False)
    paths = _legacy_sources(tmp_path)
    report = lm.ReconciliationReport(
        generated_at="now",
        snapshot_sha256={},
        tables=[],
        sources={str(path): lm._fingerprint(path) for path in paths},
    )

    assert main(_argv("legacy-lock", paths, "--report", _report_file(tmp_path, report))) == 0
    assert all(not (path.stat().st_mode & 0o200) for path in paths)
    assert "locked:" in capsys.readouterr().out

    assert main(_argv("legacy-unlock", paths)) == 0
    assert all(path.stat().st_mode & 0o200 for path in paths)


def test_legacy_lock_refuses_a_dirty_report_with_a_message_not_a_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("AICC_PG_HOST", raising=False)
    paths = _legacy_sources(tmp_path)
    dirty = lm.ReconciliationReport(
        generated_at="now",
        snapshot_sha256={},
        tables=[lm.TableReconciliation("task", 1, 0, [{"id": "x"}])],
        sources={str(path): lm._fingerprint(path) for path in paths},
    )

    assert main(_argv("legacy-lock", paths, "--report", _report_file(tmp_path, dirty))) == 1
    assert "refused:" in capsys.readouterr().err
    assert all(path.stat().st_mode & 0o200 for path in paths)
