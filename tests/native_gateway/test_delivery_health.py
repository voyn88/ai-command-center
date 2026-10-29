"""pipeline_health: DB facts → projection block → served snapshot.

The producer reads through `PipelineHealthSource`; these tests use fake
sources (and one scripted fake connection for the Postgres reader's SQL
shape), so no database is needed and the gateway is shown never to touch one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from native_gateway import projection_producer
from native_gateway.config import GatewaySettings
from native_gateway.delivery_health import (
    AUTH_FAILURE_PATTERN,
    PipelineRows,
    PostgresPipelineHealthSource,
    build_pipeline_health,
)
from native_gateway.projection_producer import build_projection, write_projection
from native_gateway.source import FileProjectionSource

from .conftest import auth_headers, fresh_sample
from .test_projection_producer import _seed_root

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _rows(**overrides) -> PipelineRows:
    base = {
        "queue": {
            "ready": 4,
            "claimed": 2,
            "dead": 1,
            "succeeded_last_hour": 9,
            "oldest_ready_age_seconds": 310,
        },
        "lanes": [
            {"role": "aicc_worker", "attempts": 2, "heartbeat_age_seconds": 11},
            {"role": "aicc_worker_2", "attempts": 1, "heartbeat_age_seconds": 40},
        ],
        "auth_failures": 3,
        "backlog_counts": {"OPEN": 5, "DEFER_TO_USER": 2, "DONE": 70, "BOGUS": 1},
        "escalations": [
            {
                "task_id": "VOYN-W0-OWNER-A",
                "title": "Решить   про\nтарифы",
                "priority": "P1",
                "updated_at": "2026-10-30T10:00:00Z",
            },
            {
                "task_id": "VOYN-W0-OWNER-B",
                "title": "x" * 400,
                "priority": None,
                "updated_at": None,
            },
        ],
        "monitor": {
            "open": [
                {"source": "voyn-queue-monitor", "failure": "dead_letter_growth:54>0"},
                {"source": "voyn-infra-monitor", "failure": "active_workers:2<4"},
            ],
            "last_seen_at": "2026-10-30T11:59:00Z",
        },
    }
    base.update(overrides)
    return PipelineRows(**base)


class _FakeSource:
    def __init__(
        self, rows: PipelineRows | None = None, error: Exception | None = None
    ):
        self.rows = rows or _rows()
        self.error = error
        self.reads = 0

    def read(self) -> PipelineRows:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.rows


def test_block_shapes_rows_through_the_allowlist():
    block = build_pipeline_health(_rows(), _NOW)

    assert block["observed_at"] == "2026-10-30T12:00:00Z"
    assert block["queue"] == {
        "ready": 4,
        "claimed": 2,
        "dead": 1,
        "succeeded_last_hour": 9,
        "oldest_ready_age_seconds": 310,
    }
    assert [lane["role"] for lane in block["active_lanes"]] == [
        "aicc_worker",
        "aicc_worker_2",
    ]
    assert block["executor_auth_failures_30m"] == 3
    # Only the closed backlog vocabulary is projected.
    assert block["backlog_by_status"] == {"DEFER_TO_USER": 2, "DONE": 70, "OPEN": 5}
    first, second = block["owner_escalations"]
    assert first["title"] == "Решить про тарифы"  # whitespace collapsed
    assert len(second["title"]) == 160 and second["title"].endswith("…")
    # Monitor: codes only (measurements stripped), never the detail.
    assert block["monitor"] == {
        "verdict": "failing",
        "open_findings": 2,
        "failures": ["active_workers", "dead_letter_growth"],
        "last_seen_at": "2026-10-30T11:59:00Z",
    }
    assert "54>0" not in json.dumps(block)


def test_monitor_is_omitted_when_unavailable_and_ok_when_clear():
    assert "monitor" not in build_pipeline_health(_rows(monitor=None), _NOW)
    clear = build_pipeline_health(
        _rows(monitor={"open": [], "last_seen_at": "2026-10-30T11:00:00Z"}), _NOW
    )
    assert clear["monitor"]["verdict"] == "ok"
    assert clear["monitor"]["open_findings"] == 0


def test_producer_without_source_has_no_block(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    assert "pipeline_health" not in build_projection(root)


def test_producer_adds_block_and_lanes_from_queue(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    source = _FakeSource()
    projection = build_projection(root, pipeline_source=source, run_journal=False)

    assert source.reads == 1
    assert projection["pipeline_health"]["queue"]["ready"] == 4
    # Control plane: no local journal by design → not degraded, and the lanes
    # are the queue's active attempts.
    assert projection["degraded"] is False
    assert projection["lanes"] == [
        {"id": "aicc_worker", "state": "active", "heartbeat_age_seconds": 11},
        {"id": "aicc_worker_2", "state": "active", "heartbeat_age_seconds": 40},
    ]


def test_run_journal_mode_keeps_journal_lanes(tmp_path, monkeypatch):
    """With the journal on (the default) nothing about lanes changes."""
    root = _seed_root(tmp_path, monkeypatch)
    projection = build_projection(root, pipeline_source=_FakeSource())
    assert projection["lanes"] == []
    assert projection["degraded"] is True  # no runtime.db, as before


def test_db_failure_degrades_and_omits_block(tmp_path, monkeypatch, caplog):
    import logging

    root = _seed_root(tmp_path, monkeypatch)
    source = _FakeSource(error=RuntimeError("connection refused"))
    with caplog.at_level(logging.WARNING, logger=projection_producer.__name__):
        projection = build_projection(root, pipeline_source=source, run_journal=False)
    assert "pipeline_health" not in projection
    assert projection["degraded"] is True
    assert projection["tasks"]  # the task list still projects
    assert any("Pipeline health unavailable" in r.message for r in caplog.records)


def test_revision_ignores_the_observation_clock(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    first = build_projection(root, pipeline_source=_FakeSource(), run_journal=False)
    second = build_projection(root, pipeline_source=_FakeSource(), run_journal=False)
    assert first["revision"] == second["revision"]
    changed = build_projection(
        root,
        pipeline_source=_FakeSource(_rows(auth_failures=4)),
        run_journal=False,
    )
    assert changed["revision"] != first["revision"]


def test_round_trip_reaches_the_snapshot_dto(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    out = tmp_path / "artifact/projection.json"
    write_projection(root, out, pipeline_source=_FakeSource(), run_journal=False)

    settings = GatewaySettings(projection_path=out, token_file=tmp_path / "unused")
    health = FileProjectionSource(settings).load().snapshot.pipelineHealth

    assert health is not None
    assert health.queue.oldestReadyAgeSeconds == 310
    assert health.queue.succeededLastHour == 9
    assert [lane.role for lane in health.activeLanes] == [
        "aicc_worker",
        "aicc_worker_2",
    ]
    assert health.executorAuthFailures30m == 3
    assert health.backlogByStatus["DEFER_TO_USER"] == 2
    assert health.ownerEscalations[0].taskID == "VOYN-W0-OWNER-A"
    assert health.ownerEscalations[0].updatedAt == "2026-10-30T10:00:00Z"
    assert health.monitor is not None and health.monitor.verdict == "failing"


def test_snapshot_route_serves_pipeline_health(client, device_token):
    body = client.get("/v1/snapshot", headers=auth_headers(device_token)).json()
    health = body["pipelineHealth"]
    assert health["queue"] == {
        "ready": 3,
        "claimed": 2,
        "dead": 1,
        "succeededLastHour": 7,
        "oldestReadyAgeSeconds": 95,
    }
    assert health["activeLanes"][0]["role"] == "aicc_worker"
    assert health["executorAuthFailures30m"] == 0
    assert health["ownerEscalations"][0]["taskID"] == "VOYN-EXAMPLE-002"
    assert health["monitor"]["failures"] == ["dead_letter_growth"]


def test_snapshot_without_block_serves_null(client, device_token, projection_path):
    data = fresh_sample(projection_path)
    del data["pipeline_health"]
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    body = client.get("/v1/snapshot", headers=auth_headers(device_token)).json()
    assert body["pipelineHealth"] is None


def test_hostile_pipeline_health_is_redacted(client, device_token, projection_path):
    data = fresh_sample(projection_path)
    health = data["pipeline_health"]
    health["owner_escalations"][0]["title"] = "rotate password postgres://a@db/x"
    health["active_lanes"][0]["role"] = "/srv/aicc/.claude"
    health["monitor"]["failures"] = ["token expired for ghp_abcdef123"]
    health["monitor"]["verdict"] = "<script>"
    health["backlog_by_status"]["password"] = 1
    health["queue"]["ready"] = -5
    health["unknown_field"] = "ssh-rsa AAAA"
    projection_path.write_text(json.dumps(data), encoding="utf-8")

    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 200
    body = response.json()["pipelineHealth"]
    assert body["ownerEscalations"][0]["title"] == "[REDACTED]"
    assert body["activeLanes"][0]["role"] == "[REDACTED]"
    assert body["monitor"] is None  # unknown verdict vocabulary → dropped
    assert "password" not in body["backlogByStatus"]
    assert body["queue"]["ready"] == 0
    lowered = response.text.lower()
    for needle in ("password", "postgres://", "ssh-rsa", "ghp_", "/home/"):
        assert needle not in lowered, needle


class _Cursor:
    """Answers each query by a marker substring; records what ran."""

    def __init__(self, answers: dict[str, list[tuple]], log: list, fail_on: str | None):
        self._answers, self._log, self._fail_on = answers, log, fail_on
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._log.append((sql, params))
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("relation does not exist")
        for marker, rows in self._answers.items():
            if marker in sql:
                self._rows = rows
                return
        raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, answers, fail_on=None):
        self.answers, self.fail_on, self.log, self.rolled_back = (
            answers,
            fail_on,
            [],
            False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self.answers, self.log, self.fail_on)

    def rollback(self):
        self.rolled_back = True


_ANSWERS = {
    "FROM work_item_public": [(4, 2, 1, 9, 310.7)],
    "GROUP BY claimed_by_role": [("aicc_worker", 2, 11.2)],
    "outcome_reason ~*": [(3,)],
    "GROUP BY status": [("OPEN", 5), ("DEFER_TO_USER", 1)],
    "status = 'DEFER_TO_USER'": [
        ("VOYN-W0-X", "Решение", "P1", datetime(2026, 10, 30, 9, 0, tzinfo=UTC))
    ],
    "WHERE state = 'open'": [
        (
            "voyn-queue-monitor",
            "dead_letter_growth",
            datetime(2026, 10, 30, 8, 0, tzinfo=UTC),
            datetime(2026, 10, 30, 11, 0, tzinfo=UTC),
        )
    ],
    "max(greatest": [(datetime(2026, 10, 30, 11, 0, tzinfo=UTC),)],
}


def test_postgres_source_reads_only_the_public_views():
    conn = _Conn(_ANSWERS)
    rows = PostgresPipelineHealthSource(lambda: conn).read()

    assert rows.queue["oldest_ready_age_seconds"] == 310
    assert rows.lanes == [
        {"role": "aicc_worker", "attempts": 2, "heartbeat_age_seconds": 11}
    ]
    assert rows.auth_failures == 3
    assert rows.escalations[0]["updated_at"] == "2026-10-30T09:00:00Z"
    assert rows.monitor["open"][0]["failure"] == "dead_letter_growth"
    sql = " ".join(statement for statement, _ in conn.log)
    # The redacted views only — `work_attempt` itself holds claim_token_hash.
    assert "FROM work_attempt " not in sql and "FROM work_item " not in sql
    assert not any(word in sql.upper() for word in ("INSERT", "UPDATE ", "DELETE"))
    assert (AUTH_FAILURE_PATTERN,) in [params for _, params in conn.log]


def test_postgres_source_survives_missing_monitor_table():
    conn = _Conn(_ANSWERS, fail_on="monitor_finding")
    rows = PostgresPipelineHealthSource(lambda: conn).read()
    assert rows.monitor is None
    assert conn.rolled_back
    assert rows.queue["ready"] == 4


def test_cli_flags_wire_the_postgres_source(tmp_path, monkeypatch):
    root = _seed_root(tmp_path, monkeypatch)
    closed = []
    monkeypatch.setattr(
        projection_producer,
        "postgres_source_from_env",
        lambda: (_FakeSource(), lambda: closed.append(True)),
    )
    out = tmp_path / "p.json"
    code = projection_producer.main(
        [
            "--root",
            str(root),
            "--out",
            str(out),
            "--pipeline-health",
            "--no-run-journal",
        ]
    )
    assert code == 0
    written = json.loads(Path(out).read_text(encoding="utf-8"))
    assert written["pipeline_health"]["executor_auth_failures_30m"] == 3
    assert written["degraded"] is False
    assert closed == [True]
