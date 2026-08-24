"""Durable, fail-closed reconciliation for the autonomous delivery lanes.

The planner, reviewer, merger and reaper remain small idempotent ticks.  This
module is the supervisor above them: PostgreSQL says what action is due, who
owns it and when its heartbeat expires; systemd only wakes this code.  Losing a
timer therefore delays a tick until the watchdog restores it, rather than
losing the next action itself.

No generic shell command is accepted from a lane payload.  Risky actions are
capability-bound callables registered by the composition root (most notably
``GUARDED_PUBLISH``); an absent capability is a recorded retry, never an
ambient ``git push`` from the root supervisor.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

__all__ = [
    "Action",
    "ActionOutcome",
    "ControlPlaneConfig",
    "ControlPlaneReport",
    "LaneLeaseGuard",
    "LaneLeaseLost",
    "PostgresControlPlaneStore",
    "Reconciler",
    "SystemdUnitManager",
]


class Action(StrEnum):
    GUARDED_PUBLISH = "GUARDED_PUBLISH"
    CI_WAIT = "CI_WAIT"
    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    ACCEPTANCE = "ACCEPTANCE"
    MERGE = "MERGE"
    DEPLOY = "DEPLOY"
    BACKLOG_SYNC = "BACKLOG_SYNC"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class Lane:
    task_id: str
    action: Action
    owner: str
    payload: dict[str, Any]
    revision: int
    claimant: str = ""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    state: str
    next_action: Action = Action.NONE
    next_owner: str = "control-plane"
    retry_after_seconds: int = 60
    detail: str = ""
    external_blocker: bool = False

    @classmethod
    def succeeded(
        cls, next_action: Action = Action.NONE, *, owner: str = "control-plane"
    ) -> ActionOutcome:
        return cls(
            "DONE" if next_action is Action.NONE else "READY", next_action, owner
        )

    @classmethod
    def waiting(cls, *, seconds: int = 60, detail: str = "") -> ActionOutcome:
        return cls("WAITING", retry_after_seconds=seconds, detail=detail)

    @classmethod
    def retry(cls, detail: str, *, seconds: int = 60) -> ActionOutcome:
        return cls("RETRY", retry_after_seconds=seconds, detail=detail)

    @classmethod
    def deployment_blocked(cls, detail: str) -> ActionOutcome:
        return cls(
            "WAITING",
            Action.DEPLOY,
            "deployer",
            retry_after_seconds=300,
            detail=detail,
            external_blocker=True,
        )


class ControlPlaneStore(Protocol):
    def heartbeat(self, component: str, status: str, detail: str = "") -> None: ...
    def discover_ready_lanes(self, *, now: datetime) -> int: ...
    def recover_stalled(self, *, now: datetime) -> list[tuple[str, str]]: ...
    def claim(
        self, owner: str, *, now: datetime, lease_seconds: int
    ) -> Lane | None: ...
    def lane_heartbeat(
        self, lane: Lane, claimant: str, *, now: datetime, lease_seconds: int
    ) -> bool: ...
    def fenced_effect(
        self,
        lane: Lane,
        claimant: str,
        effect: Callable[[], Any],
        *,
        now: datetime,
        lease_seconds: int,
    ) -> Any: ...
    def finish(self, lane: Lane, outcome: ActionOutcome, *, now: datetime) -> None: ...
    def split_deployment_blocker(
        self, lane: Lane, detail: str, *, now: datetime
    ) -> str: ...
    def component_allows_attempt(self, component: str, *, now: datetime) -> bool: ...
    def component_result(
        self,
        component: str,
        *,
        ok: bool,
        detail: str,
        now: datetime,
        circuit_seconds: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class UnitProbe:
    unit: str
    healthy: bool
    detail: str


class UnitManager(Protocol):
    def ensure_active(self, unit: str, *, dry_run: bool) -> UnitProbe: ...


class SystemdUnitManager:
    """Repair an exact allowlist; never execute names supplied by a DB row."""

    def __init__(
        self,
        allowed_units: tuple[str, ...],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        attempts: int = 2,
    ) -> None:
        self._allowed = frozenset(allowed_units)
        self._runner = runner
        self._attempts = max(1, attempts)

    def _run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return self._runner(
            list(argv), capture_output=True, text=True, check=False, timeout=30
        )

    def _probe(self, unit: str) -> UnitProbe:
        shown = self._run(
            "systemctl",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
        )
        if shown.returncode != 0:
            return UnitProbe(unit, False, f"probe_failed:{shown.stderr.strip()[:160]}")
        values = dict(
            line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line
        )
        healthy = (
            values.get("LoadState") == "loaded"
            and values.get("ActiveState") == "active"
        )
        return UnitProbe(unit, healthy, json.dumps(values, sort_keys=True))

    def ensure_active(self, unit: str, *, dry_run: bool) -> UnitProbe:
        if unit not in self._allowed:
            return UnitProbe(unit, False, "unit_not_allowlisted")
        probe = self._probe(unit)
        if probe.healthy or dry_run:
            return probe
        last = probe
        for attempt in range(self._attempts):
            started = self._run("systemctl", "start", unit)
            if started.returncode != 0:
                last = UnitProbe(
                    unit, False, f"start_failed:{started.stderr.strip()[:160]}"
                )
                continue
            last = self._probe(unit)
            if last.healthy:
                return last
            if attempt + 1 < self._attempts:
                time.sleep(0.1)
        return last


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    owner: str = "aicc-control-plane"
    max_actions_per_tick: int = 8
    lane_lease_seconds: int = 180
    unit_circuit_seconds: int = 600
    desired_units: tuple[str, ...] = (
        "aicc-backlog-planner.timer",
        "aicc-backlog-review.timer",
        "aicc-backlog-merge.timer",
        "aicc-queue-reaper.timer",
    )


@dataclass(slots=True)
class ControlPlaneReport:
    scheduled: int = 0
    recovered: list[tuple[str, str]] = field(default_factory=list)
    units: list[UnitProbe] = field(default_factory=list)
    advanced: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(unit.healthy for unit in self.units) and not self.refused


class LaneLeaseLost(RuntimeError):
    pass


@dataclass(slots=True)
class LaneLeaseGuard:
    """Cooperative fence checked immediately before every side effect."""

    store: ControlPlaneStore
    lane: Lane
    claimant: str
    lease_seconds: int
    clock: Callable[[], datetime]
    lost: threading.Event = field(default_factory=threading.Event)

    def require(self) -> None:
        if self.lost.is_set() or not self.store.lane_heartbeat(
            self.lane,
            self.claimant,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        ):
            self.lost.set()
            raise LaneLeaseLost("lane_lease_lost")

    def effect(self, callback: Callable[[], Any]) -> Any:
        if self.lost.is_set():
            raise LaneLeaseLost("lane_lease_lost")
        try:
            return self.store.fenced_effect(
                self.lane,
                self.claimant,
                callback,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            )
        except LaneLeaseLost:
            self.lost.set()
            raise


ActionHandler = Callable[[Lane, LaneLeaseGuard], ActionOutcome]


class Reconciler:
    def __init__(
        self,
        store: ControlPlaneStore,
        units: UnitManager,
        handlers: Mapping[Action, ActionHandler],
        config: ControlPlaneConfig | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._units = units
        self._handlers = dict(handlers)
        self._config = config or ControlPlaneConfig()
        self._clock = clock

    def _run_handler(self, lane: Lane, handler: ActionHandler) -> ActionOutcome:
        stop_heartbeat = threading.Event()
        guard = LaneLeaseGuard(
            self._store,
            lane,
            self._config.owner,
            self._config.lane_lease_seconds,
            self._clock,
        )

        def beat() -> None:
            interval = max(1.0, self._config.lane_lease_seconds / 3)
            while not stop_heartbeat.wait(interval):
                if not self._store.lane_heartbeat(
                    lane,
                    self._config.owner,
                    now=self._clock(),
                    lease_seconds=self._config.lane_lease_seconds,
                ):
                    guard.lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=beat,
            name=f"control-lane-heartbeat-{lane.task_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            guard.require()
            outcome = handler(lane, guard)
            guard.require()
            return outcome
        except LaneLeaseLost:
            return ActionOutcome.retry("lane_lease_lost", seconds=0)
        except Exception as exc:  # noqa: BLE001 - preserve the durable lane
            return ActionOutcome.retry(f"{type(exc).__name__}:{str(exc)[:240]}")
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)

    def run_once(self, *, dry_run: bool = False) -> ControlPlaneReport:
        now = self._clock()
        report = ControlPlaneReport()
        if not dry_run:
            self._store.heartbeat("reconciler", "RUNNING", "tick")
        try:
            for unit in self._config.desired_units:
                if not self._store.component_allows_attempt(unit, now=now):
                    probe = UnitProbe(unit, False, "circuit_open")
                else:
                    probe = self._units.ensure_active(unit, dry_run=dry_run)
                    if not dry_run:
                        self._store.component_result(
                            unit,
                            ok=probe.healthy,
                            detail=probe.detail,
                            now=now,
                            circuit_seconds=self._config.unit_circuit_seconds,
                        )
                report.units.append(probe)

            if dry_run:
                return report

            report.recovered.extend(self._store.recover_stalled(now=now))
            report.scheduled = self._store.discover_ready_lanes(now=now)

            for _ in range(self._config.max_actions_per_tick):
                now = self._clock()
                lane = self._store.claim(
                    self._config.owner,
                    now=now,
                    lease_seconds=self._config.lane_lease_seconds,
                )
                if lane is None:
                    break
                handler = self._handlers.get(lane.action)
                if handler is None:
                    outcome = ActionOutcome.retry(
                        f"capability_not_configured:{lane.action.value}", seconds=300
                    )
                else:
                    outcome = self._run_handler(lane, handler)
                now = self._clock()
                if outcome.external_blocker:
                    blocker = self._store.split_deployment_blocker(
                        lane, outcome.detail, now=now
                    )
                    outcome = ActionOutcome(
                        outcome.state,
                        outcome.next_action,
                        outcome.next_owner,
                        outcome.retry_after_seconds,
                        f"split_to:{blocker}",
                    )
                self._store.finish(lane, outcome, now=now)
                report.advanced.append((lane.task_id, outcome.state))
        except Exception as exc:  # noqa: BLE001 - report degraded, never advance
            report.refused.append(("control-plane", f"{type(exc).__name__}:{exc}"))
        finally:
            if not dry_run:
                self._store.heartbeat(
                    "reconciler",
                    "HEALTHY" if report.healthy else "DEGRADED",
                    json.dumps(
                        {"advanced": len(report.advanced), "refused": report.refused}
                    ),
                )
        return report


class PostgresControlPlaneStore:
    """Transactional implementation; every state mutation appends an event."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def heartbeat(self, component: str, status: str, detail: str = "") -> None:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_plane_component "
                "(component, owner, desired_state, observed_state, next_action, deadline_at, heartbeat_at, last_error) "
                "VALUES (%s, 'control-plane', 'ACTIVE', %s, 'PROBE', now() + interval '2 minutes', now(), %s) "
                "ON CONFLICT (component) DO UPDATE SET observed_state=EXCLUDED.observed_state, "
                "heartbeat_at=now(), last_error=EXCLUDED.last_error, updated_at=now()",
                (component, status, detail or None),
            )

    def discover_ready_lanes(self, *, now: datetime) -> int:
        """Materialise only evidence-complete transitions; missing facts stay absent.

        A local patch becomes publishable only when commit, tests and independent
        review evidence all exist.  This is intentionally stricter than parsing
        prose in an agent result.
        """
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "WITH facts AS ("
                " SELECT t.task_id, t.status, "
                " (SELECT e.value FROM backlog_evidence e WHERE e.task_id=t.task_id "
                "  AND e.kind='sha' ORDER BY e.evidence_id DESC LIMIT 1) AS head_sha, "
                " (SELECT substring(e.value from 8) FROM backlog_evidence e "
                "  WHERE e.task_id=t.task_id AND e.kind='ci' AND e.value LIKE 'MERGED:%' "
                "  ORDER BY e.evidence_id DESC LIMIT 1) AS merged_sha, "
                " (SELECT substring(e.value from 10) FROM backlog_evidence e "
                "  WHERE e.task_id=t.task_id AND e.kind='ci' AND e.value LIKE 'DEPLOYED:%' "
                "  ORDER BY e.evidence_id DESC LIMIT 1) AS deployed_sha, "
                " EXISTS (SELECT 1 FROM backlog_evidence e WHERE e.task_id=t.task_id "
                "         AND e.kind='pr') AS has_pr "
                " FROM backlog_task t), exact_facts AS ("
                " SELECT f.*, EXISTS (SELECT 1 FROM backlog_evidence e "
                "   WHERE e.task_id=f.task_id AND e.kind='acceptance' "
                "   AND e.value='ACCEPTANCE: ACCEPT ' || f.head_sha) AS accepted "
                " FROM facts f), candidates AS ("
                " SELECT f.task_id, f.head_sha, f.merged_sha, CASE "
                "  WHEN f.status='IN_PROGRESS' AND f.head_sha IS NOT NULL AND NOT f.has_pr "
                "   AND EXISTS (SELECT 1 FROM backlog_evidence e WHERE e.task_id=f.task_id "
                "               AND e.kind='ci' AND e.value='LOCAL_TESTS:PASS:' || f.head_sha) "
                "   AND EXISTS (SELECT 1 FROM backlog_evidence e WHERE e.task_id=f.task_id "
                "               AND e.kind='acceptance' "
                "               AND e.value='INDEPENDENT_REVIEW:PASS:' || f.head_sha) "
                "    THEN 'GUARDED_PUBLISH' "
                "  WHEN f.status='READY_TO_REVIEW' AND f.merged_sha IS NOT NULL "
                "   AND f.deployed_sha=f.merged_sha THEN 'BACKLOG_SYNC' "
                "  WHEN f.status='READY_TO_REVIEW' AND f.merged_sha IS NOT NULL THEN 'DEPLOY' "
                "  WHEN f.status='READY_TO_REVIEW' AND f.has_pr AND NOT f.accepted THEN 'INDEPENDENT_REVIEW' "
                "  WHEN f.status='READY_TO_REVIEW' AND f.has_pr AND f.accepted THEN 'MERGE' END action "
                " FROM exact_facts f) "
                "INSERT INTO control_plane_lane(task_id,next_action,owner,deadline_at,payload) "
                "SELECT task_id, action, CASE WHEN action='GUARDED_PUBLISH' THEN 'guarded-publisher' "
                " WHEN action='INDEPENDENT_REVIEW' THEN 'independent-reviewer' "
                " WHEN action='DEPLOY' THEN 'deployer' "
                " WHEN action='BACKLOG_SYNC' THEN 'control-plane' "
                " ELSE 'merge-controller' END, %s, "
                " jsonb_strip_nulls(jsonb_build_object('head_sha', head_sha, 'merged_sha', merged_sha)) "
                "FROM candidates WHERE action IS NOT NULL "
                "ON CONFLICT (task_id) DO UPDATE SET "
                " next_action=EXCLUDED.next_action,owner=EXCLUDED.owner,deadline_at=EXCLUDED.deadline_at,"
                " payload=EXCLUDED.payload,state='READY',claimant=NULL,lease_expires_at=NULL,attempts=0,"
                " last_error=NULL,revision=control_plane_lane.revision+1,updated_at=now() "
                "WHERE control_plane_lane.state IN ('READY','WAITING','BLOCKED') "
                " AND (control_plane_lane.next_action,control_plane_lane.payload) "
                "     IS DISTINCT FROM (EXCLUDED.next_action,EXCLUDED.payload) "
                "RETURNING task_id",
                (now,),
            )
            rows = cur.fetchall()
            for (task_id,) in rows:
                cur.execute(
                    "INSERT INTO control_plane_event(task_id,component,event,outcome) "
                    "VALUES (%s,'reconciler','schedule','granted')",
                    (task_id,),
                )
            return len(rows)

    def recover_stalled(self, *, now: datetime) -> list[tuple[str, str]]:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, state, attempts, max_attempts, owner FROM control_plane_lane "
                "WHERE (state='RUNNING' AND lease_expires_at <= %s) "
                "   OR (state='WAITING' AND deadline_at <= %s) FOR UPDATE SKIP LOCKED",
                (now, now),
            )
            recovered: list[tuple[str, str]] = []
            for task_id, previous_state, attempts, max_attempts, owner in cur.fetchall():
                attempts += int(previous_state == "RUNNING")
                exhausted = attempts >= max_attempts
                state = "BLOCKED" if exhausted else "READY"
                reason = "attempt_budget_exhausted" if exhausted else "watchdog_requeue"
                cur.execute(
                    "UPDATE control_plane_lane SET state=%s, claimant=NULL, lease_expires_at=NULL, "
                    "deadline_at=%s, attempts=%s, last_error=%s, revision=revision+1, updated_at=now() "
                    "WHERE task_id=%s",
                    (state, now, attempts, reason, task_id),
                )
                cur.execute(
                    "INSERT INTO control_plane_event(task_id,component,event,outcome,detail) "
                    "VALUES (%s,'watchdog','recover',%s,"
                    "jsonb_build_object('reason',%s::text))",
                    (task_id, state.lower(), reason),
                )
                if exhausted:
                    cur.execute(
                        "INSERT INTO control_plane_notification"
                        "(task_id,kind,owner,payload,dedupe_key) VALUES "
                        "(%s,'LANE_STALLED',%s,jsonb_build_object('reason',%s::text),%s) "
                        "ON CONFLICT(dedupe_key) DO NOTHING",
                        (task_id, owner, reason, f"lane-stalled:{task_id}:{attempts}"),
                    )
                recovered.append((task_id, reason))
            return recovered

    def claim(self, owner: str, *, now: datetime, lease_seconds: int) -> Lane | None:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT task_id,next_action,owner,payload,revision FROM control_plane_lane "
                "WHERE state='READY' AND deadline_at <= %s ORDER BY deadline_at,task_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (now,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task_id, action, lane_owner, payload, revision = row
            cur.execute(
                "UPDATE control_plane_lane SET state='RUNNING', claimant=%s, heartbeat_at=%s, "
                "lease_expires_at=%s, revision=revision+1, updated_at=now() WHERE task_id=%s",
                (owner, now, now + timedelta(seconds=lease_seconds), task_id),
            )
            cur.execute(
                "INSERT INTO control_plane_event(task_id,component,event,outcome,detail) "
                "VALUES (%s,'reconciler','claim','granted',"
                "jsonb_build_object('claimant',%s::text))",
                (task_id, owner),
            )
            return Lane(
                task_id,
                Action(action),
                lane_owner,
                payload,
                revision + 1,
                owner,
            )

    def lane_heartbeat(
        self, lane: Lane, claimant: str, *, now: datetime, lease_seconds: int
    ) -> bool:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "UPDATE control_plane_lane SET heartbeat_at=%s,lease_expires_at=%s,updated_at=now() "
                "WHERE task_id=%s AND state='RUNNING' AND claimant=%s AND revision=%s",
                (
                    now,
                    now + timedelta(seconds=lease_seconds),
                    lane.task_id,
                    claimant,
                    lane.revision,
                ),
            )
            ok = cur.rowcount == 1
            if ok:
                cur.execute(
                    "INSERT INTO control_plane_event(task_id,component,event,outcome) "
                    "VALUES (%s,'reconciler','heartbeat','granted')",
                    (lane.task_id,),
                )
            return ok

    def fenced_effect(
        self,
        lane: Lane,
        claimant: str,
        effect: Callable[[], Any],
        *,
        now: datetime,
        lease_seconds: int,
    ) -> Any:
        """Hold the lane row lock across an idempotent external side effect."""
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT state,revision,claimant,lease_expires_at "
                "FROM control_plane_lane WHERE task_id=%s FOR UPDATE",
                (lane.task_id,),
            )
            row = cur.fetchone()
            if (
                row is None
                or row[0] != "RUNNING"
                or row[1] != lane.revision
                or row[2] != claimant
                or row[3] is None
                or row[3] <= now
            ):
                raise LaneLeaseLost("lane_lease_lost")
            result = effect()
            renewed = datetime.now(UTC)
            cur.execute(
                "UPDATE control_plane_lane SET heartbeat_at=%s,lease_expires_at=%s,"
                "updated_at=now() WHERE task_id=%s AND revision=%s AND claimant=%s",
                (
                    renewed,
                    renewed + timedelta(seconds=lease_seconds),
                    lane.task_id,
                    lane.revision,
                    claimant,
                ),
            )
            if cur.rowcount != 1:
                raise LaneLeaseLost("lane_lease_lost")
            return result

    def finish(self, lane: Lane, outcome: ActionOutcome, *, now: datetime) -> None:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT state,revision,attempts,max_attempts,claimant,lease_expires_at "
                "FROM control_plane_lane "
                "WHERE task_id=%s FOR UPDATE",
                (lane.task_id,),
            )
            current = cur.fetchone()
            if (
                current is None
                or current[0] != "RUNNING"
                or current[1] != lane.revision
                or current[4] != lane.claimant
                or current[5] is None
                or current[5] <= now
            ):
                raise RuntimeError("lane_ownership_or_revision_lost")
            attempts = current[2] + (1 if outcome.state == "RETRY" else 0)
            exhausted = attempts >= current[3]
            if exhausted:
                state, next_action = "BLOCKED", lane.action.value
            elif outcome.state in {"RETRY", "WAITING"}:
                state, next_action = "WAITING", lane.action.value
            else:
                state, next_action = outcome.state, outcome.next_action.value
            if (
                lane.action is Action.BACKLOG_SYNC
                and state == "DONE"
                and next_action == Action.NONE.value
            ):
                # Close the canonical task and its durable lane in this same
                # transaction. A crash can therefore leave both uncommitted or
                # both complete, never backlog=DONE with a RUNNING lane whose
                # deployment proof gets lost on lease recovery.
                merged_sha = str(lane.payload.get("merged_sha") or "")
                if not re.fullmatch(r"[0-9a-f]{40}", merged_sha):
                    raise RuntimeError("merged_sha_missing_or_invalid")
                cur.execute(
                    "SELECT revision, EXISTS (SELECT 1 FROM backlog_evidence "
                    "WHERE task_id=%s AND kind='ci' AND value=%s) "
                    "FROM backlog_task WHERE task_id=%s FOR UPDATE",
                    (lane.task_id, f"DEPLOYED:{merged_sha}", lane.task_id),
                )
                task = cur.fetchone()
                if task is None or not task[1]:
                    raise RuntimeError("deployment_evidence_not_durable")
                cur.execute(
                    "SELECT ok,reason FROM backlog_record_evidence(%s,'sha',%s)",
                    (lane.task_id, merged_sha),
                )
                evidence_ok, evidence_reason = cur.fetchone()
                if not evidence_ok:
                    raise RuntimeError(f"merged_sha_evidence:{evidence_reason}")
                cur.execute(
                    "SELECT ok,reason FROM backlog_transition(%s,'DONE',%s)",
                    (lane.task_id, task[0]),
                )
                done, reason = cur.fetchone()
                if not done:
                    raise RuntimeError(f"backlog_done_refused:{reason}")
            cur.execute(
                "UPDATE control_plane_lane SET state=%s,next_action=%s,owner=%s,claimant=NULL,"
                "deadline_at=%s,heartbeat_at=%s,lease_expires_at=NULL,attempts=%s,last_error=%s,"
                "revision=revision+1,updated_at=now() WHERE task_id=%s",
                (
                    state,
                    next_action,
                    outcome.next_owner,
                    now + timedelta(seconds=outcome.retry_after_seconds),
                    now,
                    attempts,
                    outcome.detail or None,
                    lane.task_id,
                ),
            )
            cur.execute(
                "INSERT INTO control_plane_event(task_id,component,event,outcome,detail) "
                "VALUES (%s,'reconciler','finish',%s,"
                "jsonb_build_object('action',%s::text,'detail',%s::text))",
                (lane.task_id, state.lower(), lane.action.value, outcome.detail),
            )

    def split_deployment_blocker(
        self, lane: Lane, detail: str, *, now: datetime
    ) -> str:
        """Create a separate durable task; do not turn a merged change into DEFER."""
        import hashlib

        suffix = hashlib.sha256(detail.encode()).hexdigest()[:10].upper()
        blocker_id = f"{lane.task_id}-DEPLOY-{suffix}"
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT state,revision,claimant,lease_expires_at "
                "FROM control_plane_lane WHERE task_id=%s FOR UPDATE",
                (lane.task_id,),
            )
            control = cur.fetchone()
            if (
                control is None
                or control[0] != "RUNNING"
                or control[1] != lane.revision
                or control[2] != lane.claimant
                or control[3] is None
                or control[3] <= now
            ):
                raise LaneLeaseLost("lane_lease_lost")
            cur.execute(
                "SELECT wave,priority,repo FROM backlog_task WHERE task_id=%s",
                (lane.task_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("source_task_missing")
            wave, priority, repo = row
            cur.execute(
                "SELECT ok,reason,changed FROM backlog_upsert_task(%s,%s,%s,'OPEN','task',%s,%s,%s)",
                (
                    blocker_id,
                    wave,
                    priority,
                    f"Deployment blocker for {lane.task_id}",
                    detail,
                    repo,
                ),
            )
            ok, reason, _changed = cur.fetchone()
            if not ok:
                raise RuntimeError(f"blocker_create_refused:{reason}")
            cur.execute(
                "INSERT INTO control_plane_event(task_id,component,event,outcome,detail) "
                "VALUES (%s,'reconciler','split_blocker','granted',"
                "jsonb_build_object('blocker',%s::text))",
                (lane.task_id, blocker_id),
            )
        return blocker_id

    def component_allows_attempt(self, component: str, *, now: datetime) -> bool:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT circuit_open_until FROM control_plane_component WHERE component=%s",
                (component,),
            )
            row = cur.fetchone()
            return row is None or row[0] is None or row[0] <= now

    def component_result(
        self,
        component: str,
        *,
        ok: bool,
        detail: str,
        now: datetime,
        circuit_seconds: int,
    ) -> None:
        with self._factory() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT consecutive_failures FROM control_plane_component WHERE component=%s FOR UPDATE",
                (component,),
            )
            row = cur.fetchone()
            failures = 0 if ok else ((row[0] if row else 0) + 1)
            circuit = (
                now + timedelta(seconds=circuit_seconds) if failures >= 3 else None
            )
            cur.execute(
                "INSERT INTO control_plane_component(component,owner,desired_state,observed_state,"
                "next_action,deadline_at,heartbeat_at,consecutive_failures,circuit_open_until,last_error) "
                "VALUES (%s,'control-plane','ACTIVE',%s,'PROBE',%s,%s,%s,%s,%s) "
                "ON CONFLICT(component) DO UPDATE SET observed_state=EXCLUDED.observed_state,"
                "deadline_at=EXCLUDED.deadline_at,heartbeat_at=EXCLUDED.heartbeat_at,"
                "consecutive_failures=EXCLUDED.consecutive_failures,"
                "circuit_open_until=EXCLUDED.circuit_open_until,last_error=EXCLUDED.last_error,updated_at=now()",
                (
                    component,
                    "ACTIVE" if ok else "FAILED",
                    now + timedelta(seconds=60),
                    now,
                    failures,
                    circuit,
                    None if ok else detail,
                ),
            )
            cur.execute(
                "INSERT INTO control_plane_event(component,event,outcome,detail) "
                "VALUES (%s,'unit_probe',%s,"
                "jsonb_build_object('detail',%s::text,'failures',%s::integer))",
                (component, "pass" if ok else "fail", detail, failures),
            )
