"""The control-plane reconciler (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE).

Live 2026-08-29: `aicc-backlog-planner.timer` went `inactive (dead)` at
01:54:49 UTC after a 3d3h run -- the journal shows only `Deactivated
successfully`, no service failure, no OOM, no reboot -- and nobody noticed
for 13 hours, because review/merge/self-deploy kept running, PRs kept
merging, hosts kept deploying. The loop looked alive from every OTHER
signal while zero new work was dispatched. `systemctl is-active` catches a
unit that is down; it cannot catch a unit that is up but not doing
anything, and it cannot catch itself going the same way the planner did.

Two independent halves close that gap, and they are deliberately separate
entry points rather than one function with a flag:

* `reconcile_once` runs on the control host, under `aicc_app`. For every
  declared timer it re-asserts "active", restarts it with a bounded,
  backing-off circuit breaker when it is not (never retries forever --
  escalating once is the acceptance, not looping silently), quarantines
  known-sabotaging leftover units (the incident's second cause: an old
  `voyn-aicc-rotate.timer` sending SIGTERM into review jobs), and cross-
  checks each tick's own heartbeat for staleness -- the "timer active but
  tick not working" case `is-active` alone cannot see.
* `check_heartbeats_once` runs on a DIFFERENT host (worker-01), under
  `aicc_worker`, which the shared PostgreSQL role matrix grants read-only
  access to `control_plane_heartbeat` and nothing else
  (`command_center.db.roles._WORKER_CONTROL_PLANE_TABLES`). It does not
  touch systemd at all -- it has no reach to control-01's units, and does
  not need any: the reconciler's OWN tick heartbeats through the same
  table, so a reconciler that silently stopped ticking (exactly the
  planner's failure mode, recursively) shows up as one more stale row,
  caught by a process that cannot die the same way at the same time.

Both are oneshot ticks, not daemons: the reaper's pattern used everywhere
else in this codebase applies here too -- a missed reconcile tick delays
recovery, it never corrupts state, because every mutation
(`systemctl start`, a heartbeat upsert, a circuit-state row) is
idempotent.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "DEFAULT_QUARANTINE_UNITS",
    "DECLARED_TIMERS",
    "DeclaredTimer",
    "ReconcileConfig",
    "ReconcileReport",
    "SubprocessSystemctl",
    "Systemctl",
    "WatchdogReport",
    "check_heartbeats_once",
    "quarantine_units",
    "reconcile_once",
    "record_heartbeat",
]


@dataclass(frozen=True, slots=True)
class DeclaredTimer:
    """One control-plane timer this host must keep active."""

    #: The systemd timer unit, e.g. "aicc-backlog-planner.timer".
    name: str
    #: The paired service unit the timer fires -- derived by convention
    #: (".timer" -> ".service") unless a unit uses the "@" template shape,
    #: which none of the declared set does.
    service: str
    #: The heartbeat row this timer's tick writes, or None for a timer
    #: whose tick is not (yet) heartbeat-instrumented -- `is-active` is
    #: still checked, staleness just is not.
    tick_name: str | None
    #: Expected seconds between successful ticks (the timer's own
    #: OnUnitActiveSec/OnUnitInactiveSec cadence).
    interval_seconds: int
    #: How many missed intervals before a fresh (non-stale) heartbeat is
    #: treated as a real stall rather than ordinary scheduling jitter.
    max_missed_intervals: int = 4


def _timer(
    name: str,
    tick_name: str | None,
    interval_seconds: int,
    max_missed_intervals: int = 4,
) -> DeclaredTimer:
    service = name.removesuffix(".timer") + ".service"
    return DeclaredTimer(
        name=name,
        service=service,
        tick_name=tick_name,
        interval_seconds=interval_seconds,
        max_missed_intervals=max_missed_intervals,
    )


#: Every control-plane timer this host declares -- the set the reconciler
#: keeps active. Heartbeat-instrumented for the four ticks the live incident
#: named directly (dispatch starvation is silent; the loop's other signals
#: stay green regardless of whether these four run at all).
DECLARED_TIMERS: tuple[DeclaredTimer, ...] = (
    _timer("aicc-backlog-planner.timer", "backlog-plan", 60),
    _timer("aicc-backlog-review.timer", "backlog-review", 300),
    _timer("aicc-backlog-merge.timer", "backlog-merge", 300),
    _timer("aicc-queue-reaper.timer", "queue-reap", 60),
    _timer("voyn-aicc-self-deploy.timer", None, 300),
    _timer("voyn-aicc-credential-rotation.timer", None, 1500),
    _timer("aicc-worktree-prune.timer", None, 900),
    # The reconciler watches its own trigger timer too -- the same
    # "systemctl is-active" check every other declared timer gets, so a
    # `aicc-control-reconciler.timer` that silently deactivates the way the
    # planner's did on 2026-08-29 gets restarted by the very next tick that
    # still fires. That does not close the recursive case on its own (a
    # reconciler that stopped ticking cannot restart itself); the heartbeat
    # this tick writes below, checked by `check_heartbeats_once` on a
    # DIFFERENT host, is what closes it.
    _timer("aicc-control-reconciler.timer", "control-reconcile", 120, 5),
)

#: Known-sabotaging leftover units: the incident's second cause was a stale
#: `voyn-aicc-rotate.timer` from before the credential-rotation unit was
#: renamed -- still enabled on the host, periodically restarting workers and
#: sending SIGTERM into review jobs it had no business touching. A unit
#: found here is stopped and disabled, not merely reported: this is a
#: denylist of names known to be dangerous, not a heuristic over unknown
#: units, so quarantining it cannot take down a legitimate future addition
#: that simply has not been added to `DECLARED_TIMERS` yet.
#:
#: Override/extend with a comma-separated `AICC_RECONCILER_QUARANTINE_UNITS`
#: env var for a host carrying different leftover drift.
DEFAULT_QUARANTINE_UNITS: tuple[str, ...] = (
    "voyn-aicc-rotate.timer",
    "voyn-aicc-rotate.service",
)


def quarantine_units() -> tuple[str, ...]:
    raw = os.environ.get("AICC_RECONCILER_QUARANTINE_UNITS", "")
    if not raw.strip():
        return DEFAULT_QUARANTINE_UNITS
    extra = tuple(name.strip() for name in raw.split(",") if name.strip())
    # Additive, not a replacement: an operator naming one more sabotaging
    # unit must not silently stop watching for the ones already known.
    return tuple(dict.fromkeys(DEFAULT_QUARANTINE_UNITS + extra))


@dataclass(frozen=True, slots=True)
class ReconcileConfig:
    #: Consecutive failed recovery attempts before the circuit opens and the
    #: reconciler stops auto-retrying that unit -- "escalating only after
    #: safe automatic recovery is exhausted", not looping forever.
    circuit_failure_threshold: int = 3
    #: Backoff base; doubled per consecutive failure up to `max_cooldown`.
    base_cooldown_seconds: int = 30
    max_cooldown_seconds: int = 900
    command_timeout: int = 30


@dataclass(slots=True)
class ReconcileReport:
    ok: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    circuit_open_skipped: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    #: (unit_or_tick, reason) -- surfaced non-zero from the CLI so
    #: `OnFailure=` fires the owner-visible alert unit.
    escalated: list[tuple[str, str]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.escalated


class Systemctl(Protocol):
    def is_active(self, unit: str) -> bool: ...
    def is_enabled(self, unit: str) -> bool: ...
    def start(self, unit: str) -> bool: ...
    def stop(self, unit: str) -> bool: ...
    def disable(self, unit: str) -> bool: ...


class SubprocessSystemctl:
    """Passwordless-sudo systemctl -- the exact grant self_deploy.py already
    relies on (`sudo -n`: never prompt; a missing grant is a refusal, not a
    hang, so a misconfigured host fails the tick loudly instead of hanging
    the reconciler itself)."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["sudo", "-n", "systemctl", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args, 124, "", "timed out")

    def is_active(self, unit: str) -> bool:
        return self._run("is-active", unit).stdout.strip() == "active"

    def is_enabled(self, unit: str) -> bool:
        return self._run("is-enabled", unit).stdout.strip() in (
            "enabled",
            "enabled-runtime",
            "static",
        )

    def start(self, unit: str) -> bool:
        return self._run("start", unit).returncode == 0

    def stop(self, unit: str) -> bool:
        return self._run("stop", unit).returncode == 0

    def disable(self, unit: str) -> bool:
        return self._run("disable", unit).returncode == 0


