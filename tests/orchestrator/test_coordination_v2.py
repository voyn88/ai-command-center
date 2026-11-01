"""Coordination v2: durable economical Grok↔Cursor advisor (unit, mocked)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from command_center.orchestrator.coordination import (
    apply_advisory_order,
    build_candidate_snapshot,
    parse_coordination_result,
)
from command_center.orchestrator.coordination import (
    coordinate_once as _coordinate_once,
)


def coordinate_once(factory, enqueue, *args, **kwargs):
    factory.__self__.enqueue_callback = enqueue
    return _coordinate_once(factory, *args, **kwargs)


def test_unchanged_snapshot_skips_model_call():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": ("pr",),
            "title": "a",
        }
    ]
    snap, _compact, _blocker = build_candidate_snapshot(rows)
    store = _FakeCoordinatorStore(
        snapshot_hash=snap, blocker_hash="other", last_status="applied"
    )
    enqueued: list[Any] = []

    report = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki_x",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )

    assert report.skipped == "unchanged_snapshot"
    assert enqueued == []
    assert store.begin_calls == []


def test_stale_and_invented_ids_fail_closed():
    eligible = {"VOYN-A", "VOYN-B"}
    ordered, err = parse_coordination_result(
        '{"ordered_task_ids": ["VOYN-A", "VOYN-INVENTED"]}', eligible
    )
    assert ordered is None and err and err.startswith("invented_ids:")

    ordered, err = parse_coordination_result(
        '{"ordered_task_ids": ["VOYN-A"], "mark_done": true}', eligible
    )
    assert ordered is None and err == "forbidden_key:mark_done"

    # Stale id relative to *current* eligible set is invented for this tick.
    ordered, err = parse_coordination_result(
        '{"ordered_task_ids": ["VOYN-A", "VOYN-STALE"]}', {"VOYN-A"}
    )
    assert ordered is None and err and "VOYN-STALE" in err


def test_malformed_result_is_rejected():
    eligible = {"VOYN-A"}
    assert parse_coordination_result("not json", eligible)[0] is None
    assert parse_coordination_result("{", eligible)[0] is None
    assert parse_coordination_result('{"ordered_task_ids": "VOYN-A"}', eligible)[0] is None
    assert parse_coordination_result('{"ordered_task_ids": [1]}', eligible)[0] is None
    assert parse_coordination_result(
        '{"ordered_task_ids": ["VOYN-A", "VOYN-A"]}', eligible
    )[0] is None


def test_concurrent_and_restart_dedup_keeps_single_inflight():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "pipeline",
            "evidence_kinds": (),
            "title": "a",
        }
    ]
    store = _FakeCoordinatorStore()
    enqueued: list[Any] = []

    first = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki_1",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert first.enqueued == "wki_1"
    assert store.state["in_flight_key"]
    assert enqueued[0][1] == f"coord:v2:{first.snapshot_hash}"

    # Restart / concurrent tick: in-flight pending → no second model enqueue.
    second = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki_2",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert second.skipped == "inflight_pending"
    assert len(enqueued) == 1


def test_apply_prior_result_then_unchanged_snapshot_skips():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": (),
            "title": "a",
        },
        {
            "task_id": "VOYN-B",
            "wave": "1",
            "priority": "P2",
            "task_class": "functional",
            "evidence_kinds": (),
            "title": "b",
        },
    ]
    snap, _compact, blocker = build_candidate_snapshot(rows)
    key = f"coord:v2:{snap}"
    # Real cas_begin already persisted the snapshot before enqueue.
    store = _FakeCoordinatorStore(
        snapshot_hash=snap,
        blocker_hash=blocker,
        in_flight_key=key,
        last_status="queued",
    )
    store.work_items[key] = (
        "succeeded",
        {"result_text": '{"ordered_task_ids": ["VOYN-B", "VOYN-A"]}'},
    )
    enqueued: list[Any] = []

    applied = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki_x",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert applied.applied == ["VOYN-B", "VOYN-A"]
    assert store.state["advisory_order"] == ["VOYN-B", "VOYN-A"]
    assert store.state["in_flight_key"] is None
    assert enqueued == []

    again = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki_y",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert again.skipped == "unchanged_snapshot"
    assert enqueued == []


def test_malformed_inflight_result_sets_cooldown_and_stop_switch():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": (),
            "title": "a",
        }
    ]
    snap, _, _ = build_candidate_snapshot(rows)
    key = f"coord:v2:{snap}"
    store = _FakeCoordinatorStore(
        snapshot_hash="prev", in_flight_key=key, last_status="queued"
    )
    store.work_items[key] = ("succeeded", {"result_text": "PARTIAL nonsense"})

    rejected = coordinate_once(
        store.factory,
        lambda *a: "wki",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert rejected.rejected == "no_json_object"
    assert store.state["last_status"] == "rejected"
    assert store.state["cooldown_until"] is not None

    stopped = coordinate_once(
        store.factory,
        lambda *a: "wki",
        environ={"AICC_COORDINATOR_V2": "1", "AICC_COORDINATOR_V2_STOP": "1"},
        load_candidates=lambda: rows,
    )
    assert stopped.stopped and stopped.skipped == "stop_switch"

    disabled = coordinate_once(
        store.factory,
        lambda *a: "wki",
        environ={},
        load_candidates=lambda: rows,
    )
    assert disabled.skipped == "disabled"


def test_unchanged_blocker_after_reject_skips():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": (),
            "title": "a",
        }
    ]
    snap, _, blocker = build_candidate_snapshot(rows)
    # Different snapshot than stored, but same blocker after reject → skip.
    store = _FakeCoordinatorStore(
        snapshot_hash="different",
        blocker_hash=blocker,
        last_status="rejected",
    )
    enqueued: list[Any] = []
    report = coordinate_once(
        store.factory,
        lambda *a: enqueued.append(a) or "wki",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert report.skipped == "unchanged_blocker"
    assert enqueued == []
    assert snap != "different"


def test_advisory_order_ignores_stale_ids_and_preserves_rest():
    candidates = [("VOYN-A",), ("VOYN-B",), ("VOYN-C",)]
    ordered = apply_advisory_order(
        candidates, ["VOYN-C", "VOYN-GONE", "VOYN-A"], id_index=0
    )
    assert ordered == [("VOYN-C",), ("VOYN-A",), ("VOYN-B",)]


def test_fallback_when_enqueue_fails_clears_inflight():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": (),
            "title": "a",
        }
    ]
    store = _FakeCoordinatorStore()

    def boom(*_a):
        raise RuntimeError("no queue")

    report = coordinate_once(
        store.factory,
        boom,
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert report.rejected == "enqueue_failed:RuntimeError"
    assert store.state["in_flight_key"] is None


def test_prompt_excludes_bodies_and_uses_grok_46_model_only():
    rows = [
        {
            "task_id": "VOYN-A",
            "wave": "1",
            "priority": "P1",
            "task_class": "functional",
            "evidence_kinds": ("sha",),
            "title": "short",
            "body": "SECRET_TOKEN=should-never-appear",
        }
    ]
    store = _FakeCoordinatorStore()
    captured: list[Any] = []

    coordinate_once(
        store.factory,
        lambda *a: captured.append(a) or "wki",
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: rows,
    )
    assert captured
    _queue, _key, payload, _task, attempts = captured[0]
    assert attempts == 1
    assert "SECRET_TOKEN" not in payload["prompt"]
    assert payload["cascade"] == [
        {
            "executor": "grok",
            "task_type": "independent_review",
            "model": "grok-4.6",
            "capability": "model_only",
        }
    ]
    assert payload["mode"] == "coordination_v2"
    assert "backlog_task_id" not in payload


def test_migration_0032_is_paired_and_registers_table():
    from pathlib import Path

    from command_center.db import migrations, roles

    sql_dir = Path(migrations.SQL_DIR)
    assert (sql_dir / "0032_coordinator_v2.up.sql").is_file()
    assert (sql_dir / "0032_coordinator_v2.down.sql").is_file()
    assert "coordinator_state" in roles.ALL_TABLES
    up = (sql_dir / "0032_coordinator_v2.up.sql").read_text(encoding="utf-8")
    assert "coordinator_cas_begin" in up
    assert "CREATE TABLE coordinator_state" in up
    found = migrations.discover()
    assert found[-1].slug == "coordinator_v2"
    assert found[-1].version == 32


# ---------------------------------------------------------------------------
# Fake durable state — mirrors the CAS semantics without PostgreSQL.
# ---------------------------------------------------------------------------


class _FakeCoordinatorStore:
    def __init__(
        self,
        *,
        snapshot_hash: str = "",
        blocker_hash: str = "",
        advisory_order: list[str] | None = None,
        in_flight_key: str | None = None,
        last_status: str = "idle",
        cooldown_until: datetime | None = None,
    ) -> None:
        self.state: dict[str, Any] = {
            "snapshot_hash": snapshot_hash,
            "blocker_hash": blocker_hash,
            "advisory_order": list(advisory_order or []),
            "in_flight_key": in_flight_key,
            "in_flight_at": datetime.now(UTC) if in_flight_key else None,
            "last_result_hash": "",
            "last_status": last_status,
            "cooldown_until": cooldown_until,
            "detail": {},
        }
        self.work_items: dict[str, tuple[str, Any]] = {}
        self.begin_calls: list[tuple] = []
        if in_flight_key:
            # Default pending unless the test overrides work_items.
            self.work_items.setdefault(in_flight_key, ("ready", None))

    @contextmanager
    def factory(self):
        yield self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._sql = " ".join(sql.split())
        self._params = params
        self._result: list[tuple] = []
        self.description = ("ok",)

        if "coordinator_enqueue" in self._sql:
            name, snap, blocker, key, queue, payload = params
            self.execute("SELECT coordinator_cas_begin", (name, snap, blocker, key))
            if not self._result[0][0]:
                self._result = [(None, "cas_begin_refused")]
                return
            try:
                item = self.enqueue_callback(queue, key, json.loads(payload), "VOYN-AICC-COORD-V2", 1)
            except RuntimeError:
                self.work_items.pop(key, None)
                self.state.update(in_flight_key=None, last_status="enqueue_failed",
                                  cooldown_until=datetime.now(UTC) + timedelta(seconds=300))
                self._result = [(None, "enqueue_failed:RuntimeError")]
            else:
                self._result = [(item, None)]
            return
        if "coordinator_state_read" in self._sql:
            s = self.state
            self._result = [
                (
                    s["snapshot_hash"],
                    s["blocker_hash"],
                    json.dumps(s["advisory_order"]),
                    s["in_flight_key"],
                    s["in_flight_at"],
                    s["last_result_hash"],
                    s["last_status"],
                    s["cooldown_until"],
                    json.dumps(s["detail"]),
                )
            ]
            return
        if "coordinator_cas_begin" in self._sql:
            name, snap, blocker, key = params[:4]
            self.begin_calls.append(params)
            s = self.state
            ok = True
            if s["in_flight_key"] is not None or s["cooldown_until"] and s["cooldown_until"] > datetime.now(UTC) or s["snapshot_hash"] == snap or (
                blocker
                and s["blocker_hash"] == blocker
                and s["last_status"] in ("rejected", "cooldown")
            ):
                ok = False
            if ok:
                s.update(
                    {
                        "snapshot_hash": snap,
                        "blocker_hash": blocker or "",
                        "in_flight_key": key,
                        "in_flight_at": datetime.now(UTC),
                        "last_status": "queued",
                    }
                )
                self.work_items.setdefault(key, ("ready", None))
            self._result = [(ok,)]
            return
        if "coordinator_cas_finish" in self._sql:
            name, key, status = params[0], params[1], params[2]
            s = self.state
            if s["in_flight_key"] != key:
                self._result = [(False,)]
                return
            advisory = s["advisory_order"]
            # Applied binds order as params[3]; rejected embeds NULL in SQL so
            # params[3] is already result_hash.
            if status == "applied":
                advisory = json.loads(params[3])
                result_hash = params[4]
                detail = json.loads(params[5])
                cooldown_seconds = int(params[6] or 0)
            else:
                result_hash = params[3] if len(params) > 3 else ""
                detail = json.loads(params[4]) if len(params) > 4 and params[4] else {}
                cooldown_seconds = int(params[5] or 0) if len(params) > 5 else 0
            cooldown = None
            if cooldown_seconds > 0:
                cooldown = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
            s.update(
                {
                    "in_flight_key": None,
                    "in_flight_at": None,
                    "advisory_order": advisory,
                    "last_result_hash": result_hash or "",
                    "last_status": status,
                    "cooldown_until": cooldown,
                    "detail": detail,
                }
            )
            self._result = [(True,)]
            return
        if "coordinator_cas_clear_inflight" in self._sql:
            _name, key, _reason = params[0], params[1], params[2]
            if self.state["in_flight_key"] == key:
                self.state["in_flight_key"] = None
                self.state["in_flight_at"] = None
                self.state["last_status"] = "idle"
                self._result = [(True,)]
            else:
                self._result = [(False,)]
            return
        if "FROM work_item" in self._sql:
            _queue, key = params[0], params[1]
            item = self.work_items.get(key)
            if item is None:
                self._result = []
                self.description = None
                return
            state, payload = item
            self._result = [(state, json.dumps(payload) if payload is not None else None)]
            return
        raise AssertionError(f"unexpected SQL in fake store: {self._sql}")

    def fetchall(self) -> list[tuple]:
        return list(self._result)

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None


@pytest.mark.parametrize(
    "text,eligible,ok",
    [
        ('{"ordered_task_ids": []}', {"VOYN-A"}, True),
        ('Here:\n{"ordered_task_ids": ["VOYN-A"]}\n', {"VOYN-A"}, True),
        ('{"ordered_task_ids": ["VOYN-A"], "create_tasks": []}', {"VOYN-A"}, False),
    ],
)
def test_parse_coordination_result_parametrized(text, eligible, ok):
    ordered, err = parse_coordination_result(text, eligible)
    if ok:
        assert err is None and ordered is not None
    else:
        assert ordered is None and err
