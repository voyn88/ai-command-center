from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from command_center.orchestrator.control_plane import (
    Action,
    ActionOutcome,
    ControlPlaneConfig,
    HttpsNotificationAdapter,
    Lane,
    LaneLeaseLost,
    Reconciler,
    SystemdUnitManager,
    UnitProbe,
)
from command_center.orchestrator.desired_state import load

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


class FakeStore:
    def __init__(self, lanes=()):
        self.lanes = list(lanes)
        self.heartbeats = []
        self.finished = []
        self.split = []
        self.recovered = [("VOYN-STALLED", "watchdog_requeue")]
        self.scheduled = 1
        self.components = []

    def heartbeat(self, component, status, detail=""):
        self.heartbeats.append((component, status, detail))

    def discover_ready_lanes(self, *, now):
        return self.scheduled

    def recover_stalled(self, *, now):
        return self.recovered

    def claim(self, owner, capabilities, *, now, lease_seconds):
        for index, lane in enumerate(self.lanes):
            if lane.action in capabilities:
                return self.lanes.pop(index)
        return None

    def lane_heartbeat(self, lane, claimant, *, now, lease_seconds):
        return True

    def lane_progress(self, lane, claimant, token, *, now):
        return True

    def fenced_effect(self, lane, claimant, effect, *, now, lease_seconds):
        if not self.lane_heartbeat(
            lane, claimant, now=now, lease_seconds=lease_seconds
        ):
            raise LaneLeaseLost("lane_lease_lost")
        return effect()

    def finish(self, lane, outcome, *, now):
        self.finished.append((lane, outcome))

    def split_deployment_blocker(self, lane, detail, *, now):
        self.split.append((lane.task_id, detail))
        return f"{lane.task_id}-DEPLOY-BLOCKER"

    def advance_delivery(self, lane, stage, detail):
        return True

    def component_allows_attempt(self, component, *, now):
        return True

    def component_result(self, component, *, ok, detail, now, circuit_seconds):
        self.components.append((component, ok, detail))


class FakeUnits:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.calls = []

    def ensure_active(self, unit, *, dry_run):
        self.calls.append((unit, dry_run))
        return UnitProbe(unit, self.healthy, "active" if self.healthy else "inactive")


def config(*units):
    return ControlPlaneConfig(
        desired_units=tuple(units) or ("aicc-test.timer",), max_actions_per_tick=8
    )


def test_default_supervision_covers_each_control_service_timer_and_deadman():
    registry = load(
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "config"
        / "aicc-desired-state.json"
    )
    units = set(registry.control.units)
    for component in (
        "aicc-backlog-planner",
        "aicc-backlog-review",
        "aicc-backlog-merge",
        "aicc-queue-reaper",
        "aicc-control-plane-reconciler",
        "aicc-control-plane-notify",
        "aicc-control-plane-watchdog",
    ):
        assert {f"{component}.service", f"{component}.timer"} <= units


def test_reconciler_refuses_an_empty_or_duplicate_desired_registry():
    with pytest.raises(ValueError, match="desired unit registry"):
        Reconciler(FakeStore(), FakeUnits(), {}, ControlPlaneConfig())
    with pytest.raises(ValueError, match="desired unit registry"):
        Reconciler(
            FakeStore(),
            FakeUnits(),
            {},
            ControlPlaneConfig(desired_units=("same.timer", "same.timer")),
        )


def test_ready_lane_advances_without_waiting_for_an_operator_question():
    lane = Lane("VOYN-READY", Action.GUARDED_PUBLISH, "guarded-publisher", {}, 2)
    store = FakeStore([lane])
    units = FakeUnits()
    reconciler = Reconciler(
        store,
        units,
        {
            Action.GUARDED_PUBLISH: lambda _lane, _guard: ActionOutcome.succeeded(
                Action.CI_WAIT
            )
        },
        config("aicc-backlog-review.timer"),
        clock=lambda: NOW,
    )

    report = reconciler.run_once()

    assert report.scheduled == 1
    assert report.recovered == [("VOYN-STALLED", "watchdog_requeue")]
    assert report.advanced == [("VOYN-READY", "READY")]
    assert store.finished[0][1].next_action is Action.CI_WAIT
    assert store.heartbeats[0][:2] == ("reconciler", "RUNNING")
    assert store.heartbeats[-1][:2] == ("reconciler", "HEALTHY")


def test_missing_guarded_publisher_capability_does_not_claim_or_burn_attempt():
    lane = Lane("VOYN-READY", Action.GUARDED_PUBLISH, "guarded-publisher", {}, 2)
    store = FakeStore([lane])

    Reconciler(store, FakeUnits(), {}, config(), clock=lambda: NOW).run_once()

    assert store.finished == []
    assert store.lanes == [lane]