def _now(now: _dt.datetime | None) -> _dt.datetime:
    return now if now is not None else _dt.datetime.now(_dt.UTC)


def _upsert_heartbeat(
    conn: Any, tick_name: str, detail: str, now: _dt.datetime
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_heartbeat "
            "(tick_name, last_ok_at, detail, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tick_name) DO UPDATE SET "
            "last_ok_at = excluded.last_ok_at, "
            "detail = excluded.detail, "
            "updated_at = excluded.updated_at",
            (tick_name, now, detail, now),
        )


def record_heartbeat(
    connection_factory: Any,
    tick_name: str,
    *,
    detail: str = "",
    now: _dt.datetime | None = None,
) -> None:
    """Upsert `control_plane_heartbeat` -- called by the CLI after a tick
    completes, regardless of whether the tick found any work. A tick that
    keeps succeeding with nothing to do still advances `last_ok_at`; only a
    tick that stopped running at all goes stale, which is exactly the
    distinction the live incident showed `is-active` cannot draw on its
    own."""
    moment = _now(now)
    with connection_factory() as conn:
        _upsert_heartbeat(conn, tick_name, detail, moment)


def _load_unit_state(
    conn: Any, unit_name: str
) -> tuple[int, _dt.datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consecutive_failures, circuit_open_until "
            "FROM control_plane_unit_state WHERE unit_name = %s",
            (unit_name,),
        )
        row = cur.fetchone()
    if row is None:
        return 0, None
    return row[0], row[1]


