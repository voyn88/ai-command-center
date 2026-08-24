"""Validated, versioned desired-state registry shared by control and workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["ControlDesiredState", "DesiredState", "WorkerFleetDesiredState", "load"]

_UNIT = re.compile(r"[A-Za-z0-9_.@-]+\.(?:service|timer)\Z")
_JSON_ERRORS = (TypeError, json.JSONDecodeError)


@dataclass(frozen=True, slots=True)
class ControlDesiredState:
    units: tuple[str, ...]
    max_restarts: int
    circuit_open_seconds: int


@dataclass(frozen=True, slots=True)
class WorkerFleetDesiredState:
    units: tuple[str, ...]
    minimum_ready_lanes: int
    max_restarts: int
    max_job_seconds: int
    drain_grace_seconds: int
    recovery_timeout_seconds: int
    recovery_poll_seconds: int
    circuit_failure_threshold: int
    circuit_open_seconds: int

    @property
    def minimum_stop_seconds(self) -> int:
        return self.max_job_seconds + self.drain_grace_seconds


@dataclass(frozen=True, slots=True)
class DesiredState:
    control: ControlDesiredState
    worker_fleet: WorkerFleetDesiredState
    sha256: str


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")  # noqa: TRY004
    if value < (0 if allow_zero else 1):
        raise ValueError(f"{name} is out of range")
    return value


def _units(value: Any, name: str, *, services_only: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    units = tuple(value)
    if any(
        not isinstance(unit, str) or _UNIT.fullmatch(unit) is None for unit in units
    ):
        raise ValueError(f"{name} contains an invalid systemd unit")
    if services_only and any(not unit.endswith(".service") for unit in units):
        raise ValueError(f"{name} may contain only services")
    if len(set(units)) != len(units):
        raise ValueError(f"{name} contains duplicates")
    return units


def load(path: str | Path) -> DesiredState:
    source = Path(path)
    raw = source.read_bytes()
    try:
        root = json.loads(raw)
    except _JSON_ERRORS as exc:
        raise ValueError("desired-state registry is not valid JSON") from exc
    root = _object(root, "registry", {"schema_version", "control", "worker_fleet"})
    if root["schema_version"] != 1:
        raise ValueError("unsupported desired-state schema_version")
    control = _object(
        root["control"],
        "control",
        {"units", "max_restarts", "circuit_open_seconds"},
    )
    worker = _object(
        root["worker_fleet"],
        "worker_fleet",
        {
            "units",
            "minimum_ready_lanes",
            "max_restarts",
            "max_job_seconds",
            "drain_grace_seconds",
            "recovery_timeout_seconds",
            "recovery_poll_seconds",
            "circuit_failure_threshold",
            "circuit_open_seconds",
        },
    )
    worker_units = _units(worker["units"], "worker_fleet.units", services_only=True)
    minimum_ready = _positive(worker["minimum_ready_lanes"], "minimum_ready_lanes")
    if minimum_ready >= len(worker_units):
        raise ValueError("minimum_ready_lanes must leave one recoverable lane")
    max_job = _positive(worker["max_job_seconds"], "max_job_seconds")
    grace = _positive(worker["drain_grace_seconds"], "drain_grace_seconds")
    recovery = _positive(worker["recovery_timeout_seconds"], "recovery_timeout_seconds")
    if recovery < max_job + grace:
        raise ValueError("recovery_timeout_seconds is shorter than the drain budget")
    return DesiredState(
        control=ControlDesiredState(
            units=_units(control["units"], "control.units"),
            max_restarts=_positive(
                control["max_restarts"], "control.max_restarts", allow_zero=True
            ),
            circuit_open_seconds=_positive(
                control["circuit_open_seconds"], "control.circuit_open_seconds"
            ),
        ),
        worker_fleet=WorkerFleetDesiredState(
            units=worker_units,
            minimum_ready_lanes=minimum_ready,
            max_restarts=_positive(
                worker["max_restarts"], "worker_fleet.max_restarts", allow_zero=True
            ),
            max_job_seconds=max_job,
            drain_grace_seconds=grace,
            recovery_timeout_seconds=recovery,
            recovery_poll_seconds=_positive(
                worker["recovery_poll_seconds"], "recovery_poll_seconds"
            ),
            circuit_failure_threshold=_positive(
                worker["circuit_failure_threshold"], "circuit_failure_threshold"
            ),
            circuit_open_seconds=_positive(
                worker["circuit_open_seconds"], "worker_fleet.circuit_open_seconds"
            ),
        ),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument(
        "field",
        choices=(
            "control-units",
            "control-max-restarts",
            "control-circuit-open-seconds",
            "worker-units",
            "worker-minimum-ready",
            "worker-max-restarts",
            "worker-minimum-stop-seconds",
            "worker-recovery-timeout-seconds",
            "worker-recovery-poll-seconds",
            "worker-circuit-failure-threshold",
            "worker-circuit-open-seconds",
            "sha256",
        ),
    )
    args = parser.parse_args(argv)
    desired = load(args.path)
    values: dict[str, Any] = {
        "control-units": desired.control.units,
        "control-max-restarts": desired.control.max_restarts,
        "control-circuit-open-seconds": desired.control.circuit_open_seconds,
        "worker-units": desired.worker_fleet.units,
        "worker-minimum-ready": desired.worker_fleet.minimum_ready_lanes,
        "worker-max-restarts": desired.worker_fleet.max_restarts,
        "worker-minimum-stop-seconds": desired.worker_fleet.minimum_stop_seconds,
        "worker-recovery-timeout-seconds": desired.worker_fleet.recovery_timeout_seconds,
        "worker-recovery-poll-seconds": desired.worker_fleet.recovery_poll_seconds,
        "worker-circuit-failure-threshold": desired.worker_fleet.circuit_failure_threshold,
        "worker-circuit-open-seconds": desired.worker_fleet.circuit_open_seconds,
        "sha256": desired.sha256,
    }
    value = values[args.field]
    if isinstance(value, tuple):
        print("\n".join(value))
    else:
        print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
