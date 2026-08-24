from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from command_center.ops.credential_rotation import (
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
        return "2026-08-24T13:00:00+00:00"


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
    systemd.reload_fail.add(LANE_1)
    systemd.restart_fail.add(LANE_1)

    with pytest.raises(RotationError, match="restart failed"):
        controller.rotate()

    assert ("restart", LANE_1, 3720) in events
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
        systemd.status[unit] = "aicc-draining"

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
    assert "OnUnitInactiveSec=30min" in timer
    assert "OnUnitActiveSec" not in timer
    assert "10.20." not in worker
    assert (
        "100.114."
        not in (root / "deploy/systemd/voyn-aicc-pgtunnel.service").read_text()
    )
