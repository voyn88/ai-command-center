from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from command_center.db.config import load_config
from command_center.ops.credential_rotation import (
    CREDENTIAL_SAFETY_MARGIN_SECONDS,
    SELF_CREDENTIAL_TTL_SECONDS,
    SYSTEMD_EXIT_MARGIN_SECONDS,
    Audit,
    CircuitJournal,
    CircuitOpen,
    CircuitState,
    PhaseJournal,
    RotationConfig,
    RotationController,
    RotationError,
    RotationPhase,
    UnitState,
    _postgres_config,
    authority_timeout_seconds,
    load_lane_registry,
    main,
)
from command_center.worker.credential_file import (
    CredentialFileError,
    PreparedCredentialFile,
    prepare_password_update,
    read_environment_file,
)

OLD_PASSWORD = "a" * 64
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
LANE_1 = "voyn-aicc-worker@1.service"
LANE_2 = "voyn-aicc-worker@2.service"
TUNNEL = "voyn-aicc-pgtunnel.service"


def _environment(path: Path) -> Path:
    path.write_text(
        "# preserved\n"
        "AICC_PG_HOST=127.0.0.1\n"
        "AICC_PG_PORT=5433\n"
        "AICC_PG_DB=aicc_preprod\n"
        "AICC_PG_USER=aicc_w_wrk_voyn_worker_01\n"
        f"AICC_PG_PASSWORD={OLD_PASSWORD}\n"
        "AICC_PG_SSLMODE=disable\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_audit_retries_eintr_and_short_writes_without_truncating_jsonl(
    tmp_path: Path, monkeypatch
) -> None:
    import command_center.ops.credential_rotation as rotation_module

    target = tmp_path / "audit.jsonl"
    real_write = os.write
    calls = [0]

    def interrupted_then_short(descriptor: int, payload) -> int:
        calls[0] += 1
        if calls[0] == 1:
            raise InterruptedError
        view = memoryview(payload)
        return real_write(descriptor, view[: max(1, len(view) // 3)])

    monkeypatch.setattr(rotation_module.os, "write", interrupted_then_short)
    Audit(target).emit("rotation_test", lane=LANE_1)

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["event"] == "rotation_test"
    assert document["lane"] == LANE_1
    assert calls[0] > 2


def test_audit_quarantines_crash_tail_and_repairs_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    first = '{"schema_version":1,"event":"prior"}\n'
    tail = b'{"schema_version":1,"event":"cut'
    target.write_bytes(first.encode("utf-8") + tail)
    target.chmod(0o640)

    Audit(target).emit("after_restart")

    documents = [json.loads(line) for line in target.read_text().splitlines()]
    assert [record["event"] for record in documents] == [
        "prior",
        "rotation_audit_tail_quarantined",
        "after_restart",
    ]
    repair = documents[1]
    assert repair["discarded_bytes"] == len(tail)
    assert repair["whole_generation"] is False
    assert (tmp_path / "audit.jsonl.corrupt.1").read_bytes() == tail


def test_audit_rotation_is_bounded_and_every_generation_is_complete_jsonl(
    tmp_path: Path,
) -> None:
    target = tmp_path / "audit.jsonl"
    audit = Audit(target, max_bytes=64 * 1024, backups=2)
    for sequence in range(4):
        audit.emit("large", sequence=sequence, payload="x" * 40_000)

    generations = [target, tmp_path / "audit.jsonl.1", tmp_path / "audit.jsonl.2"]
    assert all(path.exists() for path in generations)
    assert not (tmp_path / "audit.jsonl.3").exists()
    for path in generations:
        assert path.read_bytes().endswith(b"\n")
        for line in path.read_text().splitlines():
            assert json.loads(line)["schema_version"] == 1


@pytest.mark.parametrize("journal_kind", ["phase", "circuit"])
def test_journal_write_failure_never_recloses_fd_owned_by_stream(
    tmp_path: Path, monkeypatch, journal_kind: str
) -> None:
    """fdopen() owns the descriptor even when a later durability step fails.

    Retrying os.close() from the outer exception handler can close an unrelated
    descriptor if the kernel has already recycled the number.
    """
    import command_center.ops.credential_rotation as rotation_module

    journal = (
        PhaseJournal(tmp_path / "phase.json")
        if journal_kind == "phase"
        else CircuitJournal(tmp_path / "circuit.json")
    )
    explicit_closes: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        explicit_closes.append(descriptor)
        real_close(descriptor)

    def fail_directory_sync() -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(rotation_module.os, "close", recording_close)
    monkeypatch.setattr(journal, "_sync_directory", fail_directory_sync)

    with pytest.raises(OSError, match="directory fsync failed"):
        if journal_kind == "phase":
            journal.write("draining")
        else:
            journal.write(CircuitState(1, None, "RotationError"))

    assert explicit_closes == []


@pytest.mark.parametrize("journal_kind", ["phase", "circuit"])
def test_oversized_journal_read_never_recloses_fd_owned_by_stream(
    tmp_path: Path, monkeypatch, journal_kind: str
) -> None:
    import command_center.ops.credential_rotation as rotation_module

    path = tmp_path / f"{journal_kind}.json"
    path.write_text("x" * 4097, encoding="utf-8")
    path.chmod(0o600)
    journal = PhaseJournal(path) if journal_kind == "phase" else CircuitJournal(path)
    explicit_closes: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        explicit_closes.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(rotation_module.os, "close", recording_close)

    with pytest.raises(RotationError, match="unexpectedly large"):
        journal.load()

    assert explicit_closes == []


def _config(tmp_path: Path, **changes) -> RotationConfig:
    config = RotationConfig(
        env_file=_environment(tmp_path / "worker.env"),
        lock_file=tmp_path / "rotation.lock",
        phase_file=tmp_path / "phase.json",
        circuit_file=tmp_path / "circuit.json",
        audit_file=None,
        tunnel_unit=TUNNEL,
        tunnel_host="127.0.0.1",
        tunnel_port=5433,
        worker_units=(LANE_1, LANE_2),
        prerequisite_timeout=10,
        reload_timeout=10,
        restart_timeout=3720,
        rotation_threshold_seconds=SELF_CREDENTIAL_TTL_SECONDS,
        poll_initial=1,
        poll_max=1,
    )
    return replace(config, **changes)


class FakeSystemd:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.status = {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
        self.reload_fail: set[str] = set()
        self.restart_fail: set[str] = set()
        self.tunnel_ready = True

    def state(self, unit: str) -> UnitState:
        self.events.append(("state", unit))
        if unit == TUNNEL:
            return UnitState(
                "active" if self.tunnel_ready else "activating",
                "running" if self.tunnel_ready else "start",
                "",
                10,
            )
        return UnitState("active", "running", self.status[unit], 100)

    def drain(self, unit: str) -> None:
        self.events.append(("drain", unit))
        self.status[unit] = "aicc-drained"

    def reload(self, unit: str, timeout: float) -> None:
        self.events.append(("reload", unit, timeout))
        if unit in self.reload_fail:
            raise RotationError("reload failed")
        self.status[unit] = "aicc-ready"

    def reload_many(self, units: tuple[str, ...], timeout: float) -> None:
        self.events.append(("reload_many", units, timeout))
        for unit in units:
            self.reload(unit, timeout)

    def restart(self, unit: str, timeout: float) -> None:
        self.events.append(("restart", unit, timeout))
        if unit in self.restart_fail:
            raise RotationError("restart failed")
        self.status[unit] = "aicc-ready"


class FakeAuthority:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.current_password = OLD_PASSWORD
        self.now = lambda: NOW
        self.current_expires_at = NOW + timedelta(hours=1)

    def probe(self, config) -> None:
        self.events.append(("probe", config.password))
        if config.password != self.current_password:
            raise RotationError("stale credential")
        if self.now() >= self.current_expires_at:
            raise RotationError("expired credential")

    def current_expiry(self, config) -> tuple[datetime, float]:
        self.events.append(("expiry", config.password))
        if config.password != self.current_password:
            raise RotationError("stale credential")
        return self.current_expires_at, (
            self.current_expires_at - self.now()
        ).total_seconds()

    def rotate(self, config, new_secret: str, verifier: str):
        self.events.append(("rotate", config.password))
        assert config.password == self.current_password
        assert verifier.startswith("SCRAM-SHA-256$")
        self.current_password = new_secret
        self.current_expires_at = self.now() + timedelta(hours=1)
        return self.current_expires_at


def _controller(tmp_path: Path, events: list[tuple]):
    systemd = FakeSystemd(events)
    authority = FakeAuthority(events)
    controller = RotationController(
        _config(tmp_path),
        systemd,
        authority,
        Audit(),
        port_probe=lambda host, port, timeout: events.append(
            ("port", host, port, timeout)
        ),
    )
    return controller, systemd, authority


def _directives(unit_text: str) -> dict[str, list[str]]:
    """Every occurrence of every key -- a dict() would keep only the LAST
    ExecStopPost=, which is exactly where an unreviewed privileged command
    could hide (independent-review finding on a915297)."""
    collected: dict[str, list[str]] = {}
    for line in unit_text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            collected.setdefault(key, []).append(value)
    return collected


def test_both_lanes_reload_new_credential_without_simultaneous_restart(
    tmp_path: Path,
) -> None:
    """Regression for the worker-2 stale-auth incident: both lanes consume the
    same new file, lane 2 is not forgotten, and lane 2 is activated only after
    lane 1 has passed readiness/auth."""

    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    controller.rotate()

    assert not any(event[0] == "drain" for event in events)
    assert not any(event[0] == "restart" for event in events)
    lane_1_reload = events.index(("reload", LANE_1, 10))
    lane_2_reload = events.index(("reload", LANE_2, 10))
    new_probes = [
        index
        for index, event in enumerate(events)
        if event == ("probe", authority.current_password)
    ]
    assert lane_1_reload < new_probes[-2] < lane_2_reload < new_probes[-1]
    values = read_environment_file(controller.config.env_file)
    assert values["AICC_PG_PASSWORD"] == authority.current_password
    assert values["AICC_PG_PASSWORD"] != OLD_PASSWORD


def test_server_authoritative_threshold_defers_fresh_credential_without_drain(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    controller.config = replace(controller.config, rotation_threshold_seconds=1800)

    controller.rotate()

    assert not any(event[0] in {"drain", "rotate", "reload"} for event in events)
    assert any(event[0] == "expiry" for event in events)


def test_healthy_not_due_tick_clears_transient_failure_count(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    controller.config = replace(controller.config, rotation_threshold_seconds=1800)
    controller.record_failure(RotationError("synthetic"))
    assert controller.config.circuit_file.exists()

    controller.rotate()

    assert not controller.config.circuit_file.exists()
    assert not any(event[0] in {"drain", "rotate", "reload"} for event in events)


def test_durable_circuit_uses_database_time_and_blocks_mutating_retry(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    for _ in range(3):
        controller.record_failure(RotationError("synthetic"))

    with pytest.raises(CircuitOpen, match="cooldown"):
        controller.rotate()

    state = CircuitJournal(controller.config.circuit_file).load()
    assert state.failures == 3
    expected = NOW + timedelta(seconds=controller.config.circuit_cooldown_seconds)
    assert state.blocked_until is not None
    assert timedelta(0) <= state.blocked_until - expected < timedelta(seconds=0.1)
    assert not any(event[0] in {"drain", "rotate", "reload"} for event in events)


def test_open_circuit_cli_retry_does_not_extend_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    recorded: list[Exception] = []

    def refuse(self) -> None:
        raise CircuitOpen("cooldown")

    monkeypatch.setattr(RotationController, "rotate", refuse)
    monkeypatch.setattr(
        RotationController,
        "record_failure",
        lambda self, error: recorded.append(error),
    )
    result = main(
        [
            "--env-file",
            str(_environment(tmp_path / "worker.env")),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(_lane_registry(tmp_path, monkeypatch)),
        ]
    )

    assert result == 1
    assert recorded == []


def test_cli_records_an_ordinary_failure_into_the_circuit_journal(
    tmp_path: Path, monkeypatch
) -> None:
    """The positive twin the negative control above needs: if main() never
    fed record_failure for ordinary RotationErrors, the breaker would never
    open and Restart=on-failure would hammer the identity authority every
    two minutes forever -- and the negative test alone stays green on that
    broken code (independent-review finding on a915297)."""
    recorded: list[Exception] = []

    def explode(self) -> None:
        raise RotationError("ordinary failure")

    monkeypatch.setattr(RotationController, "rotate", explode)
    monkeypatch.setattr(
        RotationController,
        "record_failure",
        lambda self, error: recorded.append(error),
    )
    result = main(
        [
            "--env-file",
            str(_environment(tmp_path / "worker.env")),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(_lane_registry(tmp_path, monkeypatch)),
        ]
    )

    assert result == 1
    assert len(recorded) == 1 and isinstance(recorded[0], RotationError)


def test_expired_circuit_allows_half_open_success_and_clears_latch(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    CircuitJournal(controller.config.circuit_file).write(
        CircuitState(3, NOW - timedelta(seconds=1), "RotationError")
    )

    controller.rotate()

    assert any(event[0] == "rotate" for event in events)
    assert not controller.config.circuit_file.exists()


def test_canary_then_bounded_cohort_rotates_nine_lane_fleet(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    lanes = tuple(f"voyn-aicc-worker@{number}.service" for number in range(1, 10))
    systemd.status = {unit: "aicc-ready" for unit in lanes}
    controller.config = replace(
        controller.config,
        worker_units=lanes,
        reload_timeout=180,
    )

    budget, restart_allowed = controller._post_rotation_budget(
        load_config(read_environment_file(controller.config.env_file))
    )
    assert budget == 1940
    assert restart_allowed is False
    controller.rotate()

    reloads = [event[1] for event in events if event[0] == "reload"]
    assert reloads[0] == lanes[0]
    assert set(reloads) == set(lanes)
    assert systemd.status == {unit: "aicc-ready" for unit in lanes}
    assert authority.current_password != OLD_PASSWORD
    assert not any(event[0] == "restart" for event in events)


def test_singleton_hot_swap_never_closes_its_only_ready_lane(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    controller.config = replace(controller.config, worker_units=(LANE_1,))
    systemd.status = {LANE_1: "aicc-ready"}

    controller.rotate()

    assert ("reload", LANE_1, 10) in events
    assert not any(event[0] in {"drain", "restart"} for event in events)
    assert systemd.status == {LANE_1: "aicc-ready"}


def test_hot_budget_has_no_lane_count_ceiling() -> None:
    config = load_config(
        {
            "AICC_PG_HOST": "127.0.0.1",
            "AICC_PG_PORT": "5433",
            "AICC_PG_DB": "aicc_preprod",
            "AICC_PG_USER": "aicc_w_test",
            "AICC_PG_PASSWORD": OLD_PASSWORD,
            "AICC_PG_SSLMODE": "disable",
        }
    )

    def budget_for(count: int) -> float:
        lanes = tuple(
            f"voyn-aicc-worker@{number}.service" for number in range(1, count + 1)
        )
        controller = RotationController.__new__(RotationController)
        controller.config = RotationConfig(
            env_file=Path("worker.env"),
            lock_file=Path("rotation.lock"),
            phase_file=Path("phase.json"),
            circuit_file=Path("circuit.json"),
            audit_file=None,
            tunnel_unit=TUNNEL,
            tunnel_host="127.0.0.1",
            tunnel_port=5433,
            worker_units=lanes,
            reload_timeout=180,
        )
        return controller._post_rotation_budget(config)[0]

    assert budget_for(26) == budget_for(1_000)


def test_retry_lifetime_covers_every_failed_attempt_and_all_delays(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    config = load_config(read_environment_file(controller.config.env_file))

    one_attempt = (
        2 * controller.config.prerequisite_timeout
        + 2 * authority_timeout_seconds(config)
    )
    assert controller._failed_pre_mutation_attempt_budget(config) == one_attempt
    assert controller._retry_lifetime_budget(config) == (
        controller.config.circuit_failure_threshold * one_attempt
        + controller.config.failure_retry_window_seconds
    )


def test_batch_failure_fails_closed_for_whole_unproved_generation(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    lanes = tuple(f"voyn-aicc-worker@{number}.service" for number in range(1, 11))
    tail = lanes[-1]
    systemd.status = {unit: "aicc-ready" for unit in lanes}
    systemd.reload_fail.add(tail)
    controller.config = replace(
        controller.config,
        worker_units=lanes,
    )
    controller._set_credential_deadline(
        authority.current_expires_at,
        SELF_CREDENTIAL_TTL_SECONDS,
        "test credential",
    )
    controller._restart_fallback_allowed = True

    failures = controller._activate_fleet(
        load_config(read_environment_file(controller.config.env_file))
    )

    assert len(failures) == len(lanes) - 1
    assert all(unit in " ".join(failures) for unit in lanes[1:])
    assert not any(event[0] == "restart" and event[1] == tail for event in events)


def test_failed_canary_defers_cohorts_until_durable_recovery(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    audit_events: list[str] = []
    controller, systemd, _ = _controller(tmp_path, events)
    lanes = tuple(f"voyn-aicc-worker@{number}.service" for number in range(1, 6))
    systemd.status = {unit: "aicc-ready" for unit in lanes}
    systemd.reload_fail.add(lanes[0])
    controller.config = replace(
        controller.config,
        worker_units=lanes,
        reload_timeout=180,
    )
    controller.audit = type(
        "RecordingAudit",
        (),
        {"emit": lambda self, event, **fields: audit_events.append(event)},
    )()

    with pytest.raises(RotationError, match="restart fallback exceeds"):
        controller.rotate()

    assert "worker_activation_fleet_deferred" in audit_events
    assert controller.config.phase_file.exists(), "failed recovery keeps the latch"


def test_recovery_candidate_dedupe_compares_secret_without_fast_hash(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    duplicate = prepare_password_update(controller.config.env_file, OLD_PASSWORD)
    phase = RotationPhase("mutation_started", duplicate.temporary)

    candidates = controller._recovery_candidates(phase)

    assert len(candidates) == 1
    duplicate.discard()


def test_reload_and_restart_failure_is_non_success_and_recovers_other_lane(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    controller.config = replace(controller.config, restart_timeout=20)
    systemd.reload_fail.add(LANE_1)
    systemd.restart_fail.add(LANE_1)

    with pytest.raises(RotationError, match="restart failed"):
        controller.rotate()

    assert ("restart", LANE_1, 20) in events
    assert ("reload", LANE_2, 10) in events, "the second lane restores availability"


def test_tunnel_boot_race_uses_bounded_backoff_then_rotates(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    clock = [0.0]
    attempts = [0]

    def state(unit: str) -> UnitState:
        if unit == TUNNEL:
            attempts[0] += 1
            systemd.tunnel_ready = attempts[0] >= 3
        return FakeSystemd.state(systemd, unit)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    systemd.state = state  # type: ignore[method-assign]
    controller.monotonic = lambda: clock[0]
    controller.sleep = sleep
    controller.rotate()

    assert attempts[0] >= 3
    assert clock[0] <= controller.config.prerequisite_timeout


def test_long_jobs_do_not_delay_hot_credential_handoff(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, _, _ = _controller(tmp_path, events)
    # The hot path never closes the claim gate or waits for a 3600-second
    # handler. Existing checkouts retire after return while the replacement
    # pool is authenticated before the atomic process-local swap.
    controller.rotate()

    rotate_index = next(i for i, event in enumerate(events) if event[0] == "rotate")
    assert not any(event[0] == "drain" for event in events)
    assert events.index(("reload", LANE_1, 10)) > rotate_index
    assert events.index(("reload", LANE_2, 10)) > rotate_index
    assert not any(event[0] == "restart" for event in events)


def test_tunnel_timeout_fails_before_any_drain_or_rotation(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    controller.config = replace(
        controller.config,
        prerequisite_timeout=3,
        poll_initial=1,
        poll_max=1,
    )
    systemd.tunnel_ready = False
    clock = [0.0]
    controller.monotonic = lambda: clock[0]
    controller.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    with pytest.raises(RotationError, match="tunnel readiness"):
        controller.rotate()
    assert not any(event[0] in {"drain", "rotate"} for event in events)


def test_missed_timer_refuses_when_current_ttl_cannot_cover_mutation_attempt(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    clock = [0.0]
    controller.config = replace(
        controller.config,
        prerequisite_timeout=10,
        reload_timeout=10,
        poll_initial=10,
        poll_max=10,
    )
    controller.monotonic = lambda: clock[0]
    controller.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    authority.now = lambda: NOW + timedelta(seconds=clock[0])
    authority.current_expires_at = NOW + timedelta(seconds=320)

    with pytest.raises(RotationError, match="pre-mutation attempt"):
        controller.rotate()

    assert authority.current_password == OLD_PASSWORD
    assert not any(event[0] == "rotate" for event in events)
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert not controller.config.phase_file.exists()


def test_readiness_delay_refreshes_expiry_and_rotates_when_mutation_still_fits(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    clock = [0.0]
    controller.config = replace(
        controller.config,
        prerequisite_timeout=400,
        poll_initial=50,
        poll_max=50,
    )
    controller.monotonic = lambda: clock[0]
    controller.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    authority.now = lambda: NOW + timedelta(seconds=clock[0])
    authority.current_expires_at = NOW + timedelta(seconds=1300)
    original_state = systemd.state

    def state(unit: str) -> UnitState:
        if unit == LANE_1 and clock[0] < 300:
            events.append(("state", unit))
            return UnitState("activating", "start", "", 100)
        return original_state(unit)

    systemd.state = state  # type: ignore[method-assign]

    controller.rotate()

    assert clock[0] == 300
    assert not any(event[0] == "drain" for event in events)
    assert any(event[0] == "rotate" for event in events)
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert not controller.config.phase_file.exists()


def test_mutation_transport_failure_preserves_ready_fleet_and_durable_candidates(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)

    def lost_ack(config, new_secret: str, verifier: str):
        events.append(("rotate", config.password))
        raise RotationError("tunnel lost after mutation may have committed")

    authority.rotate = lost_ack  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="may have committed"):
        controller.rotate()

    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert not any(event[0] in {"drain", "reload", "restart"} for event in events)
    phase = PhaseJournal(controller.config.phase_file).load()
    assert phase is not None and phase.phase == "mutation_started"
    assert phase.recovery_file is not None and phase.recovery_file.exists()


def test_post_rotation_activation_failure_rolls_every_lane_to_verified_new_auth(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    controller.config = replace(controller.config, restart_timeout=20)
    failures_left = [1]
    original_restart = systemd.restart

    def restart(unit: str, timeout: float) -> None:
        if unit == LANE_1 and failures_left[0]:
            failures_left[0] -= 1
            raise RotationError("transient restart failure")
        original_restart(unit, timeout)

    systemd.reload_fail.add(LANE_1)
    systemd.restart = restart  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="transient restart failure"):
        controller.rotate()

    assert authority.current_password != OLD_PASSWORD
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    # A failed canary prevents the normal cohort rollout. Lane 2 is touched only
    # by the recovery pass, which proves the durable credential before READY.
    assert events.count(("reload", LANE_2, 10)) == 1


def test_one_hour_expiry_refuses_long_restart_without_zero_ready_lane(
    tmp_path: Path,
) -> None:
    """A 3600-second job cannot fit after a one-hour credential is issued.

    Exercise lane 1 failure, lane 2 recovery and both rollback attempts against
    one deterministic monotonic/wall clock. The controller must never begin the
    3720-second fallback and every new-credential probe stays inside the expiry
    safety boundary.
    """

    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    elapsed = [0.0]
    controller.config = replace(
        controller.config,
        reload_timeout=180,
        poll_initial=1,
        poll_max=1,
    )
    controller.monotonic = lambda: elapsed[0]
    authority.now = lambda: NOW + timedelta(seconds=elapsed[0])
    original_reload = systemd.reload

    def reload(unit: str, timeout: float) -> None:
        elapsed[0] += timeout
        original_reload(unit, timeout)

    original_probe = authority.probe

    def probe(config) -> None:
        events.append(("timed-probe", config.password, elapsed[0]))
        original_probe(config)

    systemd.reload = reload  # type: ignore[method-assign]
    systemd.reload_fail.add(LANE_1)
    authority.probe = probe  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="restart fallback exceeds"):
        controller.rotate()

    assert not any(event[0] == "restart" for event in events)
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert events.count(("reload", LANE_2, 180)) == 1
    usable_until = SELF_CREDENTIAL_TTL_SECONDS - CREDENTIAL_SAFETY_MARGIN_SECONDS
    new_probes = [
        event[2]
        for event in events
        if event[0] == "timed-probe" and event[1] == authority.current_password
    ]
    assert new_probes
    assert max(new_probes) < usable_until
    assert elapsed[0] < usable_until


def test_controller_refuses_mutation_when_preflight_consumed_recovery_budget(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    clock = [0.0]
    controller.config = replace(
        controller.config,
        controller_timeout=1000,
        reload_timeout=10,
    )
    controller.monotonic = lambda: clock[0]

    original_state = systemd.state
    advanced = [False]

    def state(unit: str) -> UnitState:
        if unit == LANE_1 and not advanced[0]:
            advanced[0] = True
            clock[0] = 700.0
        return original_state(unit)

    systemd.state = state  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="before credential mutation"):
        controller.rotate()

    assert authority.current_password == OLD_PASSWORD
    assert not any(event[0] == "rotate" for event in events)


def test_unexpected_short_expiry_commits_recovery_secret_and_keeps_phase_durable(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    original_rotate = authority.rotate

    def rotate(config, new_secret: str, verifier: str):
        original_rotate(config, new_secret, verifier)
        authority.current_expires_at = NOW + timedelta(minutes=5)
        return authority.current_expires_at

    authority.rotate = rotate  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="safety margin"):
        controller.rotate()

    assert authority.current_password != OLD_PASSWORD
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert (
        read_environment_file(controller.config.env_file)["AICC_PG_PASSWORD"]
        == authority.current_password
    )
    assert PhaseJournal(controller.config.phase_file).load().phase == (
        "credential_committed"
    )


def test_ambiguous_mutation_phase_promotes_only_working_secret_and_reopens_lanes(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    new_secret = "b" * 64
    prepared = prepare_password_update(controller.config.env_file, new_secret)
    controller.phase_journal.write("mutation_started", prepared.temporary)
    CircuitJournal(controller.config.circuit_file).write(
        CircuitState(3, NOW + timedelta(minutes=30), "RotationError")
    )
    authority.current_password = new_secret
    systemd.status = {LANE_1: "aicc-drained", LANE_2: "aicc-drained"}

    assert controller.recover_interrupted() is True

    assert (
        read_environment_file(controller.config.env_file)["AICC_PG_PASSWORD"]
        == new_secret
    )
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert not controller.config.phase_file.exists()
    assert not controller.config.circuit_file.exists()


def test_interrupted_recovery_waits_through_tunnel_boot_race(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    controller.phase_journal.write("gates_closed")
    systemd.status = {LANE_1: "aicc-drained", LANE_2: "aicc-drained"}
    clock = [0.0]
    attempts = [0]

    def state(unit: str) -> UnitState:
        if unit == TUNNEL:
            attempts[0] += 1
            systemd.tunnel_ready = attempts[0] >= 3
        return FakeSystemd.state(systemd, unit)

    systemd.state = state  # type: ignore[method-assign]
    controller.monotonic = lambda: clock[0]
    controller.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    assert controller.recover_interrupted() is True

    assert attempts[0] >= 3
    assert clock[0] <= controller.config.prerequisite_timeout
    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert not controller.config.phase_file.exists()


def test_sigterm_crash_phase_is_reopened_by_execstop_recovery(tmp_path: Path) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    systemd.status = {LANE_1: "aicc-drained", LANE_2: "aicc-drained"}
    script = (
        "import os,signal,sys; "
        "from pathlib import Path; "
        "from command_center.ops.credential_rotation import PhaseJournal; "
        "PhaseJournal(Path(sys.argv[1])).write('gates_closed'); "
        "os.kill(os.getpid(), signal.SIGTERM)"
    )

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(controller.config.phase_file)],
        check=False,
    )
    assert crashed.returncode == -signal.SIGTERM
    assert controller.config.phase_file.exists()

    assert controller.recover_interrupted() is True

    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    assert ("reload", LANE_1, 10) in events
    assert ("reload", LANE_2, 10) in events
    assert not controller.config.phase_file.exists()


def _lane_registry(tmp_path: Path, monkeypatch) -> Path:
    """A registry file plus a loader patched to this process's own uid/gid.

    The production loader demands root:root; tests keep the full parsing and
    mode contract while substituting ownership expectations they can satisfy.
    """
    registry = tmp_path / "aicc-worker-lanes.conf"
    registry.write_text(f"# lanes\n{LANE_1}\n{LANE_2}\n", encoding="utf-8")
    registry.chmod(0o644)
    import command_center.ops.credential_rotation as rotation_module

    original = rotation_module.load_lane_registry
    monkeypatch.setattr(
        rotation_module,
        "load_lane_registry",
        lambda path: original(path, expected_uid=os.getuid(), expected_gid=os.getgid()),
    )
    return registry


def test_cli_returns_nonzero_and_audits_controller_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def fail(self) -> None:
        raise RotationError("readiness failed")

    monkeypatch.setattr(RotationController, "rotate", fail)
    result = main(
        [
            "--env-file",
            str(_environment(tmp_path / "worker.env")),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(_lane_registry(tmp_path, monkeypatch)),
        ]
    )

    assert result == 1
    assert '"event":"rotation_failed"' in capsys.readouterr().out


def test_missing_environment_is_audited_nonzero_not_silently_skipped(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(RotationController, "recover_interrupted", lambda self: False)
    monkeypatch.setattr(RotationController, "_wait_tunnel", lambda self, **kwargs: None)
    result = main(
        [
            "--env-file",
            str(tmp_path / "missing-worker.env"),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(_lane_registry(tmp_path, monkeypatch)),
        ]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert '"event":"rotation_failed"' in output
    assert "cannot read credential file" in output


def test_environment_update_preserves_unrelated_lines_and_mode(tmp_path: Path) -> None:
    path = _environment(tmp_path / "worker.env")
    prepared = prepare_password_update(path, "b" * 64)
    assert read_environment_file(path)["AICC_PG_PASSWORD"] == OLD_PASSWORD
    prepared.commit()
    assert read_environment_file(path)["AICC_PG_PASSWORD"] == "b" * 64
    assert path.read_text(encoding="utf-8").startswith("# preserved\n")
    assert path.stat().st_mode & 0o777 == 0o600


def test_environment_parser_rejects_duplicate_password(tmp_path: Path) -> None:
    path = _environment(tmp_path / "worker.env")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"AICC_PG_PASSWORD={'c' * 64}\n")
    with pytest.raises(CredentialFileError, match="duplicate"):
        prepare_password_update(path, "b" * 64)


def test_post_rotation_rename_failure_retains_only_recovery_secret(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[tuple] = []
    controller, _, authority = _controller(tmp_path, events)

    def fail_commit(self: PreparedCredentialFile) -> None:
        raise OSError("filesystem became read-only")

    monkeypatch.setattr(PreparedCredentialFile, "commit", fail_commit)
    with pytest.raises(RotationError, match="accepted credential retained"):
        controller.rotate()

    assert authority.current_password != OLD_PASSWORD, "database mutation occurred"
    assert (
        read_environment_file(controller.config.env_file)["AICC_PG_PASSWORD"]
        == OLD_PASSWORD
    )
    recovery = list(tmp_path.glob(".worker.env.*"))
    assert len(recovery) == 1
    assert (
        read_environment_file(recovery[0])["AICC_PG_PASSWORD"]
        == authority.current_password
    )


def test_versioned_units_pin_drain_shutdown_and_non_overlapping_timer(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    worker = (root / "deploy/systemd/voyn-aicc-worker@.service").read_text()
    rotation = (
        root / "deploy/systemd/voyn-aicc-credential-rotation.service"
    ).read_text()
    timer = (root / "deploy/systemd/voyn-aicc-credential-rotation.timer").read_text()
    alert = (
        root / "deploy/systemd/voyn-aicc-credential-rotation-alert@.service"
    ).read_text()

    assert "Type=notify-reload" in worker
    assert "KillMode=mixed" in worker
    assert "TimeoutStopSec=3660s" in worker
    assert "TimeoutStartSec=195s" in worker
    assert "--worker-unit" not in rotation, (
        "lane enumeration in the unit file binds the fleet to a fixed size; "
        "lanes come from the root-owned registry"
    )
    assert "--lane-registry /etc/voyn/aicc-worker-lanes.conf" in rotation
    assert "ConditionPathExists" not in rotation
    assert "OnFailure=voyn-aicc-credential-rotation-alert@%n.service" in rotation
    assert "ExecStopPost=" in rotation
    assert "--recover-only" in rotation
    assert "--phase-file /var/lib/voyn-aicc-credential-rotation/phase.json" in rotation
    assert (
        "--circuit-file /var/lib/voyn-aicc-credential-rotation/circuit.json" in rotation
    )
    assert "daemon.err" in alert
    lines = _directives(rotation)
    assert lines["Restart"] == ["on-failure"]
    assert lines["RestartSec"] == ["2min"]
    assert len(lines["ExecStart"]) == 1, "exactly one ExecStart"
    # exactly ONE ExecStopPost -- a second one is where an unreviewed
    # privileged command would hide (independent-review finding on a915297).
    assert len(lines["ExecStopPost"]) == 1, "exactly one ExecStopPost"
    argv = shlex.split(lines["ExecStart"][0])
    recovery_argv = shlex.split(lines["ExecStopPost"][0])
    assert [argument for argument in recovery_argv if argument != "--recover-only"] == (
        argv
    )

    def option(name: str) -> float:
        return float(argv[argv.index(name) + 1])

    controller_timeout = option("--controller-timeout")
    prerequisite_timeout = option("--prerequisite-timeout")
    reload_timeout = option("--reload-timeout")
    restart_timeout = option("--restart-timeout")
    systemd_timeout = float(lines["TimeoutStartSec"][0].removesuffix("s"))
    registry_text = (root / "deploy/voyn-aicc-worker-lanes.conf").read_text()
    deployed_lanes = [
        line.strip()
        for line in registry_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert deployed_lanes == [LANE_1, LANE_2]
    lane_count = len(deployed_lanes)
    waves = 1 if lane_count == 1 else 2
    stop_budget = float(argv[argv.index("--stop-budget") + 1])
    systemd_stop = float(lines["TimeoutStopSec"][0].removesuffix("s"))
    assert systemd_stop >= stop_budget + SYSTEMD_EXIT_MARGIN_SECONDS
    # Derived through the controller's own formula and the real env parser,
    # so a change to connect/statement timeouts moves this proof with it.
    # The VALUES are the test environment's; the production numbers live in
    # the deployed env file this test cannot read.
    authority_timeout = authority_timeout_seconds(
        _postgres_config(
            read_environment_file(_environment(tmp_path / "authority.env"))
        )
    )
    safe_post_rotation = (
        waves * 4 * reload_timeout
        + (2 * waves + 1) * authority_timeout
        + CREDENTIAL_SAFETY_MARGIN_SECONDS
    )
    complete_rotation = (
        2 * prerequisite_timeout + 3 * authority_timeout + safe_post_rotation
    )
    assert systemd_timeout >= controller_timeout + SYSTEMD_EXIT_MARGIN_SECONDS
    assert controller_timeout >= complete_rotation
    assert SELF_CREDENTIAL_TTL_SECONDS >= authority_timeout + safe_post_rotation
    assert (
        SELF_CREDENTIAL_TTL_SECONDS
        < authority_timeout + safe_post_rotation + 2 * lane_count * restart_timeout
    ), "a one-hour credential must disable the long restart fallback globally"
    enrollment = (
        root / "command_center/db/sql/0003_worker_enrollment.up.sql"
    ).read_text()
    rotate_function = enrollment.split("CREATE FUNCTION enroll_rotate_self", 1)[1]
    rotate_function = rotate_function.split("$$;", 1)[0]
    assert "p_new_scram_verifier, interval '1 hour'" in rotate_function
    expiry_migration = (
        root / "command_center/db/sql/0013_current_credential_expiry.up.sql"
    ).read_text()
    assert "v := identity_assert(p_secret)" in expiry_migration
    assert "v_now := clock_timestamp()" in expiry_migration
    assert "REVOKE ALL ON FUNCTION identity_current_credential(text) FROM PUBLIC" in (
        expiry_migration
    )
    assert (
        "GRANT EXECUTE ON FUNCTION identity_current_credential(text) TO aicc_worker"
        in (expiry_migration)
    )
    assert "OnUnitInactiveSec=25min" in timer
    rotation_threshold = option("--rotation-threshold")
    retry_window = option("--failure-retry-window")
    retry_attempts = int(option("--circuit-failure-threshold"))
    failed_pre_mutation = 2 * prerequisite_timeout + 2 * authority_timeout
    assert retry_window >= retry_attempts * 120, (
        "retry window must include every two-minute RestartSec delay"
    )
    normal_delay = 25 * 60
    timer_slack = 60 + 15
    remaining_at_next_tick = SELF_CREDENTIAL_TTL_SECONDS - normal_delay - timer_slack
    assert remaining_at_next_tick <= rotation_threshold
    assert (
        retry_attempts * failed_pre_mutation
        + retry_window
        + CREDENTIAL_SAFETY_MARGIN_SECONDS
        <= rotation_threshold
    ), "threshold must cover every failed attempt, recovery split, and delay"
    assert "OnUnitActiveSec" not in timer
    assert "10.20." not in worker
    assert (
        "100.114."
        not in (root / "deploy/systemd/voyn-aicc-pgtunnel.service").read_text()
    )


def _helper_module():
    root = Path(__file__).parents[2]
    loader = importlib.machinery.SourceFileLoader(
        "voyn_aicc_rotation_helper",
        str(root / "deploy/voyn-aicc-rotation-helper"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_lane_registry_parses_only_strict_lane_units(tmp_path: Path) -> None:
    registry = tmp_path / "lanes.conf"
    registry.write_text(f"# fleet\n{LANE_1}\n{LANE_2}\n", encoding="utf-8")
    registry.chmod(0o644)
    lanes = load_lane_registry(
        registry, expected_uid=os.getuid(), expected_gid=os.getgid()
    )
    assert lanes == (LANE_1, LANE_2)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "# only comments\n",
        "voyn-aicc-worker@1.service\nvoyn-aicc-worker@1.service\n",
        "voyn-aicc-worker@evil.service\n",
        "voyn-aicc-worker@1.service --now\n",
        "sshd.service\n",
        "voyn-aicc-worker@12345.service\n",
    ],
)
def test_lane_registry_refuses_malformed_content(tmp_path: Path, body: str) -> None:
    registry = tmp_path / "lanes.conf"
    registry.write_text(body, encoding="utf-8")
    registry.chmod(0o644)
    with pytest.raises(RotationError):
        load_lane_registry(registry, expected_uid=os.getuid(), expected_gid=os.getgid())


def test_lane_registry_refuses_unsafe_ownership_and_mode(tmp_path: Path) -> None:
    registry = tmp_path / "lanes.conf"
    registry.write_text(f"{LANE_1}\n{LANE_2}\n", encoding="utf-8")
    registry.chmod(0o666)
    with pytest.raises(RotationError, match="writable"):
        load_lane_registry(registry, expected_uid=os.getuid(), expected_gid=os.getgid())
    registry.chmod(0o644)
    with pytest.raises(RotationError, match="root:root"):
        load_lane_registry(
            registry, expected_uid=os.getuid() + 1, expected_gid=os.getgid()
        )
    missing = tmp_path / "absent.conf"
    with pytest.raises(RotationError, match="cannot open"):
        load_lane_registry(missing, expected_uid=os.getuid(), expected_gid=os.getgid())


def test_rotation_helper_resolves_exact_systemctl_argv() -> None:
    helper = _helper_module()
    lanes = (LANE_1, LANE_2)
    assert helper.resolve("drain", LANE_1, lanes) == [
        "kill",
        "--kill-whom=main",
        "--signal=SIGUSR1",
        LANE_1,
    ]
    assert helper.resolve("show", TUNNEL, lanes)[0] == "show"
    assert helper.resolve("reload", LANE_2, lanes) == ["reload", LANE_2]
    assert helper.resolve("restart", LANE_2, lanes) == ["restart", LANE_2]
    assert helper.resolve_many("reload", lanes, lanes) == ["reload", *lanes]


def test_rotation_helper_batch_refuses_transaction_if_any_lane_is_unauthorized() -> None:
    helper = _helper_module()
    with pytest.raises(ValueError, match="not in the lane registry"):
        helper.resolve_many(
            "reload",
            (LANE_1, "voyn-aicc-worker@999.service"),
            (LANE_1, LANE_2),
        )


@pytest.mark.parametrize(
    ("verb", "unit"),
    [
        ("drain", TUNNEL),
        ("restart", TUNNEL),
        ("reload", TUNNEL),
        ("drain", "voyn-aicc-worker@3.service"),
        ("stop", LANE_1),
        ("show", "sshd.service"),
        ("show", "voyn-aicc-worker@1.service --now"),
    ],
)
def test_rotation_helper_refuses_out_of_registry_requests(verb: str, unit: str) -> None:
    helper = _helper_module()
    with pytest.raises(ValueError):
        helper.resolve(verb, unit, (LANE_1, LANE_2))


def test_rotation_helper_and_module_share_the_registry_grammar(
    tmp_path: Path,
) -> None:
    helper = _helper_module()
    text = f"# fleet\n{LANE_1}\n{LANE_2}\n"
    assert helper.load_registry_lines(text) == (LANE_1, LANE_2)
    for bad in ("", "voyn-aicc-worker@x.service\n", f"{LANE_1}\n{LANE_1}\n"):
        with pytest.raises(ValueError):
            helper.load_registry_lines(bad)


def test_sudoers_grants_only_the_helper_without_lane_enumeration() -> None:
    root = Path(__file__).parents[2]
    sudoers = (root / "deploy/sudoers.d/voyn-aicc-credential-rotation").read_text()
    assert "/usr/local/sbin/voyn-aicc-rotation-helper" in sudoers
    assert "systemctl" not in sudoers
    assert "voyn-aicc-worker@1" not in sudoers
    assert "voyn-aicc-worker@2" not in sudoers
    assert "NOPASSWD: VOYN_AICC_ROTATION" in sudoers


def test_recover_only_runs_under_the_stop_budget(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, RotationConfig] = {}

    class _Recorder:
        def __init__(self, config, systemd, authority, audit) -> None:
            captured["config"] = config

        def recover_interrupted(self) -> bool:
            return False

        def rotate(self) -> None:  # pragma: no cover - not taken here
            raise AssertionError("recover-only must not rotate")

    import command_center.ops.credential_rotation as rotation_module

    monkeypatch.setattr(rotation_module, "RotationController", _Recorder)
    registry = _lane_registry(tmp_path, monkeypatch)
    result = main(
        [
            "--recover-only",
            "--env-file",
            str(_environment(tmp_path / "worker.env")),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(registry),
            "--controller-timeout",
            "7200",
            "--stop-budget",
            "1140",
        ]
    )
    assert result == 0
    assert captured["config"].controller_timeout == 1140.0
    assert captured["config"].stop_budget == 1140.0
    assert captured["config"].worker_units == (LANE_1, LANE_2)


def test_malformed_registry_refuses_rotation_fail_closed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    registry = tmp_path / "lanes.conf"
    registry.write_text("sshd.service\n", encoding="utf-8")
    registry.chmod(0o644)
    import command_center.ops.credential_rotation as rotation_module

    original = rotation_module.load_lane_registry
    monkeypatch.setattr(
        rotation_module,
        "load_lane_registry",
        lambda path: original(path, expected_uid=os.getuid(), expected_gid=os.getgid()),
    )
    result = main(
        [
            "--env-file",
            str(_environment(tmp_path / "worker.env")),
            "--lock-file",
            str(tmp_path / "rotation.lock"),
            "--phase-file",
            str(tmp_path / "phase.json"),
            "--circuit-file",
            str(tmp_path / "circuit.json"),
            "--tunnel-unit",
            TUNNEL,
            "--lane-registry",
            str(registry),
        ]
    )
    assert result == 78
    assert '"event":"rotation_refused"' in capsys.readouterr().out
