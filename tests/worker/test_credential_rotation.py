from __future__ import annotations

import shlex
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from command_center.ops.credential_rotation import (
    CREDENTIAL_SAFETY_MARGIN_SECONDS,
    SELF_CREDENTIAL_TTL_SECONDS,
    SYSTEMD_EXIT_MARGIN_SECONDS,
    Audit,
    RotationConfig,
    RotationController,
    RotationError,
    UnitState,
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


def _config(tmp_path: Path, **changes) -> RotationConfig:
    config = RotationConfig(
        env_file=_environment(tmp_path / "worker.env"),
        lock_file=tmp_path / "rotation.lock",
        audit_file=None,
        tunnel_unit=TUNNEL,
        tunnel_host="127.0.0.1",
        tunnel_port=5433,
        worker_units=(LANE_1, LANE_2),
        prerequisite_timeout=10,
        drain_timeout=3660,
        reload_timeout=10,
        restart_timeout=3720,
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

    def restart(self, unit: str, timeout: float) -> None:
        self.events.append(("restart", unit, timeout))
        if unit in self.restart_fail:
            raise RotationError("restart failed")
        self.status[unit] = "aicc-ready"


class FakeAuthority:
    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.current_password = OLD_PASSWORD

    def probe(self, config) -> None:
        self.events.append(("probe", config.password))
        if config.password != self.current_password:
            raise RotationError("stale credential")

    def rotate(self, config, new_secret: str, verifier: str):
        self.events.append(("rotate", config.password))
        assert config.password == self.current_password
        assert verifier.startswith("SCRAM-SHA-256$")
        self.current_password = new_secret
        return NOW + timedelta(hours=1)


def _controller(tmp_path: Path, events: list[tuple]):
    systemd = FakeSystemd(events)
    authority = FakeAuthority(events)
    controller = RotationController(
        _config(tmp_path),
        systemd,
        authority,
        Audit(),
        wall_clock=lambda: NOW,
        port_probe=lambda host, port, timeout: events.append(
            ("port", host, port, timeout)
        ),
    )
    return controller, systemd, authority


def test_both_lanes_reload_new_credential_without_simultaneous_restart(
    tmp_path: Path,
) -> None:
    """Regression for the worker-2 stale-auth incident: both lanes consume the
    same new file, lane 2 is not forgotten, and lane 2 is activated only after
    lane 1 has passed readiness/auth."""

    events: list[tuple] = []
    controller, _, authority = _controller(tmp_path, events)
    controller.rotate()

    assert ("drain", LANE_1) in events and ("drain", LANE_2) in events
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
    controller, systemd, _ = _controller(tmp_path, events)
    # `aicc-draining` means the claim gate is closed; a current 3600-second
    # handler may still be alive. Rotation must not wait for that handler or
    # the one-hour credential would expire before it could be reloaded.
    original_drain = systemd.drain

    def drain(unit: str) -> None:
        original_drain(unit)
        systemd.status[unit] = "aicc-drained"

    systemd.drain = drain  # type: ignore[method-assign]
    controller.rotate()

    rotate_index = next(i for i, event in enumerate(events) if event[0] == "rotate")
    assert events.index(("drain", LANE_1)) < rotate_index
    assert events.index(("drain", LANE_2)) < rotate_index
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


def test_post_drain_prerequisite_failure_resumes_both_lanes_with_verified_auth(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, _ = _controller(tmp_path, events)
    tunnel_checks = [0]

    def state(unit: str) -> UnitState:
        if unit == TUNNEL:
            tunnel_checks[0] += 1
            # Initial preflight succeeds; the post-drain proof fails.
            systemd.tunnel_ready = tunnel_checks[0] == 1
        return FakeSystemd.state(systemd, unit)

    clock = [0.0]
    systemd.state = state  # type: ignore[method-assign]
    controller.config = replace(controller.config, prerequisite_timeout=2)
    controller.monotonic = lambda: clock[0]
    controller.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    with pytest.raises(RotationError, match="tunnel readiness"):
        controller.rotate()

    assert systemd.status == {LANE_1: "aicc-ready", LANE_2: "aicc-ready"}
    for lane in (LANE_1, LANE_2):
        assert ("reload", lane, 10) in events
    assert not any(event[0] == "rotate" for event in events)


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
    # One activation failure plus one rollback attempt; the latter proves the
    # durable current credential before advertising ready.
    assert events.count(("reload", LANE_2, 10)) >= 2


def test_one_hour_expiry_bounds_both_lanes_and_rollback_before_long_restart(
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
    controller.wall_clock = lambda: NOW + timedelta(seconds=elapsed[0])
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
    assert systemd.status == {LANE_1: "aicc-drained", LANE_2: "aicc-ready"}
    assert events.count(("reload", LANE_2, 180)) == 2
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
    controller, _, authority = _controller(tmp_path, events)
    clock = [0.0]
    controller.config = replace(
        controller.config,
        controller_timeout=1000,
        drain_timeout=10,
        reload_timeout=10,
    )
    controller.monotonic = lambda: clock[0]

    tunnel_calls = [0]

    def port_probe(host: str, port: int, timeout: float) -> None:
        tunnel_calls[0] += 1
        if tunnel_calls[0] == 2:
            clock[0] = 500.0

    controller.port_probe = port_probe

    with pytest.raises(RotationError, match="before credential mutation"):
        controller.rotate()

    assert authority.current_password == OLD_PASSWORD
    assert not any(event[0] == "rotate" for event in events)


def test_unexpected_short_expiry_retains_recovery_secret_and_keeps_lanes_drained(
    tmp_path: Path,
) -> None:
    events: list[tuple] = []
    controller, systemd, authority = _controller(tmp_path, events)
    original_rotate = authority.rotate

    def rotate(config, new_secret: str, verifier: str):
        original_rotate(config, new_secret, verifier)
        return NOW + timedelta(minutes=5)

    authority.rotate = rotate  # type: ignore[method-assign]

    with pytest.raises(RotationError, match="recovery file retained"):
        controller.rotate()

    assert authority.current_password != OLD_PASSWORD
    assert systemd.status == {LANE_1: "aicc-drained", LANE_2: "aicc-drained"}
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
            "--tunnel-unit",
            TUNNEL,
            "--worker-unit",
            LANE_1,
            "--worker-unit",
            LANE_2,
        ]
    )

    assert result == 1
    assert '"event":"rotation_failed"' in capsys.readouterr().out


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
    with pytest.raises(RotationError, match="recovery file retained"):
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


def test_versioned_units_pin_drain_shutdown_and_non_overlapping_timer() -> None:
    root = Path(__file__).parents[2]
    worker = (root / "deploy/systemd/voyn-aicc-worker@.service").read_text()
    rotation = (
        root / "deploy/systemd/voyn-aicc-credential-rotation.service"
    ).read_text()
    timer = (root / "deploy/systemd/voyn-aicc-credential-rotation.timer").read_text()

    assert "Type=notify-reload" in worker
    assert "KillMode=mixed" in worker
    assert "TimeoutStopSec=3660s" in worker
    assert "TimeoutStartSec=180s" in worker
    assert "ExecReload=/bin/kill -HUP $MAINPID" in worker
    assert rotation.index("--worker-unit voyn-aicc-worker@1.service") < rotation.index(
        "--worker-unit voyn-aicc-worker@2.service"
    )
    lines = dict(line.split("=", 1) for line in rotation.splitlines() if "=" in line)
    argv = shlex.split(lines["ExecStart"])

    def option(name: str) -> float:
        return float(argv[argv.index(name) + 1])

    controller_timeout = option("--controller-timeout")
    prerequisite_timeout = option("--prerequisite-timeout")
    drain_timeout = option("--drain-timeout")
    reload_timeout = option("--reload-timeout")
    restart_timeout = option("--restart-timeout")
    systemd_timeout = float(lines["TimeoutStartSec"].removesuffix("s"))
    lane_count = argv.count("--worker-unit")
    authority_timeout = 10 + 30
    safe_post_rotation = (
        lane_count * 4 * reload_timeout
        + (2 * lane_count + 1) * authority_timeout
        + CREDENTIAL_SAFETY_MARGIN_SECONDS
    )
    complete_rotation = (
        (lane_count + 2) * prerequisite_timeout
        + drain_timeout
        + 3 * authority_timeout
        + safe_post_rotation
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
    assert "OnUnitInactiveSec=30min" in timer
    assert "OnUnitActiveSec" not in timer
    assert "10.20." not in worker
    assert (
        "100.114."
        not in (root / "deploy/systemd/voyn-aicc-pgtunnel.service").read_text()
    )
