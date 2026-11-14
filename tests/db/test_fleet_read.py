"""Unit tests for ``FleetReadStore`` against an in-memory fake connection.

PostgreSQL integration coverage for the same tables lives elsewhere; this file
pins the Python seam — SQL shape, row shaping, and the AICC_PG_* gate — without
requiring a live server.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.db.fleet_read import (
    FleetReadStore,
    ensure_pool,
    postgres_configured,
)


class _FakeCursor:
    def __init__(self, responses: list[list[tuple]]) -> None:
        self._responses = list(responses)
        self.statements: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if not self._responses:
            raise AssertionError(f"unexpected execute: {sql!r}")
        self._rows = self._responses.pop(0)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, responses: list[list[tuple]]) -> None:
        self.cursor_obj = _FakeCursor(responses)

    def cursor(self):
        return self.cursor_obj


def _factory(responses: list[list[tuple]]):
    conn = _FakeConn(responses)

    @contextmanager
    def factory():
        yield conn

    return factory, conn


def test_postgres_configured_requires_host(monkeypatch) -> None:
    monkeypatch.delenv("AICC_PG_HOST", raising=False)
    assert postgres_configured() is False
    assert postgres_configured({"AICC_PG_HOST": ""}) is False
    assert postgres_configured({"AICC_PG_HOST": "  "}) is False
    assert postgres_configured({"AICC_PG_HOST": "127.0.0.1"}) is True


def test_ensure_pool_is_a_no_op_without_host(monkeypatch) -> None:
    monkeypatch.delenv("AICC_PG_HOST", raising=False)
    ensure_pool()  # must not raise or import a live pool open


def test_tasks_by_status_wave_shapes_counts() -> None:
    factory, conn = _factory(
        [[("OPEN", "0", 3), ("IN_PROGRESS", "0", 1), ("DONE", "1", 2)]]
    )
    rows = FleetReadStore(factory).tasks_by_status_wave()
    assert rows == [
        {"status": "OPEN", "wave": "0", "count": 3},
        {"status": "IN_PROGRESS", "wave": "0", "count": 1},
        {"status": "DONE", "wave": "1", "count": 2},
    ]
    assert "backlog_task" in conn.cursor_obj.statements[0][0]


def test_queue_by_state_returns_counts_and_reasons() -> None:
    factory, conn = _factory(
        [
            [("ready", 4), ("dead", 1)],
            [("wki_1", "dead", "max_attempts_exhausted", "t1")],
            [
                (
                    "att_1",
                    "wki_2",
                    "aicc_w_wrk_voyn_worker_01",
                    "failed",
                    "lease_expired",
                    "t2",
                )
            ],
        ]
    )
    payload = FleetReadStore(factory).queue_by_state(reason_limit=5)
    assert payload["counts"] == [
        {"state": "ready", "count": 4},
        {"state": "dead", "count": 1},
    ]
    assert payload["dead_reasons"][0]["dead_reason"] == "max_attempts_exhausted"
    assert payload["attempt_reasons"][0]["outcome_reason"] == "lease_expired"
    sqls = [sql for sql, _ in conn.cursor_obj.statements]
    assert any("work_item_public" in sql for sql in sqls)
    assert any("work_attempt_public" in sql for sql in sqls)


def test_recent_attempts_per_executor_serializes_timestamps() -> None:
    factory, _conn = _factory(
        [
            [
                (
                    "att_9",
                    "wki_9",
                    2,
                    "aicc_w_wrk_voyn_control_01",
                    "succeeded",
                    None,
                    "2026-09-09T10:00:00+00:00",
                    "2026-09-09T10:01:00+00:00",
                )
            ]
        ]
    )
    rows = FleetReadStore(factory).recent_attempts_per_executor()
    assert rows[0]["claimed_by_role"] == "aicc_w_wrk_voyn_control_01"
    assert rows[0]["created_at"] == "2026-09-09T10:00:00+00:00"
    assert rows[0]["outcome_reason"] is None


def test_open_monitor_findings_includes_linked_task() -> None:
    factory, _conn = _factory(
        [
            [
                (
                    42,
                    "worker-01",
                    "unit_failed",
                    "VOYN-MON-WORKER-01-INFRA",
                    "2026-09-09T08:00:00+00:00",
                    "2026-09-09T09:00:00+00:00",
                )
            ]
        ]
    )
    rows = FleetReadStore(factory).open_monitor_findings()
    assert rows == [
        {
            "finding_id": 42,
            "source": "worker-01",
            "failure": "unit_failed",
            "task_id": "VOYN-MON-WORKER-01-INFRA",
            "opened_at": "2026-09-09T08:00:00+00:00",
            "seen_at": "2026-09-09T09:00:00+00:00",
        }
    ]


def test_merged_prs_per_day_counts_sha_evidence() -> None:
    factory, conn = _factory([[("2026-09-09", 5), ("2026-09-08", 2)]])
    rows = FleetReadStore(factory).merged_prs_per_day(days=14)
    assert rows == [
        {"day": "2026-09-09", "merges": 5},
        {"day": "2026-09-08", "merges": 2},
    ]
    sql, params = conn.cursor_obj.statements[0]
    assert "backlog_evidence" in sql and "kind = 'sha'" in sql
    assert params == (14,)


def test_ensure_pool_opens_when_host_set_and_pool_closed(monkeypatch) -> None:
    calls: list[str] = []
    from command_center.db import pool as pool_mod

    class _Closed(RuntimeError):
        pass

    def _get_pool():
        calls.append("get")
        raise _Closed("closed")

    def _open_pool():
        calls.append("open")

    monkeypatch.setenv("AICC_PG_HOST", "127.0.0.1")
    monkeypatch.setattr(pool_mod, "PoolNotOpenError", _Closed)
    monkeypatch.setattr(pool_mod, "get_pool", _get_pool)
    monkeypatch.setattr(pool_mod, "open_pool", _open_pool)
    ensure_pool()
    assert calls == ["get", "open"]


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("tasks_by_status_wave", {}),
        ("queue_by_state", {}),
        ("recent_attempts_per_executor", {}),
        ("open_monitor_findings", {}),
        ("merged_prs_per_day", {}),
    ],
)
def test_empty_result_sets_are_lists_or_empty_payload(method, kwargs) -> None:
    """Each reader must tolerate an empty catalog without raising."""
    if method == "queue_by_state":
        factory, _ = _factory([[], [], []])
    else:
        factory, _ = _factory([[]])
    result = getattr(FleetReadStore(factory), method)(**kwargs)
    if method == "queue_by_state":
        assert result == {"counts": [], "dead_reasons": [], "attempt_reasons": []}
    else:
        assert result == []
