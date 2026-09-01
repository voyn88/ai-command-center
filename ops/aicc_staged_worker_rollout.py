#!/usr/bin/python3
"""Discover, roll and verify every AICC systemd worker lane."""

from __future__ import annotations

import argparse
import errno
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Single source of truth for the snapshot property set: import the ordered
# tuple from the install-transaction module (same ops/ directory) so the two
# cannot drift and trip the cross-module set-equality check at recovery time
# (review on d8920b6). Both scripts are invoked by absolute path, so add
# this file's own directory to the path before importing its sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aicc_install_transaction import SNAPSHOT_PROPERTIES

LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
UNIT_RE = re.compile(r"voyn-aicc-worker@([A-Za-z0-9][A-Za-z0-9_-]{0,62})\.service")
# One instance per accepted broker connection. These have no unit file of
# their own -- they run off /etc/systemd/system/aicc-agent-launcher@.service,
# which the control generation removes -- so they must be discovered,
# snapshotted and restored exactly like the worker lanes.
LAUNCHER_UNIT_RE = re.compile(r"aicc-agent-launcher@[^/@\s]+\.service")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
RESTORABLE_UNIT_RE = re.compile(
    r"(?:voyn-aicc-worker@[^/@\s]+\.service|"
    r"voyn-aicc-worker(?:-2)?\.service|"
    r"aicc-worker\.service|"
    r"aicc-agent-launcher@[^/@\s]+\.service|"
    r"aicc-agent-launcher\.socket|aicc-principal-recovery\.service)"
)
LEGACY_WORKER_UNITS = (
    "voyn-aicc-worker.service",
    "voyn-aicc-worker-2.service",
    "aicc-worker.service",
)
DEFAULT_LANES = Path("/etc/aicc/worker-lanes")
DEFAULT_PRIVILEGED_USERS = Path("/etc/aicc/privileged-principals")
WORKER_TEMPLATE = Path("/etc/systemd/system/voyn-aicc-worker@.service")
WORKER_DROPIN = Path(
    "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf"
)
# ONE delivery story for the fail-closed flag: the drop-in supplies
# Environment= (REQUIRED_ISOLATION_ENVIRONMENT below is verified from it);
# an env-baked ExecStart made the staged drop-in decorative for the template
# family and enshrined two contradictory rollouts (review on 6e22b93).
EXPECTED_WORKER_EXECSTART = (
    "/opt/aicc/current/.venv/bin/python -m command_center.worker"
)
REQUIRED_ISOLATION_ENVIRONMENT = "AICC_AGENT_PRINCIPAL_ISOLATION=required"
EXPECTED_ENVIRONMENT_FILES = (
    "/etc/aicc/lease.env",
    "/var/lib/voyn-aicc-credential-rotation/worker.env",
    "/etc/aicc/executors.env",
    "/etc/aicc/workspace-authority.env",
)
EXPECTED_GROUPS = frozenset({"aicc-workspace", "aicc-publisher"})
CURRENT_RELEASE = Path("/opt/aicc/current")
RELEASE_ROOT = Path("/opt/aicc/releases")


class RolloutError(RuntimeError):
    pass


# Test seam: the lane-registry ownership authority. Patched per-module by
# tests to simulate non-root/root registries WITHOUT mutating the global os
# module (which leaked suite-wide; review on 7d4391c). Production == os.fstat.
_registry_fstat = os.fstat

# Test seam: the uid every release path must belong to. Production == 0 (root).
# Tests patch it per-module to the running user so a real symlink chain can be
# built in a temp directory without privileges -- the shape being verified is
# the chain, and it cannot be built root-owned without root. Never read from
# configuration, an environment variable or a file: this is the authority the
# whole check rests on.
_REQUIRED_OWNER_UID = 0

#: Bound on symlink hops while resolving the interpreter. A cycle (or a chain
#: deep enough to look like one) is refused rather than followed forever.
_MAX_SYMLINK_HOPS = 8

# Test seam: where the ancestor walk stops. Production == the filesystem root,
# because a writable directory ANYWHERE above the interpreter can have the
# whole subtree renamed out from under it. Tests point it at their own temp
# tree, whose ancestors (`/tmp` is world-writable and sticky) cannot be made
# to pass and are not what is under test. Never read from configuration.
_TRUSTED_ROOT = Path("/")


