#!/usr/bin/python3
"""Discover, roll and verify every AICC systemd worker lane."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

UNIT_RE = re.compile(r"voyn-aicc-worker@[^/@\s]+\.service")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
RESTORABLE_UNIT_RE = re.compile(
    r"(?:voyn-aicc-worker@[^/@\s]+\.service|"
    r"voyn-aicc-worker(?:-2)?\.service|"
    r"aicc-agent-launcher\.socket|aicc-principal-recovery\.service)"
)
LEGACY_WORKER_UNITS = (
    "voyn-aicc-worker.service",
    "voyn-aicc-worker-2.service",
)
DEFAULT_LANES = Path("/etc/aicc/worker-lanes")
DEFAULT_PRIVILEGED_USERS = Path("/etc/aicc/privileged-principals")
WORKER_TEMPLATE = Path("/etc/systemd/system/voyn-aicc-worker@.service")
WORKER_DROPIN = Path(
    "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf"
)
EXPECTED_WORKER_EXECSTART = "/opt/aicc/.venv/bin/python -m command_center.worker"
REQUIRED_ISOLATION_ENVIRONMENT = "AICC_AGENT_PRINCIPAL_ISOLATION=required"


class RolloutError(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitState:
    enabled: bool
    active: bool


class Systemd:
    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["/usr/bin/systemctl", *args],
            capture_output=True,
            check=False,
            text=True,
        )
        if check and result.returncode:
            raise RolloutError(
                result.stderr.strip() or f"systemctl {' '.join(args)} failed"
            )
        return result.stdout.strip()

    def property(self, unit: str, name: str) -> str:
        return self.run("show", unit, f"--property={name}", "--value")


def _configured_units(path: Path) -> set[str]:
    units: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        unit = (
            value if UNIT_RE.fullmatch(value) else f"voyn-aicc-worker@{value}.service"
        )
        if not UNIT_RE.fullmatch(unit):
            raise RolloutError(f"invalid worker lane: {value}")
        units.add(unit)
    return units


def _configured_users(path: Path) -> tuple[str, ...]:
    users: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if not USER_RE.fullmatch(value):
            raise RolloutError(f"invalid privileged principal: {value}")
        users.append(value)
    if not users:
        raise RolloutError("privileged principal set is empty")
    return tuple(dict.fromkeys(users))


def discover_units(
    systemd: Systemd, lanes_path: Path = DEFAULT_LANES
) -> tuple[str, ...]:
    units = _configured_units(lanes_path)
    for command in (
        ("list-unit-files", "voyn-aicc-worker@*.service", "--no-legend", "--no-pager"),
        (
            "list-units",
            "voyn-aicc-worker@*.service",
            "--all",
            "--no-legend",
            "--no-pager",
        ),
    ):
        for line in systemd.run(*command, check=False).splitlines():
            candidate = line.split(maxsplit=1)[0] if line.split() else ""
            if UNIT_RE.fullmatch(candidate):
                units.add(candidate)
    if not units:
        raise RolloutError("no worker lanes discovered")
    return tuple(sorted(units))


def retire_legacy_units(systemd: Systemd) -> None:
    """Drain and disable every pre-template worker before lane startup."""
    for unit in LEGACY_WORKER_UNITS:
        systemd.run("stop", unit, check=False)
        systemd.run("disable", unit, check=False)
    verify_legacy_units_retired(systemd)


def verify_legacy_units_retired(systemd: Systemd) -> None:
    """Prove old claimers cannot coexist with templated worker lanes."""
    for unit in LEGACY_WORKER_UNITS:
        active = systemd.run("is-active", unit, check=False)
        enabled = systemd.run("is-enabled", unit, check=False)
        main_pid = systemd.run(
            "show", unit, "--property=MainPID", "--value", check=False
        )
        if active == "active" or enabled == "enabled" or main_pid not in {"", "0"}:
            raise RolloutError(f"legacy worker was not drained and disabled: {unit}")


def snapshot(systemd: Systemd, units: tuple[str, ...]) -> dict[str, object]:
    return {
        "version": 2,
        "units": {
            unit: {
                "exists": systemd.run(
                    "show", unit, "--property=LoadState", "--value", check=False
                )
                not in {"", "not-found"},
                "enabled": systemd.run("is-enabled", unit, check=False) == "enabled",
                "active": systemd.run("is-active", unit, check=False) == "active",
            }
            for unit in units
        },
    }


def restore(systemd: Systemd, state: dict[str, object]) -> None:
    raw_units = state.get("units")
    if state.get("version") != 2 or not isinstance(raw_units, dict):
        raise RolloutError("invalid service snapshot")
    for unit, raw in sorted(raw_units.items(), reverse=True):
        if (
            not isinstance(unit, str)
            or not RESTORABLE_UNIT_RE.fullmatch(unit)
            or not isinstance(raw, dict)
            or not isinstance(raw.get("exists"), bool)
        ):
            raise RolloutError("invalid service snapshot unit")
        if raw["exists"] is False:
            systemd.run("stop", unit, check=False)
            systemd.run("disable", unit, check=False)
            active = systemd.run("is-active", unit, check=False)
            enabled = systemd.run("is-enabled", unit, check=False)
            load_state = systemd.run(
                "show", unit, "--property=LoadState", "--value", check=False
            )
            main_pid = systemd.run(
                "show", unit, "--property=MainPID", "--value", check=False
            )
            self_recovery = (
                unit == "aicc-principal-recovery.service"
                and active == "active"
                and main_pid == str(os.getpid())
            )
            if (
                enabled == "enabled"
                or (active == "active" and not self_recovery)
                or (load_state not in {"", "not-found"} and not self_recovery)
                or (active != "active" and main_pid not in {"", "0"})
            ):
                raise RolloutError(f"service snapshot did not restore exactly: {unit}")
            continue
        systemd.run("enable" if raw.get("enabled") is True else "disable", unit)
        systemd.run("start" if raw.get("active") is True else "stop", unit)
        active = systemd.run("is-active", unit, check=False)
        enabled = systemd.run("is-enabled", unit, check=False)
        load_state = systemd.run(
            "show", unit, "--property=LoadState", "--value", check=False
        )
        main_pid = systemd.run(
            "show", unit, "--property=MainPID", "--value", check=False
        )
        if (
            load_state in {"", "not-found"}
            or ((active == "active") is not (raw.get("active") is True))
            or ((enabled == "enabled") is not (raw.get("enabled") is True))
            or (active != "active" and main_pid not in {"", "0"})
        ):
            raise RolloutError(f"service snapshot did not restore exactly: {unit}")


def _uid_for_user(user: str) -> int:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError as exc:
        raise RolloutError(f"system user does not exist: {user}") from exc


def _process_uid(pid: int) -> int:
    try:
        for line in (
            Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
        ):
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (FileNotFoundError, ValueError) as exc:
        raise RolloutError(f"cannot prove MainPID UID for {pid}") from exc
    raise RolloutError(f"MainPID {pid} has no Uid field")


def _systemd_words(value: str, *, unit: str, property_name: str) -> tuple[str, ...]:
    """Parse a systemd ``show`` word list without accepting malformed quoting."""
    try:
        return tuple(shlex.split(value))
    except ValueError as exc:
        raise RolloutError(f"{unit} {property_name} is malformed") from exc


def _execstart_argv(value: str, *, unit: str) -> str:
    """Extract the effective argv from ``systemctl show -p ExecStart`` output."""
    if value == EXPECTED_WORKER_EXECSTART:
        return value
    matches = re.findall(r"(?:^|;\s*)argv\[\]=([^;]*?)(?=\s*;|$)", value)
    if len(matches) != 1:
        raise RolloutError(f"{unit} ExecStart is not the versioned worker command")
    return matches[0].strip()


def verify_unit_configuration(systemd: Systemd, unit: str) -> None:
    """Fail closed on the exact effective worker isolation configuration.

    This check is deliberately independent from liveness.  The rollout calls
    it for *every discovered lane* before retiring or starting anything, then
    repeats it immediately before and after each start.  A missing, masked or
    later-overridden drop-in therefore cannot turn a rollout into a fail-open
    worker start.
    """
    expected = {
        "LoadState": "loaded",
        "FragmentPath": str(WORKER_TEMPLATE),
    }
    for name, value in expected.items():
        if systemd.property(unit, name) != value:
            raise RolloutError(f"{unit} {name} is not {value}")

    dropins = _systemd_words(
        systemd.property(unit, "DropInPaths"),
        unit=unit,
        property_name="DropInPaths",
    )
    if dropins.count(str(WORKER_DROPIN)) != 1:
        raise RolloutError(f"{unit} does not inherit the principal boundary")

    environment = _systemd_words(
        systemd.property(unit, "Environment"),
        unit=unit,
        property_name="Environment",
    )
    isolation_values = tuple(
        item
        for item in environment
        if item.startswith("AICC_AGENT_PRINCIPAL_ISOLATION=")
    )
    if isolation_values != (REQUIRED_ISOLATION_ENVIRONMENT,):
        raise RolloutError(f"{unit} effective principal-isolation flag is not required")

    if _execstart_argv(systemd.property(unit, "ExecStart"), unit=unit) != (
        EXPECTED_WORKER_EXECSTART
    ):
        raise RolloutError(f"{unit} ExecStart is not the versioned worker command")


def verify_unit(
    systemd: Systemd,
    unit: str,
    *,
    agent_uid: int,
    privileged_uids: frozenset[int],
    uid_for_user=None,
    process_uid=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    verify_unit_configuration(systemd, unit)
    expected = {
        "ActiveState": "active",
        "SubState": "running",
        "NoNewPrivileges": "yes",
        "ProtectHome": "read-only",
        "ProtectControlGroups": "yes",
    }
    for name, value in expected.items():
        if systemd.property(unit, name) != value:
            raise RolloutError(f"{unit} {name} is not {value}")
    groups = set(systemd.property(unit, "SupplementaryGroups").split())
    if not {"aicc-publisher", "aicc-workspace"}.issubset(groups):
        raise RolloutError(f"{unit} lacks required authority groups")
    user = systemd.property(unit, "User")
    unit_uid = uid_for_user(user)
    if unit_uid == agent_uid or unit_uid in privileged_uids:
        raise RolloutError(
            f"{unit} User UID is not isolated from privileged principals"
        )
    raw_pid = systemd.property(unit, "MainPID")
    if not raw_pid.isdecimal() or int(raw_pid) <= 0:
        raise RolloutError(f"{unit} has no live MainPID")
    if process_uid(int(raw_pid)) != unit_uid:
        raise RolloutError(f"{unit} MainPID UID does not match systemd User")


def verify_all(
    systemd: Systemd,
    units: tuple[str, ...],
    *,
    agent_user: str,
    privileged_users: tuple[str, ...],
    uid_for_user=None,
    process_uid=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    verify_legacy_units_retired(systemd)
    agent_uid = uid_for_user(agent_user)
    privileged_uids = frozenset(uid_for_user(user) for user in privileged_users)
    if agent_uid in privileged_uids:
        raise RolloutError("agent UID aliases a privileged principal")
    for unit in units:
        verify_unit(
            systemd,
            unit,
            agent_uid=agent_uid,
            privileged_uids=privileged_uids,
            uid_for_user=uid_for_user,
            process_uid=process_uid,
        )


def rollout(
    systemd: Systemd,
    units: tuple[str, ...],
    *,
    agent_user: str,
    privileged_users: tuple[str, ...],
    uid_for_user=None,
    process_uid=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    # Validate the complete discovered fleet before the first mutation.
    # In particular, do not retire a healthy legacy fleet and then learn that
    # a configured/live template lane is masked or fail-open.
    for unit in units:
        verify_unit_configuration(systemd, unit)
    original = snapshot(systemd, (*units, *LEGACY_WORKER_UNITS))
    try:
        retire_legacy_units(systemd)
        agent_uid = uid_for_user(agent_user)
        privileged_uids = frozenset(uid_for_user(user) for user in privileged_users)
        for unit in units:
            systemd.run("enable", unit)
            # A blocking stop is the drain barrier. TimeoutStopSec remains
            # longer than the maximum job, so PID 1 waits before lane advance.
            systemd.run("stop", unit)
            if systemd.property(unit, "ActiveState") != "inactive":
                raise RolloutError(f"{unit} did not drain to inactive")
            if systemd.property(unit, "MainPID") != "0":
                raise RolloutError(f"{unit} retained a stale MainPID after drain")
            # A daemon-reload or drop-in replacement may race the drain.  The
            # final pre-start check makes that race fail closed too.
            verify_unit_configuration(systemd, unit)
            systemd.run("start", unit)
            verify_unit(
                systemd,
                unit,
                agent_uid=agent_uid,
                privileged_uids=privileged_uids,
                uid_for_user=uid_for_user,
                process_uid=process_uid,
            )
    except BaseException:
        # Never reactivate a lane while its effective configuration is known
        # to be fail-open.  The outer installation WAL restores the previous
        # file generation; until that recovery completes, draining every
        # template lane is safer than resurrecting the snapshot with a masked
        # or overridden isolation drop-in.
        unsafe_configuration = False
        for unit in units:
            try:
                verify_unit_configuration(systemd, unit)
            except RolloutError:
                unsafe_configuration = True
        if unsafe_configuration:
            for unit in units:
                systemd.run("stop", unit, check=False)
                systemd.run("disable", unit, check=False)
        else:
            restore(systemd, original)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "restore", "rollout", "verify"))
    parser.add_argument("--lanes", type=Path, default=DEFAULT_LANES)
    parser.add_argument(
        "--privileged-users-file", type=Path, default=DEFAULT_PRIVILEGED_USERS
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--agent-user", default="aicc-agent")
    parser.add_argument("--include-unit", action="append", default=[])
    parser.add_argument("--privileged-user", action="append", default=[])
    args = parser.parse_args()
    systemd = Systemd()
    if args.action == "restore":
        if args.state is None:
            parser.error("restore requires --state")
        restore(systemd, json.loads(args.state.read_text(encoding="utf-8")))
        return 0
    units = discover_units(systemd, args.lanes)
    if args.action == "snapshot":
        if args.state is None:
            parser.error("snapshot requires --state")
        included = tuple(args.include_unit)
        if any(not RESTORABLE_UNIT_RE.fullmatch(unit) for unit in included):
            parser.error("--include-unit is not allowlisted")
        payload = json.dumps(
            snapshot(systemd, (*units, *included)), sort_keys=True
        ).encode()
        temporary = args.state.with_name(f".{args.state.name}.{os.getpid()}")
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        os.replace(temporary, args.state)
        descriptor = os.open(args.state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    elif args.action == "rollout":
        privileged_users = (
            *_configured_users(args.privileged_users_file),
            *args.privileged_user,
        )
        rollout(
            systemd,
            units,
            agent_user=args.agent_user,
            privileged_users=privileged_users,
        )
    else:
        privileged_users = (
            *_configured_users(args.privileged_users_file),
            *args.privileged_user,
        )
        verify_all(
            systemd,
            units,
            agent_user=args.agent_user,
            privileged_users=privileged_users,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