def test_unrelated_deployment_failure_becomes_a_separate_task():
    lane = Lane("VOYN-MERGED", Action.DEPLOY, "deployer", {}, 4)
    store = FakeStore([lane])
    reconciler = Reconciler(
        store,
        FakeUnits(),
        {
            Action.DEPLOY: lambda _lane, _guard: ActionOutcome.deployment_blocked(
                "ssh access missing"
            )
        },
        config(),
        clock=lambda: NOW,
    )

    reconciler.run_once()

    assert store.split == [("VOYN-MERGED", "ssh access missing")]
    outcome = store.finished[0][1]
    assert outcome.state == "WAITING"
    assert outcome.next_action is Action.DEPLOY
    assert outcome.detail == "split_to:VOYN-MERGED-DEPLOY-BLOCKER"


def test_dry_run_is_strictly_read_only():
    store = FakeStore()
    units = FakeUnits(healthy=False)
    report = Reconciler(
        store, units, {}, config("aicc-backlog-review.timer"), clock=lambda: NOW
    ).run_once(dry_run=True)

    assert not report.healthy
    assert store.heartbeats == []
    assert store.components == []
    assert store.finished == []
    assert store.lanes == []
    assert units.calls == [("aicc-backlog-review.timer", True)]


def test_lost_lease_fences_handler_before_external_side_effect():
    lane = Lane("VOYN-FENCED", Action.MERGE, "merge-controller", {}, 7)
    store = FakeStore([lane])
    store.lane_heartbeat = lambda *_args, **_kwargs: False
    side_effects = []

    def handler(_lane, guard):
        guard.require()
        side_effects.append("merge")
        return ActionOutcome.succeeded(Action.DEPLOY)

    report = Reconciler(
        store,
        FakeUnits(),
        {Action.MERGE: handler},
        config(),
        clock=lambda: NOW,
    ).run_once()

    assert side_effects == []
    assert report.advanced == [("VOYN-FENCED", "RETRY")]
    assert store.finished[0][1].detail == "lane_lease_lost"


def test_fenced_effect_is_the_only_external_side_effect_boundary():
    lane = Lane("VOYN-FENCED-EFFECT", Action.MERGE, "merge-controller", {}, 9)
    store = FakeStore([lane])
    effects = []

    def handler(_lane, guard):
        guard.effect(lambda: effects.append("merge"))
        return ActionOutcome.succeeded(Action.DEPLOY)

    report = Reconciler(
        store, FakeUnits(), {Action.MERGE: handler}, config(), clock=lambda: NOW
    ).run_once()

    assert effects == ["merge"]
    assert report.advanced == [(lane.task_id, "READY")]


def test_unit_manager_refuses_any_name_outside_compiled_allowlist():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    manager = SystemdUnitManager(("aicc-safe.timer",), runner=run)
    result = manager.ensure_active("ssh.service", dry_run=False)

    assert not result.healthy
    assert result.detail == "unit_not_allowlisted"
    assert calls == []


def test_unit_manager_repairs_an_inactive_allowlisted_timer_and_rechecks():
    calls = []
    probes = iter(
        [
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\n",
            "LoadState=loaded\nActiveState=active\nSubState=waiting\n",
        ]
    )

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, next(probes), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    manager = SystemdUnitManager(("aicc-safe.timer",), runner=run)
    result = manager.ensure_active("aicc-safe.timer", dry_run=False)

    assert result.healthy
    assert [call[1] for call in calls] == ["show", "start", "show"]


def test_unit_manager_dry_run_accepts_loaded_inactive_unit_as_plan_only():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n",
            "",
        )

    result = SystemdUnitManager(("aicc-safe.timer",), runner=run).ensure_active(
        "aicc-safe.timer", dry_run=True
    )

    assert result.healthy
    assert result.detail.startswith("would_start:")
    assert [call[1] for call in calls] == ["show"]


def test_unit_probe_checks_result_and_restart_counter_not_only_active_state():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "LoadState=loaded\nActiveState=active\nSubState=running\n"
            "Result=success\nNRestarts=4\n",
            "",
        )

    manager = SystemdUnitManager(("aicc-worker.service",), runner=run, attempts=1)
    result = manager.ensure_active("aicc-worker.service", dry_run=True)

    assert not result.healthy
    assert '"NRestarts": "4"' in result.detail
    assert '"Result": "success"' in result.detail
    assert "--property=Result" in calls[0]
    assert "--property=NRestarts" in calls[0]


def test_notification_adapter_is_https_only_and_sends_no_tooling():
    with pytest.raises(ValueError, match="HTTPS"):
        HttpsNotificationAdapter("http://owner.example/alert")

    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, *, timeout):
        requests.append((request, timeout))
        return Response()

    adapter = HttpsNotificationAdapter(
        "https://owner.example/alert", "secret", opener=open_request
    )
    adapter({"task_id": "VOYN-X", "kind": "LANE_STALLED"})

    request, timeout = requests[0]
    assert timeout == 15
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer secret"
    assert b'"task_id": "VOYN-X"' in request.data