def _verify_immutable_object(path: Path) -> os.stat_result:
    """Refuse `path` unless root owns it and nobody else can rewrite it.

    Symlinks are exempted from the mode check, and that is not a relaxation.
    Linux ignores a symlink's permission bits entirely -- every symlink is
    created `lrwxrwxrwx` and cannot be chmod'ed -- so `st_mode & 0o022` is
    true for *every* symlink that has ever existed. Applying it here meant the
    check could never pass: `.venv/bin/python` is a symlink in every virtual
    environment Python builds, so the gate refused its own release on every
    host, always (found by the first live install that reached it, 2026-08-31).
    What actually protects a symlink from being swapped is write permission on
    the directory holding it, and that directory is verified as its own entry
    in the chain below.
    """
    try:
        info = path.lstat()
    except OSError as exc:
        raise RolloutError(f"AICC release path is unavailable: {path}") from exc
    if info.st_uid != _REQUIRED_OWNER_UID:
        raise RolloutError(f"AICC release path is mutable: {path}")
    if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
        raise RolloutError(f"AICC release path is mutable: {path}")
    return info


def _ancestors_of(path: Path) -> tuple[Path, ...]:
    """Every directory between `path` and `_TRUSTED_ROOT`, inclusive.

    Terminates at the trusted root, and also when a parent stops changing, so
    a path outside the trusted root still ends rather than looping at `/`.
    """
    ancestors: list[Path] = []
    current = path.parent
    while True:
        ancestors.append(current)
        if current == _TRUSTED_ROOT or current == current.parent:
            return tuple(ancestors)
        current = current.parent


def _interpreter_resolution_chain(executable: Path) -> tuple[Path, ...]:
    """Every path the kernel touches to reach the real interpreter, plus the
    directory each one lives in.

    A virtualenv interpreter is a symlink chain that leaves the release:
    `.venv/bin/python -> python3 -> /usr/bin/python3 -> python3.12`. Verifying
    only the paths inside the release therefore proves nothing about the binary
    that actually runs -- the last two hops are system paths this function is
    the only thing that looks at.

    Every ancestor of every hop is included, not just its immediate parent.
    Write permission on any directory above the interpreter is enough to
    rename the subtree out and drop a replacement in its place, so checking
    `/usr/bin` while ignoring `/usr` would leave the binary swappable by
    anyone who can write to `/usr` (independent review of `ab887b2` caught
    exactly that: only the hop and its immediate parent were checked).
    """
    chain: list[Path] = []
    current = executable
    for _ in range(_MAX_SYMLINK_HOPS + 1):
        chain.append(current)
        chain.extend(_ancestors_of(current))
        try:
            info = current.lstat()
        except OSError as exc:
            raise RolloutError(f"AICC release path is unavailable: {current}") from exc
        if not stat.S_ISLNK(info.st_mode):
            return tuple(chain)
        target = os.readlink(current)
        current = Path(target) if os.path.isabs(target) else current.parent / target
    raise RolloutError(f"AICC release interpreter symlink chain is too deep: {executable}")


def verify_immutable_release() -> None:
    """Prove the selected worker executable belongs to one immutable commit release."""
    try:
        link = CURRENT_RELEASE.lstat()
        target_text = os.readlink(CURRENT_RELEASE)
        target = CURRENT_RELEASE.resolve(strict=True)
    except OSError as exc:
        raise RolloutError("current AICC release is unavailable") from exc
    if (
        not CURRENT_RELEASE.is_symlink()
        or link.st_uid != _REQUIRED_OWNER_UID
        or target.parent != RELEASE_ROOT
        or not re.fullmatch(r"[0-9a-f]{40}", target.name)
        or target_text != f"releases/{target.name}"
    ):
        raise RolloutError("current AICC release selector is not immutable")
    executable = target / ".venv/bin/python"
    # Walk the FULL chain, including the intermediate .venv and .venv/bin:
    # directory-write permission on ANY ancestor lets an attacker rename the
    # interpreter out and drop a replacement, independent of the file's own
    # mode (review on 52ced1f). Endpoints-only validation missed exactly
    # those two directories.
    ancestors = (
        CURRENT_RELEASE.parent,
        RELEASE_ROOT,
        target,
        target / ".venv",
        target / ".venv/bin",
    )
    seen: set[Path] = set()
    for path in ancestors + _interpreter_resolution_chain(executable):
        if path in seen:
            continue
        seen.add(path)
        _verify_immutable_object(path)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RolloutError("AICC release interpreter is not executable")


