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


def test_backlog_export_rejects_an_empty_output_path() -> None:
    """`${AICC_MASTER_BACKLOG}` in the systemd unit's `EnvironmentFile=` is a
    braced substitution: unset or blank still yields one argument (`''`),
    never a dropped `--output` flag -- so only a check on the *value*, not
    on presence (`required=True` alone), catches the unconfigured case
    before it reaches `Path('')` and `write_atomically`'s atomic replace of
    the current directory."""
    args = build_parser().parse_args(["backlog-export", "--output", "/tmp/x.md"])
    assert args.output == "/tmp/x.md"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backlog-export", "--output", ""])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backlog-export"])


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


def _run_import(tmp_path, monkeypatch, capsys, text, *extra):
    """Drive `main(["backlog-import", ...])` far enough to reach the file
    check, with a connection object that is `None`.

    Nothing here fakes the store: the point is that the refusal below happens
    with no database work at all, and `None` proves it -- any code path that
    reached for the connection would raise instead of returning a code.
    """
    from contextlib import nullcontext

    from command_center.db import cli as cli_module
    from command_center.db import pool

    path = tmp_path / "backlog.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli_module, "load_config", lambda: object())
    monkeypatch.setattr(pool, "open_pool", lambda config: None)
    monkeypatch.setattr(pool, "connection", lambda: nullcontext(None))
    code = cli_module.main(["backlog-import", str(path), *extra])
    return code, capsys.readouterr()


def test_backlog_import_refuses_a_file_the_exporter_generated(
    tmp_path, monkeypatch, capsys
) -> None:
    """The bidirectional bridge (ADR-0011) is safe only while import reads the
    AUTHORED file and export writes the rendering, and until now nothing
    enforced that -- it was a convention in prose.

    Pointed at a rendering, `backlog-import` would not fail: the exporter
    emits `- VOYN_RECOMMENDATION | ...` lines, which `parse_backlog` does not
    recognise as tasks even to report as `unparsed`, so the run would print
    "inserted 0, updated 0, unchanged 0" and exit 0 while nothing the owner
    typed ever reached the store -- and `ops/aicc_backlog_publish.py`, which
    only checks the exit code, would report a healthy publish every five
    minutes. A misconfigured path has to fail loudly instead.
    """
    from datetime import UTC, datetime

    from command_center.db import backlog_export

    rendered = backlog_export.render_projection(
        [], generated_at=datetime(2026, 9, 9, tzinfo=UTC)
    )
    code, captured = _run_import(tmp_path, monkeypatch, capsys, rendered)

    assert code == 1
    assert "refused" in captured.err
    # Names the fix, not just the fault: which file to point at, and which
    # direction $AICC_MASTER_BACKLOG runs in.
    assert "AICC_MASTER_BACKLOG" in captured.err
    assert "inserted" not in captured.out


def test_backlog_import_still_accepts_an_authored_backlog(
    tmp_path, monkeypatch, capsys
) -> None:
    """The guard's other direction, and the one that would hurt more if it
    were wrong: refusing the owner's real file would freeze the store while
    reporting a clean refusal every tick. `--parse-only` keeps this on the
    same no-database path as the refusal test above."""
    authored = "- **VOYN-W0-X** | Wave 0 | OPEN | P0 | d | `s` | body\n"
    code, captured = _run_import(
        tmp_path, monkeypatch, capsys, authored, "--parse-only"
    )

    assert code == 0
    assert "parsed: 1 tasks" in captured.out