def _save_unit_state(
    conn: Any,
    unit_name: str,
    *,
    consecutive_failures: int,
    circuit_open_until: _dt.datetime | None,
    last_action: str,
    last_outcome: str,
    now: _dt.datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_unit_state "
            "(unit_name, consecutive_failures, circuit_open_until, "
            " last_action, last_outcome, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (unit_name) DO UPDATE SET "
            "consecutive_failures = excluded.consecutive_failures, "
            "circuit_open_until = excluded.circuit_open_until, "
            "last_action = excluded.last_action, "
            "last_outcome = excluded.last_outcome, "
            "updated_at = excluded.updated_at",
            (
                unit_name,
                consecutive_failures,
                circuit_open_until,
                last_action,
                last_outcome,
                now,
            ),
        )


def _record_event(
    conn: Any, unit_name: str, action: str, outcome: str, detail: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_event (unit_name, action, outcome, detail) "
            "VALUES (%s, %s, %s, %s)",
            (unit_name, action, outcome, detail),
        )


def _backoff_seconds(cfg: ReconcileConfig, consecutive_failures: int) -> int:
    return min(
        cfg.max_cooldown_seconds,
        cfg.base_cooldown_seconds * (2 ** max(0, consecutive_failures - 1)),
    )


def _recover_unit(
    systemctl: Systemctl,
    conn: Any,
    *,
    unit_name: str,
    action: str,
    do_recover: Any,
    cfg: ReconcileConfig,
    now: _dt.datetime,
    report: ReconcileReport,
    ok_bucket: list[str],
    retry_bucket: list[str],
) -> None:
    """One bounded-backoff circuit-breaker attempt: retry until
    `circuit_failure_threshold` consecutive failures, then stop retrying and
    escalate once -- never a silent infinite retry loop, never a silent
    stall either."""
    failures, open_until = _load_unit_state(conn, unit_name)
    if open_until is not None and now < open_until:
        report.circuit_open_skipped.append(unit_name)
        return

    recovered = do_recover()
    if recovered:
        if failures or open_until is not None:
            _record_event(conn, unit_name, action, "recovered", "")
        _save_unit_state(
            conn,
            unit_name,
            consecutive_failures=0,
            circuit_open_until=None,
            last_action=action,
            last_outcome="ok",
            now=now,
        )
        ok_bucket.append(unit_name)
        return

    failures += 1
    if failures >= cfg.circuit_failure_threshold:
        # Circuit opens: stop auto-retrying this unit for the cooldown
        # window and escalate once, rather than hammering a unit that
        # cannot be fixed by restarting it again.
        cooldown = _backoff_seconds(cfg, failures)
        _save_unit_state(
            conn,
            unit_name,
            consecutive_failures=failures,
            circuit_open_until=now + _dt.timedelta(seconds=cooldown),
            last_action=action,
            last_outcome="circuit_open",
            now=now,
        )
        _record_event(
            conn,
            unit_name,
            action,
            "circuit_open",
            f"{failures} consecutive failures; cooldown {cooldown}s",
        )
        report.escalated.append(
            (unit_name, f"{action}_failed_{failures}_times_circuit_open")
        )
        return

    cooldown = _backoff_seconds(cfg, failures)
    _save_unit_state(
        conn,
        unit_name,
        consecutive_failures=failures,
        circuit_open_until=now + _dt.timedelta(seconds=cooldown),
        last_action=action,
        last_outcome="failed",
        now=now,
    )
    _record_event(
        conn, unit_name, action, "failed", f"attempt {failures}, retry in {cooldown}s"
    )
    retry_bucket.append(unit_name)