@dataclass(frozen=True)
class UnitState:
    enabled: bool
    active: bool


class Systemd:
    def probe(self, *args: str) -> tuple[int, str, str]:
        result = subprocess.run(
            ["/usr/bin/systemctl", *args],
            capture_output=True,
            check=False,
            text=True,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def run(self, *args: str, check: bool = True) -> str:
        returncode, stdout, stderr = self.probe(*args)
        if check and returncode:
            raise RolloutError(
                stderr or f"systemctl {' '.join(args)} failed"
            )
        return stdout

    def property(self, unit: str, name: str) -> str:
        return self.run("show", unit, f"--property={name}", "--value")


def _configured_units(path: Path) -> set[str]:
    raw_registry = _read_lane_registry(path)
    units: set[str] = set()
    for raw in raw_registry.splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        match = UNIT_RE.fullmatch(value)
        lane = match.group(1) if match else value
        if not LANE_RE.fullmatch(lane):
            raise RolloutError(f"invalid worker lane: {value}")
        unit = f"voyn-aicc-worker@{lane}.service"
        if unit in units:
            raise RolloutError(f"duplicate worker lane: {value}")
        units.add(unit)
    return units


def _read_lane_registry(path: Path) -> str:
    """Read the canonical lane registry through one stable, trusted fd.

    ``lstat(); path.read_text()`` is not an ownership check: an unprivileged
    writer can replace the pathname between those calls.  Open the final
    component relative to a no-follow parent, validate the opened inode, read
    only that descriptor, and compare both the descriptor and pathname after
    the read.  A replacement therefore fails closed instead of changing the
    lanes used by a rollout.
    """
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_fd = os.open(path.parent, parent_flags)
        # The basename is intentionally opened with O_NOFOLLOW.  fstat below,
        # rather than lstat alone, is the authority for every byte consumed.
        descriptor = os.open(path.name, file_flags, dir_fd=parent_fd)
        before = _registry_fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > 64 * 1024
        ):
            raise RolloutError(
                "worker lane registry must be root:root regular and not writable"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                raise RolloutError("worker lane registry was truncated while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = _registry_fstat(descriptor)
        try:
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise RolloutError("worker lane registry pathname disappeared") from exc
        identity = (before.st_dev, before.st_ino)
        if (
            (after.st_dev, after.st_ino) != identity
            or (named.st_dev, named.st_ino) != identity
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or after.st_uid != 0
            or after.st_gid != 0
            or stat.S_IMODE(after.st_mode) & 0o022
        ):
            raise RolloutError("worker lane registry changed while being read")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RolloutError("worker lane registry is not valid UTF-8") from exc
    except RolloutError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RolloutError(f"worker lane registry is a symlink: {path}") from exc
        raise RolloutError(
            f"worker lane registry cannot be read safely: {path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_fd is not None:
            os.close(parent_fd)


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


def _listed_instances(
    systemd: Systemd, pattern: str, accepts: re.Pattern[str], *, check: bool
) -> frozenset[str]:
    units: set[str] = set()
    for command in (
        ("list-unit-files", pattern, "--no-legend", "--no-pager"),
        ("list-units", pattern, "--all", "--no-legend", "--no-pager"),
    ):
        for line in systemd.run(*command, check=check).splitlines():
            fields = line.split()
            if fields and fields[0] == "●":
                fields = fields[1:]
            candidate = fields[0] if fields else ""
            if accepts.fullmatch(candidate):
                units.add(candidate)
    return frozenset(units)


def _listed_template_units(systemd: Systemd, *, check: bool) -> frozenset[str]:
    return _listed_instances(
        systemd, "voyn-aicc-worker@*.service", UNIT_RE, check=check
    )


def _listed_launcher_units(systemd: Systemd, *, check: bool) -> frozenset[str]:
    return _listed_instances(
        systemd, "aicc-agent-launcher@*.service", LAUNCHER_UNIT_RE, check=check
    )


def discover_launcher_units(systemd: Systemd) -> tuple[str, ...]:
    """Every live broker instance, for the snapshot only.

    Deliberately not part of `discover_units`: that set is what `rollout()`
    drains, starts and verifies as worker lanes, and a launcher instance is
    neither. What it is, is a running unit whose fragment the control
    generation deletes -- so it belongs in the snapshot that rollback
    restores, and in the closure check that refuses to proceed while one
    exists outside it.
    """
    return tuple(sorted(_listed_launcher_units(systemd, check=False)))


def discover_units(
    systemd: Systemd, lanes_path: Path = DEFAULT_LANES
) -> tuple[str, ...]:
    units = _configured_units(lanes_path)
    units.update(_listed_template_units(systemd, check=False))
    if not units:
        raise RolloutError("no worker lanes discovered")
    return tuple(sorted(units))


def verify_snapshot_closure(systemd: Systemd, state: dict[str, object]) -> None:
    """Fail closed if a retry would leave a worker outside its old snapshot."""
    raw_units = state.get("units")
    if state.get("version") not in {2, 3} or not isinstance(raw_units, dict):
        raise RolloutError("invalid service snapshot")
    expected = set(raw_units)
    if any(
        not isinstance(unit, str) or not RESTORABLE_UNIT_RE.fullmatch(unit)
        for unit in expected
    ):
        raise RolloutError("invalid service snapshot unit")
    discovered = _listed_template_units(systemd, check=True) | _listed_launcher_units(
        systemd, check=True
    )
    extras = sorted(discovered - expected)
    if extras:
        raise RolloutError(
            f"template units exist outside service snapshot: {extras}"
        )


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
        if (
            active in {"active", "activating"}
            or enabled in {"enabled", "enabled-runtime"}
            or main_pid not in {"", "0"}
        ):
            raise RolloutError(f"legacy worker was not drained and disabled: {unit}")


def snapshot(systemd: Systemd, units: tuple[str, ...]) -> dict[str, object]:
    def unit_state(unit: str) -> dict[str, object]:
        exists = systemd.run(
            "show", unit, "--property=LoadState", "--value", check=False
        ) not in {"", "not-found"}
        return {
            "exists": exists,
            "enabled": systemd.run("is-enabled", unit, check=False) == "enabled",
            "active": systemd.run("is-active", unit, check=False) == "active",
            "properties": {
                name: systemd.run(
                    "show", unit, f"--property={name}", "--value", check=False
                )
                for name in SNAPSHOT_PROPERTIES
            }
            if exists
            else {},
        }

    return {
        "version": 3,
        "units": {unit: unit_state(unit) for unit in units},
    }


def restore(systemd: Systemd, state: dict[str, object]) -> None:
    raw_units = state.get("units")
    version = state.get("version")
    if version not in {2, 3} or not isinstance(raw_units, dict):
        raise RolloutError("invalid service snapshot")
    for unit, raw in sorted(raw_units.items(), reverse=True):
        if (
            not isinstance(unit, str)
            or not RESTORABLE_UNIT_RE.fullmatch(unit)
            or not isinstance(raw, dict)
            or not isinstance(raw.get("exists"), bool)
            or (
                version == 3
                and (
                    not isinstance(raw.get("properties"), dict)
                    or any(
                        not isinstance(name, str) or not isinstance(value, str)
                        for name, value in raw["properties"].items()
                    )
                )
            )
        ):
            raise RolloutError("invalid service snapshot unit")
        if raw["exists"] is False:
            # A template instance can disappear after its connection closes.
            # `stop`/`disable` then return non-zero on real systemd; treating
            # that as success in a fake hid the mismatch.  Prove absence first
            # and avoid mutating an object that no longer exists.  A loaded
            # unit is still driven to the snapshotted absent state below.
            load_returncode, load_before, _load_stderr = systemd.probe(
                "show", unit, "--property=LoadState", "--value"
            )
            if load_returncode or not load_before:
                raise RolloutError(
                    f"cannot prove absent service load state: {unit}"
                )
            initially_not_found = load_before == "not-found"
            if not initially_not_found:
                systemd.run("stop", unit, check=False)
                systemd.run("disable", unit, check=False)
            active_rc, active, _active_stderr = systemd.probe("is-active", unit)
            enabled_rc, enabled, _enabled_stderr = systemd.probe(
                "is-enabled", unit
            )
            load_rc, load_state, _post_load_stderr = systemd.probe(
                "show", unit, "--property=LoadState", "--value"
            )
            pid_rc, main_pid, _pid_stderr = systemd.probe(
                "show", unit, "--property=MainPID", "--value"
            )
            if (
                active_rc not in {0, 3, 4}
                or enabled_rc not in {0, 1, 4}
                or load_rc
                or pid_rc
                or not active
                or not enabled
                or not load_state
                or not main_pid
            ):
                raise RolloutError(
                    f"cannot prove absent service state after restore: {unit}"
                )
            self_recovery = (
                unit == "aicc-principal-recovery.service"
                and active == "active"
                and main_pid == str(os.getpid())
            )
            absent_exactly = (
                active == "inactive"
                and enabled in {"disabled", "not-found"}
                and load_state == "not-found"
                and main_pid == "0"
            )
            if not absent_exactly and not self_recovery:
                raise RolloutError(f"service snapshot did not restore exactly: {unit}")
            continue
        if version == 3:
            properties = raw["properties"]
            if set(properties) != set(SNAPSHOT_PROPERTIES):
                raise RolloutError(
                    f"service snapshot properties are incomplete: {unit}"
                )
            for name, expected in properties.items():
                actual = systemd.run(
                    "show", unit, f"--property={name}", "--value", check=False
                )
                if actual != expected:
                    raise RolloutError(
                        f"refusing unsafe snapshot restart: {unit} {name}"
                    )
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
        if version == 3:
            for name, expected in properties.items():
                actual = systemd.run(
                    "show", unit, f"--property={name}", "--value", check=False
                )
                if actual != expected:
                    raise RolloutError(
                        f"service snapshot property did not restore: {unit} {name}"
                    )


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


def _process_environment(pid: int) -> tuple[str, ...]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise RolloutError(f"cannot prove MainPID environment for {pid}") from exc
    try:
        return tuple(value.decode("utf-8") for value in raw.split(b"\0") if value)
    except UnicodeDecodeError as exc:
        raise RolloutError(f"MainPID {pid} environment is malformed") from exc


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
    argv = matches[0].strip()
    # systemd's @/! prefixes decouple the executed binary (path=) from argv0:
    # `ExecStart=@/usr/bin/evil <argv...>` runs /usr/bin/evil while argv[]
    # still matches the expected command. Assert path= equals argv0 so the
    # gate checks the binary that actually runs (review on 4a0a878).
    paths = re.findall(r"(?:^|[;{]\s*)path=([^;]*?)(?=\s*;|$)", value)
    if len(paths) != 1:
        raise RolloutError(f"{unit} ExecStart has no single path= field")
    path = paths[0].strip()
    if path != argv.split(" ", 1)[0]:
        raise RolloutError(f"{unit} ExecStart path= does not match argv[0]")
    return argv


def _environment_file_entries(value: str, *, unit: str) -> tuple[tuple[str, bool], ...]:
    """Return ``(path, optional)`` pairs from EnvironmentFiles serialization.

    systemd renders each file as ``<path> (ignore_errors=<yes|no>)``.  Verifying
    the path alone is fail-open: a required authority file (lease.env, the
    rotation worker.env, workspace-authority.env) marked optional with a ``-``
    prefix keeps the identical path but flips ignore_errors to ``yes``, so a
    tampered unit whose missing authority file is silently skipped would still
    pass a path-only check.  Pairing the flag with its path closes that hole;
    the required/optional split is enforced by the caller.
    """
    words = _systemd_words(value, unit=unit, property_name="EnvironmentFiles")
    entries: list[tuple[str, bool]] = []
    index = 0
    while index < len(words):
        path = words[index]
        if not path.startswith("/") or index + 1 >= len(words):
            raise RolloutError(f"{unit} EnvironmentFiles is malformed")
        flag = words[index + 1]
        if flag == "(ignore_errors=no)":
            optional = False
        elif flag == "(ignore_errors=yes)":
            optional = True
        else:
            raise RolloutError(f"{unit} EnvironmentFiles is malformed")
        entries.append((path, optional))
        index += 2
    return tuple(entries)


def _optional_environment_files(unit: str) -> frozenset[str]:
    """The only EnvironmentFiles that may be ``ignore_errors=yes`` (``-`` prefix).

    Everything else in the versioned set is a required authority file whose
    absence must fail the worker start closed, never be silently skipped.
    """
    match = UNIT_RE.fullmatch(unit)
    if match is None:
        raise RolloutError(f"unsupported worker unit: {unit}")
    instance = unit.removeprefix("voyn-aicc-worker@").removesuffix(".service")
    return frozenset({"/etc/aicc/executors.env", f"/etc/aicc/worker-{instance}.env"})


def _protect_home_is_safe(value: str) -> bool:
    # `ProtectHome=true` is serialized as `yes`; older versions may report
    # the equivalent read-only mount using the explicit enum spelling.
    return value in {"yes", "read-only"}


def _expected_environment_files(unit: str) -> tuple[str, ...]:
    match = UNIT_RE.fullmatch(unit)
    if match is None:
        raise RolloutError(f"unsupported worker unit: {unit}")
    instance = unit.removeprefix("voyn-aicc-worker@").removesuffix(".service")
    return (
        *EXPECTED_ENVIRONMENT_FILES[:-1],
        f"/etc/aicc/worker-{instance}.env",
        EXPECTED_ENVIRONMENT_FILES[-1],
    )


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
        "User": "aicc-worker",
        "Group": "aicc-worker",
        "WorkingDirectory": "/opt/aicc/current",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectControlGroups": "yes",
        "PrivateTmp": "yes",
        "PrivateDevices": "yes",
        "ProtectProc": "invisible",
        "ProcSubset": "pid",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes",
        "ProtectClock": "yes",
        "ProtectHostname": "yes",
        "RestrictSUIDSGID": "yes",
        "LockPersonality": "yes",
        "KeyringMode": "private",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "UMask": "0077",
    }
    for name, value in expected.items():
        if systemd.property(unit, name) != value:
            qualifier = "isolated " if name == "User" else ""
            raise RolloutError(f"{unit} {name} is not {qualifier}{value}")

    dropins = _systemd_words(
        systemd.property(unit, "DropInPaths"),
        unit=unit,
        property_name="DropInPaths",
    )
    # Present AND last, not exact-set: distros ship global drop-ins (e.g.
    # Ubuntu's 10-timeout-abort.conf) that DropInPaths lists for every
    # service; requiring equality aborted healthy hosts. Last-wins ordering
    # plus the effective-property assertions above still catch relaxation
    # (review on d8920b6).
    if not dropins or dropins[-1] != str(WORKER_DROPIN):
        raise RolloutError(f"{unit} does not inherit the principal boundary")

    if not _protect_home_is_safe(systemd.property(unit, "ProtectHome")):
        raise RolloutError(f"{unit} ProtectHome is not isolated")

    environment_entries = _environment_file_entries(
        systemd.property(unit, "EnvironmentFiles"), unit=unit
    )
    environment_files = tuple(path for path, _ in environment_entries)
    if environment_files != _expected_environment_files(unit):
        raise RolloutError(f"{unit} EnvironmentFiles is not the versioned set")
    optional_allowed = _optional_environment_files(unit)
    for path, optional in environment_entries:
        if optional and path not in optional_allowed:
            raise RolloutError(
                f"{unit} EnvironmentFiles marks required {path} optional"
            )

    groups = frozenset(systemd.property(unit, "SupplementaryGroups").split())
    if groups != EXPECTED_GROUPS:
        raise RolloutError(f"{unit} SupplementaryGroups is not the versioned set")

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
    process_environment=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    process_environment = process_environment or _process_environment
    verify_unit_configuration(systemd, unit)
    expected = {
        "ActiveState": "active",
        "SubState": "running",
        "NoNewPrivileges": "yes",
        "ProtectControlGroups": "yes",
    }
    for name, value in expected.items():
        if systemd.property(unit, name) != value:
            raise RolloutError(f"{unit} {name} is not {value}")
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
    isolation = tuple(
        value
        for value in process_environment(int(raw_pid))
        if value.startswith("AICC_AGENT_PRINCIPAL_ISOLATION=")
    )
    if isolation != (REQUIRED_ISOLATION_ENVIRONMENT,):
        raise RolloutError(f"{unit} MainPID principal-isolation flag is not required")


def verify_all(
    systemd: Systemd,
    units: tuple[str, ...],
    *,
    agent_user: str,
    privileged_users: tuple[str, ...],
    uid_for_user=None,
    process_uid=None,
    process_environment=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    process_environment = process_environment or _process_environment
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
            process_environment=process_environment,
        )


# Test seam: whether a required environment file is present. Production is
# the filesystem. Tests drive a fake systemd against paths that do not exist
# on the machine running them, and the property under test there is the
# rollout's ordering, not this host's /etc.
_environment_file_exists = os.path.exists


def verify_required_environment_files(systemd: "Systemd", units: tuple[str, ...]) -> None:
    """Refuse before the first mutation if a required environment file is
    absent from the host.

    `verify_unit_configuration` proves the unit *declares* the right files and
    does not mark a required one optional. It says nothing about whether the
    file exists, because that is not a property of the unit. So a fleet could
    pass every configuration check, have its legacy lanes retired, and only
    then discover at start time that `/etc/aicc/lease.env` was never placed on
    the host — leaving the machine between the old configuration and the new
    one, which is the single worst outcome a rollout can produce.

    That is not hypothetical: it is what happened on worker-01 (2026-08-31).
    The new `voyn-aicc-worker@1.service` failed with `result 'resources'`
    because `lease.env` did not exist, after the legacy lanes were already
    gone.

    The file carries the lease DSN and is therefore *not* installable from the
    repository — no secret belongs in a versioned tree. It is placed by the
    operator or by credential rotation, which is exactly why the installer has
    to check for it rather than create it, and has to check *first*.
    """
    missing: list[str] = []
    for unit in units:
        entries = _environment_file_entries(
            systemd.property(unit, "EnvironmentFiles"), unit=unit
        )
        for path, optional in entries:
            if optional:
                continue
            if not _environment_file_exists(path) and path not in missing:
                missing.append(path)
    if missing:
        raise RolloutError(
            "required environment files are absent: " + ", ".join(sorted(missing))
        )


def rollout(
    systemd: Systemd,
    units: tuple[str, ...],
    *,
    agent_user: str,
    privileged_users: tuple[str, ...],
    uid_for_user=None,
    process_uid=None,
    process_environment=None,
) -> None:
    uid_for_user = uid_for_user or _uid_for_user
    process_uid = process_uid or _process_uid
    process_environment = process_environment or _process_environment
    # Validate the complete discovered fleet before the first mutation.
    # In particular, do not retire a healthy legacy fleet and then learn that
    # a configured/live template lane is masked or fail-open.
    for unit in units:
        verify_unit_configuration(systemd, unit)
    # Declaring the right files is not the same as having them. Checked here,
    # before anything is retired, so an absent authority file cannot strand
    # the host between configurations.
    verify_required_environment_files(systemd, units)
    try:
        agent_uid = uid_for_user(agent_user)
        privileged_uids = frozenset(uid_for_user(user) for user in privileged_users)
        # verify_all() has always refused an aliased agent/privileged UID, but
        # a caller invoking this mutating rollout directly (bypassing the
        # separate shell verifier) was not protected by that check: it could
        # retire a healthy legacy fleet and start the new one under a UID
        # that is not actually isolated from a privileged principal (review
        # finding on 5f2f1dd). Refuse before the first mutation.
        if agent_uid in privileged_uids:
            raise RolloutError("agent UID aliases a privileged principal")
        # Legacy claimers retire BEFORE any templated lane starts claiming
        # (runbook step 5; review on d8920b6) -- and before the loop, so an
        # empty lane set cannot leave them enabled via a silent no-op.
        retire_legacy_units(systemd)
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
                process_environment=process_environment,
            )
    except BaseException:
        # Never restart from a service snapshot while the failed file
        # generation is still installed. The outer write-ahead transaction
        # first restores the exact prior files and only then restores the
        # attempt snapshot. Until that ordered recovery, every touched worker
        # stays fail-closed.
        for unit in (*units, *LEGACY_WORKER_UNITS):
            systemd.run("stop", unit, check=False)
            systemd.run("disable", unit, check=False)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "snapshot",
            "restore",
            "verify-snapshot-closure",
            "rollout",
            "verify",
        ),
    )
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
    if args.action == "verify-snapshot-closure":
        if args.state is None:
            parser.error("verify-snapshot-closure requires --state")
        verify_snapshot_closure(
            systemd, json.loads(args.state.read_text(encoding="utf-8"))
        )
        return 0
    units = discover_units(systemd, args.lanes)
    if args.action in {"rollout", "verify"}:
        verify_immutable_release()
    if args.action == "snapshot":
        if args.state is None:
            parser.error("snapshot requires --state")
        included = tuple(args.include_unit)
        if any(not RESTORABLE_UNIT_RE.fullmatch(unit) for unit in included):
            parser.error("--include-unit is not allowlisted")
        payload = json.dumps(
            snapshot(
                systemd,
                tuple(
                    sorted({*units, *included, *discover_launcher_units(systemd)})
                ),
            ),
            sort_keys=True,
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