def reconcile_once(
    systemctl: Systemctl,
    connection_factory: Any,
    *,
    declared: tuple[DeclaredTimer, ...] = DECLARED_TIMERS,
    quarantine: tuple[str, ...] | None = None,
    config: ReconcileConfig = ReconcileConfig(),
    now: _dt.datetime | None = None,
) -> ReconcileReport:
    report = ReconcileReport()
    moment = _now(now)
    quarantine = quarantine if quarantine is not None else quarantine_units()

    with connection_factory() as conn:
        for unit in quarantine:
            if systemctl.is_active(unit) or systemctl.is_enabled(unit):
                systemctl.stop(unit)
                systemctl.disable(unit)
                still_active = systemctl.is_active(unit)
                _record_event(
                    conn,
                    unit,
                    "quarantine",
                    "quarantined" if not still_active else "quarantine_incomplete",
                    "",
                )
                report.quarantined.append(unit)
                if still_active:
                    report.escalated.append((unit, "quarantine_failed_still_active"))

        for timer in declared:
            active = systemctl.is_active(timer.name)
            if not active:
                _retry_bucket: list[str] = []
                _recover_unit(
                    systemctl,
                    conn,
                    unit_name=timer.name,
                    action="restart_timer",
                    do_recover=lambda t=timer: systemctl.start(t.name)
                    and systemctl.is_active(t.name),
                    cfg=config,
                    now=moment,
                    report=report,
                    ok_bucket=report.restarted,
                    retry_bucket=_retry_bucket,
                )
                continue

            report.ok.append(timer.name)
            if timer.tick_name is None:
                continue

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_ok_at FROM control_plane_heartbeat "
                    "WHERE tick_name = %s",
                    (timer.tick_name,),
                )
                row = cur.fetchone()
            stale_after = _dt.timedelta(
                seconds=timer.interval_seconds * timer.max_missed_intervals
            )
            last_ok_at = row[0] if row else None
            is_stale = last_ok_at is None or (moment - last_ok_at) > stale_after
            if not is_stale:
                continue

            # The timer reports active but the tick it fires has not
            # succeeded within its SLO -- exactly the failure class an
            # `is-active` check alone cannot see. One bounded restart of the
            # SERVICE (not the timer, which is already active) before this
            # escalates through the same circuit breaker.
            _retry_bucket = []
            _recover_unit(
                systemctl,
                conn,
                unit_name=f"{timer.name}#heartbeat",
                action="restart_service_stale_heartbeat",
                do_recover=lambda t=timer: systemctl.start(t.service),
                cfg=config,
                now=moment,
                report=report,
                ok_bucket=report.restarted,
                retry_bucket=_retry_bucket,
            )

        # The reconciler's own liveness signal: completing this pass IS what
        # "control-reconcile" succeeding means, independent of whether any
        # individual unit needed recovery -- a tick that found and handled a
        # problem is a working tick, not a failing one.
        _upsert_heartbeat(
            conn,
            "control-reconcile",
            f"restarted={len(report.restarted)} "
            f"quarantined={len(report.quarantined)} "
            f"escalated={len(report.escalated)}",
            moment,
        )

    return report


@dataclass(slots=True)
class WatchdogReport:
    ok: list[str] = field(default_factory=list)
    #: (tick_name, age_seconds) for heartbeats that are missing or stale.
    stale: list[tuple[str, float]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.stale


def check_heartbeats_once(
    connection_factory: Any,
    *,
    declared: tuple[DeclaredTimer, ...] = DECLARED_TIMERS,
    now: _dt.datetime | None = None,
) -> WatchdogReport:
    """The independent cross-check (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE's
    second acceptance clause): read-only, runs on a DIFFERENT host under
    `aicc_worker`, touches no systemd unit at all. It cannot restart
    anything on control-01 -- it has no reach to -- so its whole job is to
    notice staleness and let the CLI's exit code drive an owner-visible
    alert. A reconciler that silently stopped ticking shows up here as one
    more stale tick_name, the same signal every other stalled tick
    produces."""
    report = WatchdogReport()
    moment = _now(now)
    with connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tick_name, last_ok_at FROM control_plane_heartbeat")
            rows = dict(cur.fetchall())

    for timer in declared:
        if timer.tick_name is None:
            continue
        last_ok_at = rows.get(timer.tick_name)
        stale_after = timer.interval_seconds * timer.max_missed_intervals
        if last_ok_at is None:
            report.stale.append((timer.tick_name, float("inf")))
            continue
        age = (moment - last_ok_at).total_seconds()
        if age > stale_after:
            report.stale.append((timer.tick_name, age))
        else:
            report.ok.append(timer.tick_name)

    return report
