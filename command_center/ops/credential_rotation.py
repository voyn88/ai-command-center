"""Rolling, drain-safe rotation for a registry-defined AICC worker fleet.

The old host-local script changed PostgreSQL's verifier and restarted both
workers immediately.  This controller instead uses an explicit protocol:

1. prove the tunnel, current credential and every registered lane are healthy;
2. prepare the new EnvironmentFile, rotate through the authenticated session,
   and atomically publish the already-fsynced file;
3. hot-reload and authenticate a canary, then one registry-derived systemd
   transaction. Existing database
   checkouts retire only when returned, so a running job and its heartbeat are
   not cut.

There is never a concurrent restart. A failed in-process reload may fall back
to a graceful restart only when the controller and credential deadlines can
cover the complete stop/readiness budget. Otherwise that lane remains drained
while the rest of the fleet is recovered. Every transition is emitted as structured
JSON and failures return non-zero.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import stat as stat_module
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from command_center.db.config import PostgresConfig, load_config
from command_center.worker.credential_file import (
    PreparedCredentialFile,
    parse_environment_text,
    prepare_password_update,
    read_environment_file,
)

_READY = "aicc-ready"

# enroll_rotate_self() grants a fixed one-hour credential. Keep this contract
# beside the protocol budget so a unit/test change cannot silently make a
# graceful 3600-second job incompatible with post-rotation recovery.
SELF_CREDENTIAL_TTL_SECONDS = 3600.0
CREDENTIAL_SAFETY_MARGIN_SECONDS = 300.0
SYSTEMD_EXIT_MARGIN_SECONDS = 600.0
AUDIT_SCHEMA_VERSION = 1
AUDIT_MAX_BYTES = 8 * 1024 * 1024
AUDIT_BACKUPS = 5
AUDIT_MAX_RECORD_BYTES = 64 * 1024


class RotationError(RuntimeError):
    """A fail-closed operational error safe to show in an audit event."""


class CircuitOpen(RotationError):
    """A non-mutating cooldown refusal that must not extend its own deadline."""


@dataclass(frozen=True, slots=True)
class UnitState:
    active: str
    sub: str
    status: str
    main_pid: int


@dataclass(frozen=True, slots=True)
class RotationConfig:
    env_file: Path
    lock_file: Path
    phase_file: Path
    circuit_file: Path
    audit_file: Path | None
    tunnel_unit: str
    tunnel_host: str
    tunnel_port: int
    worker_units: tuple[str, ...]
    prerequisite_timeout: float = 120.0
    # ExecStopPost recovery budget (systemd TimeoutStopSec minus a margin).
    # Recovery invoked from the stop path is bounded to this window; when the
    # fleet is too large to recover inside it, recovery refuses fail-closed,
    # the phase journal survives, and the next timer start recovers under the
    # full controller budget. This is what keeps a static unit file correct
    # for an arbitrary lane count.
    reload_timeout: float = 120.0
    restart_timeout: float = 3720.0
    controller_timeout: float = 7200.0
    stop_budget: float = 1140.0
    rotation_threshold_seconds: float = 2100.0
    failure_retry_window_seconds: float = 360.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 300.0
    poll_initial: float = 0.25
    poll_max: float = 5.0


class Systemd(Protocol):
    def state(self, unit: str) -> UnitState: ...

    def drain(self, unit: str) -> None: ...

    def reload(self, unit: str, timeout: float) -> None: ...

    def reload_many(self, units: tuple[str, ...], timeout: float) -> None: ...

    def restart(self, unit: str, timeout: float) -> None: ...


class CredentialAuthority(Protocol):
    def probe(self, config: PostgresConfig) -> None: ...

    def current_expiry(self, config: PostgresConfig) -> tuple[datetime, float]: ...

    def rotate(
        self, config: PostgresConfig, new_secret: str, verifier: str
    ) -> object: ...


class Audit:
    """Crash-tolerant, bounded JSONL audit sink.

    A separate inode-stable lock serialises open/repair/rotate/write. Locking the
    data inode is insufficient because a concurrent writer can open the newly
    created path immediately after rotation and bypass the lock held on the old
    inode. Every durable path is opened with ``O_NOFOLLOW`` and accepted only
    when it is a safely-owned regular file.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = AUDIT_MAX_BYTES,
        backups: int = AUDIT_BACKUPS,
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._backups = backups

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            try:
                written = os.write(descriptor, remaining)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError(errno.EIO, "audit write made no progress")
            remaining = remaining[written:]

    @staticmethod
    def _validate_file(descriptor: int, description: str) -> os.stat_result:
        info = os.fstat(descriptor)
        if not stat_module.S_ISREG(info.st_mode):
            raise RotationError(f"{description} is not a regular file")
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RotationError(f"{description} ownership/mode is unsafe")
        return info

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _safe_existing(path: Path, description: str) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise RotationError(f"{description} ownership/type is unsafe")
        return True

    def _shift_backups(self, stem: Path) -> None:
        if self._backups < 1:
            raise RotationError("audit backup count must be positive")
        oldest = stem.with_name(f"{stem.name}.{self._backups}")
        if self._safe_existing(oldest, "rotation audit backup"):
            oldest.unlink()
        for number in range(self._backups - 1, 0, -1):
            source = stem.with_name(f"{stem.name}.{number}")
            target = stem.with_name(f"{stem.name}.{number + 1}")
            if self._safe_existing(source, "rotation audit backup"):
                if self._safe_existing(target, "rotation audit backup"):
                    target.unlink()
                os.replace(source, target)

    def _quarantine_tail(self, descriptor: int, size: int) -> dict[str, object] | None:
        if size == 0 or os.pread(descriptor, 1, size - 1) == b"\n":
            return None
        window_start = max(0, size - AUDIT_MAX_RECORD_BYTES)
        window = os.pread(descriptor, size - window_start, window_start)
        newline = window.rfind(b"\n")
        if newline < 0 and window_start:
            # A record larger than the schema limit cannot be trusted. Preserve
            # the whole current generation as one bounded quarantine backup and
            # start a new JSONL generation instead of guessing a truncate point.
            truncate_at = 0
            corrupt = b""
            whole_generation = True
        else:
            truncate_at = window_start + newline + 1
            corrupt = window[newline + 1 :]
            whole_generation = False

        assert self._path is not None
        quarantine = self._path.with_name(f"{self._path.name}.corrupt")
        self._shift_backups(quarantine)
        target = quarantine.with_name(f"{quarantine.name}.1")
        if whole_generation:
            os.fsync(descriptor)
            os.replace(self._path, target)
        else:
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{quarantine.name}.", dir=quarantine.parent
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(temporary_fd, 0o600)
                self._write_all(temporary_fd, corrupt)
                os.fsync(temporary_fd)
                os.close(temporary_fd)
                temporary_fd = -1
                os.replace(temporary, target)
                os.ftruncate(descriptor, truncate_at)
                os.fsync(descriptor)
            finally:
                if temporary_fd != -1:
                    os.close(temporary_fd)
                temporary.unlink(missing_ok=True)
        self._sync_directory(self._path.parent)
        return {
            "discarded_bytes": size - truncate_at,
            "quarantine": target.name,
            "whole_generation": whole_generation,
        }

    def _open_data_file(self) -> int:
        assert self._path is not None
        try:
            self._path.lstat()
            created = False
        except FileNotFoundError:
            created = True
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | os.O_CLOEXEC
            | getattr(os, "O_NONBLOCK", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, 0o640)
        try:
            self._validate_file(descriptor, "rotation audit path")
            os.set_blocking(descriptor, True)
            if created:
                self._sync_directory(self._path.parent)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _rotate_if_needed(self, descriptor: int, incoming: int) -> int:
        assert self._path is not None
        if self._max_bytes < AUDIT_MAX_RECORD_BYTES:
            raise RotationError("rotation audit size bound is below one record")
        size = os.fstat(descriptor).st_size
        if size == 0 or size + incoming <= self._max_bytes:
            return descriptor
        os.fsync(descriptor)
        os.close(descriptor)
        self._shift_backups(self._path)
        target = self._path.with_name(f"{self._path.name}.1")
        os.replace(self._path, target)
        self._sync_directory(self._path.parent)
        return self._open_data_file()

    def emit(self, event: str, **fields: object) -> None:
        record = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print(line, flush=True)
        if self._path is None:
            return
        payload = (line + "\n").encode("utf-8")
        if len(payload) > AUDIT_MAX_RECORD_BYTES:
            raise RotationError("rotation audit record exceeds schema bound")
        self._path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        lock_path = self._path.with_name(f".{self._path.name}.lock")
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            self._validate_file(lock_fd, "rotation audit lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            descriptor = self._open_data_file()
            try:
                repair = self._quarantine_tail(descriptor, os.fstat(descriptor).st_size)
                if repair is not None and repair["whole_generation"]:
                    os.close(descriptor)
                    descriptor = self._open_data_file()
                repair_payload = b""
                if repair is not None:
                    repair_payload = (
                        json.dumps(
                            {
                                "schema_version": AUDIT_SCHEMA_VERSION,
                                "ts": datetime.now(UTC).isoformat(),
                                "event": "rotation_audit_tail_quarantined",
                                **repair,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                descriptor = self._rotate_if_needed(
                    descriptor, len(repair_payload) + len(payload)
                )
                if repair_payload:
                    self._write_all(descriptor, repair_payload)
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(lock_fd)


_RECOVERABLE_PHASES = frozenset(
    {
        "draining",
        "gates_closed",
        "mutation_started",
        "database_rotated",
        "credential_committed",
        "activating",
    }
)


@dataclass(frozen=True, slots=True)
class RotationPhase:
    phase: str
    recovery_file: Path | None = None


class PhaseJournal:
    """Atomic, fsynced recovery state for the privileged drain boundary."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RotationPhase | None:
        try:
            fd = os.open(
                self.path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RotationError(
                f"cannot read rotation phase journal: {error}"
            ) from error
        try:
            metadata = os.fstat(fd)
            if not stat_module.S_ISREG(metadata.st_mode):
                raise RotationError("rotation phase journal is not a regular file")
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise RotationError("rotation phase journal ownership/mode is unsafe")
            stream = os.fdopen(fd, "r", encoding="utf-8")
            fd = -1
            with stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise RotationError("rotation phase journal is unexpectedly large")
        except BaseException:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RotationError("rotation phase journal is not valid JSON") from error
        if not isinstance(document, dict) or set(document) != {
            "phase",
            "recovery_file",
        }:
            raise RotationError("rotation phase journal has an invalid shape")
        phase = document["phase"]
        recovery = document["recovery_file"]
        if phase not in _RECOVERABLE_PHASES:
            raise RotationError("rotation phase journal has an unknown phase")
        if recovery is not None and not isinstance(recovery, str):
            raise RotationError("rotation phase recovery path is invalid")
        return RotationPhase(
            phase=phase, recovery_file=Path(recovery) if recovery else None
        )

    def write(self, phase: str, recovery_file: Path | None = None) -> None:
        if phase not in _RECOVERABLE_PHASES:
            raise RotationError(f"refusing unknown rotation phase {phase!r}")
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        document = json.dumps(
            {
                "phase": phase,
                "recovery_file": str(recovery_file) if recovery_file else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with stream:
                stream.write(document + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except BaseException:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            temporary.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self._sync_directory()

    def _sync_directory(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


@dataclass(frozen=True, slots=True)
class CircuitState:
    failures: int
    blocked_until: datetime | None
    last_reason: str


class CircuitJournal:
    """Crash-safe failure latch evaluated against the database clock.

    The journal never uses the host wall clock to decide when a mutating retry
    is allowed. ``blocked_until`` is written only after a server timestamp has
    been proved by ``identity_current_credential`` and is compared with a later
    server timestamp from the same authority.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> CircuitState:
        try:
            fd = os.open(
                self.path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return CircuitState(0, None, "")
        except OSError as error:
            raise RotationError(
                f"cannot read rotation circuit journal: {error}"
            ) from error
        try:
            metadata = os.fstat(fd)
            if not stat_module.S_ISREG(metadata.st_mode):
                raise RotationError("rotation circuit journal is not a regular file")
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise RotationError("rotation circuit journal ownership/mode is unsafe")
            stream = os.fdopen(fd, "r", encoding="utf-8")
            fd = -1
            with stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise RotationError("rotation circuit journal is unexpectedly large")
        except BaseException:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RotationError("rotation circuit journal is not valid JSON") from error
        if not isinstance(document, dict) or set(document) != {
            "failures",
            "blocked_until",
            "last_reason",
        }:
            raise RotationError("rotation circuit journal has an invalid shape")
        failures = document["failures"]
        blocked_raw = document["blocked_until"]
        reason = document["last_reason"]
        if (
            not isinstance(failures, int)
            or isinstance(failures, bool)
            or failures < 0
            or failures > 1_000_000
            or not isinstance(reason, str)
            or len(reason) > 128
            or (blocked_raw is not None and not isinstance(blocked_raw, str))
        ):
            raise RotationError("rotation circuit journal has invalid values")
        blocked_until: datetime | None = None
        if blocked_raw is not None:
            try:
                blocked_until = datetime.fromisoformat(blocked_raw)
            except ValueError as error:
                raise RotationError(
                    "rotation circuit journal has an invalid deadline"
                ) from error
            if blocked_until.tzinfo is None:
                raise RotationError("rotation circuit deadline is not timezone-aware")
        return CircuitState(failures, blocked_until, reason)

    def write(self, state: CircuitState) -> None:
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        document = json.dumps(
            {
                "failures": state.failures,
                "blocked_until": (
                    state.blocked_until.isoformat() if state.blocked_until else None
                ),
                "last_reason": state.last_reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with stream:
                stream.write(document + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except BaseException:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            temporary.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self._sync_directory()

    def _sync_directory(self) -> None:
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class SubprocessSystemd:
    """Talks to systemd only through the root-owned rotation helper.

    The helper (deploy/voyn-aicc-rotation-helper, installed at
    /usr/local/sbin) validates the verb and the unit against the root-owned
    lane registry and then execs the exact systemctl argv itself; sudo grants
    the rotator nothing but that helper. This process therefore sends
    `<verb> <unit>` pairs, never raw systemctl arguments.
    """

    def __init__(
        self,
        command: tuple[str, ...] = (
            "sudo",
            "-n",
            "/usr/local/sbin/voyn-aicc-rotation-helper",
        ),
    ) -> None:
        self._command = command

    def _run(self, *arguments: str, timeout: float = 30.0) -> str:
        try:
            result = subprocess.run(
                [*self._command, *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RotationError(
                f"systemctl {' '.join(arguments)} failed: {error}"
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RotationError(
                f"systemctl {' '.join(arguments)} returned {result.returncode}: {detail}"
            )
        return result.stdout

    def state(self, unit: str) -> UnitState:
        output = self._run("show", unit)
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        try:
            pid = int(fields.get("MainPID", "0"))
        except ValueError as error:
            raise RotationError(f"{unit}: invalid MainPID") from error
        return UnitState(
            active=fields.get("ActiveState", ""),
            sub=fields.get("SubState", ""),
            status=fields.get("StatusText", ""),
            main_pid=pid,
        )

    def drain(self, unit: str) -> None:
        self._run("drain", unit)

    def reload(self, unit: str, timeout: float) -> None:
        self._run("reload", unit, timeout=timeout)

    def reload_many(self, units: tuple[str, ...], timeout: float) -> None:
        if not units:
            return
        # The root-owned helper validates every argument against the same lane
        # registry before issuing one systemd transaction. systemd starts the
        # reload jobs concurrently, so the wall-clock safety budget is constant
        # instead of growing by one full timeout per lane.
        self._run("reload", *units, timeout=timeout)

    def restart(self, unit: str, timeout: float) -> None:
        self._run("restart", unit, timeout=timeout)


class PsycopgCredentialAuthority:
    @staticmethod
    def _connect(config: PostgresConfig):
        import psycopg

        return psycopg.connect(config.conninfo(), autocommit=True)

    def probe(self, config: PostgresConfig) -> None:
        with self._connect(config) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if row != (1,):
            raise RotationError("credential auth probe returned an unexpected result")

    def current_expiry(self, config: PostgresConfig) -> tuple[datetime, float]:
        with self._connect(config) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM identity_current_credential(%s)", (config.password,)
            )
            row = cursor.fetchone()
        if not row or len(row) != 3:
            raise RotationError(
                "credential expiry authority returned an invalid result"
            )
        expiry, server_now, refusal = row
        if refusal is not None:
            raise RotationError(f"credential expiry refused: {refusal}")
        if not isinstance(expiry, datetime) or not isinstance(server_now, datetime):
            raise RotationError(
                "credential expiry authority returned invalid timestamps"
            )
        if expiry.tzinfo is None or server_now.tzinfo is None:
            raise RotationError("credential expiry authority returned naive timestamps")
        return expiry, (expiry - server_now).total_seconds()

    def rotate(self, config: PostgresConfig, new_secret: str, verifier: str) -> object:
        with self._connect(config) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM enroll_rotate_self(%s,%s,%s)",
                (
                    config.password,
                    hashlib.sha256(new_secret.encode()).hexdigest(),
                    verifier,
                ),
            )
            row = cursor.fetchone()
        if not row or len(row) != 2:
            raise RotationError("credential authority returned an invalid result")
        expires, refusal = row
        if refusal is not None:
            raise RotationError(f"credential rotation refused: {refusal}")
        return expires


LANE_UNIT_PATTERN = re.compile(r"^voyn-aicc-worker@[0-9]{1,4}\.service$")


def load_lane_registry(
    path: Path, *, expected_uid: int = 0, expected_gid: int = 0
) -> tuple[str, ...]:
    """Read the root-owned worker lane registry, failing closed.

    The same file authorizes the root rotation helper, so both sides of the
    sudo boundary derive the fleet from one root-owned authority. Unsafe
    ownership/mode, symlinks, duplicates, malformed entries or an empty
    registry refuse the rotation rather than shrinking it.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RotationError(f"cannot open lane registry {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat_module.S_ISREG(info.st_mode):
            raise RotationError("lane registry is not a regular file")
        if info.st_uid != expected_uid or info.st_gid != expected_gid:
            raise RotationError("lane registry must be owned by root:root")
        if stat_module.S_IMODE(info.st_mode) & 0o022:
            raise RotationError("lane registry must not be group/other writable")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            text = handle.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)
    lanes: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not LANE_UNIT_PATTERN.fullmatch(line):
            raise RotationError(f"malformed lane registry entry: {line!r}")
        if line in lanes:
            raise RotationError(f"duplicate lane registry entry: {line!r}")
        lanes.append(line)
    if not lanes:
        raise RotationError("lane registry names no lanes")
    return tuple(lanes)


def scram_verifier(password: str, iterations: int = 4096) -> str:
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encoded_salt = base64.b64encode(salt).decode()
    stored_key = base64.b64encode(hashlib.sha256(client_key).digest()).decode()
    encoded_server_key = base64.b64encode(server_key).decode()
    return (
        f"SCRAM-SHA-256${iterations}:{encoded_salt}${stored_key}:{encoded_server_key}"
    )


def _postgres_config(values: dict[str, str]) -> PostgresConfig:
    return load_config(values)


def authority_timeout_seconds(config: PostgresConfig) -> float:
    """Upper bound for one identity-authority connect plus one statement.

    Exported so the unit-budget test derives the SAME number the controller
    enforces instead of hardcoding it (independent-review finding on
    f7515b5): a change to the connect/statement timeouts moves the safety
    proof with it.
    """
    return config.connect_timeout + config.statement_timeout_ms / 1000.0


class RotationController:
    def __init__(
        self,
        config: RotationConfig,
        systemd: Systemd,
        authority: CredentialAuthority,
        audit: Audit,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        port_probe: Callable[[str, int, float], None] | None = None,
    ) -> None:
        self.config = config
        self.systemd = systemd
        self.authority = authority
        self.audit = audit
        self.monotonic = monotonic
        self.sleep = sleep
        self.port_probe = port_probe or self._port_probe
        self.phase_journal = PhaseJournal(config.phase_file)
        self.circuit_journal = CircuitJournal(config.circuit_file)
        self._controller_deadline: float | None = None
        self._credential_deadline: float | None = None
        self._last_server_now: datetime | None = None
        self._last_server_monotonic: float | None = None
        self._restart_fallback_allowed = False

    @staticmethod
    def _authority_timeout(config: PostgresConfig) -> float:
        """Upper bound for one connect plus one statement."""
        return authority_timeout_seconds(config)

    def _load_credential_deadline(
        self, config: PostgresConfig, description: str
    ) -> tuple[datetime, float]:
        expiry, remaining = self.authority.current_expiry(config)
        self._set_credential_deadline(expiry, remaining, description)
        return expiry, remaining

    def _set_credential_deadline(
        self, expiry: datetime, remaining: float, description: str
    ) -> None:
        if expiry.tzinfo is None or not math.isfinite(remaining):
            raise RotationError(f"{description} returned an invalid expiry")
        usable = remaining - CREDENTIAL_SAFETY_MARGIN_SECONDS
        if usable <= 0:
            raise RotationError(f"{description} expires inside the safety margin")
        observed_monotonic = self.monotonic()
        self._credential_deadline = observed_monotonic + usable
        self._last_server_now = expiry - timedelta(seconds=remaining)
        self._last_server_monotonic = observed_monotonic
        self.audit.emit(
            "credential_expiry_proved",
            description=description,
            expires=expiry.isoformat(),
            remaining=remaining,
        )

    def _activation_waves(self) -> tuple[tuple[str, ...], ...]:
        """One canary, then one systemd-managed fleet transaction.

        The old fixed-size cohorts made the one-hour credential lifetime impose
        a hidden fleet ceiling (26 lanes at the deployed settings). systemd is
        already the concurrency authority for service jobs, so one validated
        multi-unit transaction scales with fleet size without spawning a Python
        thread per lane or multiplying the credential deadline by lane count.
        """
        units = self.config.worker_units
        if not units:
            return ()
        waves: list[tuple[str, ...]] = [(units[0],)]
        if len(units) > 1:
            waves.append(units[1:])
        return tuple(waves)

    def _failed_pre_mutation_attempt_budget(self, config: PostgresConfig) -> float:
        """Worst case before PostgreSQL may have accepted a new verifier.

        There is no rollback term: no claim gate is closed before mutation and
        no worker state has changed. Once ``rotate`` is called, a lost reply is
        treated as mutation and recovery is charged to the newly issued
        credential by ``_post_rotation_budget`` instead.
        """

        authority_timeout = self._authority_timeout(config)
        return 2 * self.config.prerequisite_timeout + 2 * authority_timeout

    def _retry_lifetime_budget(self, config: PostgresConfig) -> float:
        return (
            self.config.circuit_failure_threshold
            * self._failed_pre_mutation_attempt_budget(config)
            + self.config.failure_retry_window_seconds
        )

    def _server_now_estimate(self) -> datetime | None:
        if self._last_server_now is None or self._last_server_monotonic is None:
            return None
        elapsed = max(0.0, self.monotonic() - self._last_server_monotonic)
        return self._last_server_now + timedelta(seconds=elapsed)

    def _circuit_blocks(self) -> bool:
        server_now = self._server_now_estimate()
        if server_now is None:
            raise RotationError("server time is unavailable for circuit decision")
        state = self.circuit_journal.load()
        if state.failures < self.config.circuit_failure_threshold:
            return False
        blocked_until = state.blocked_until
        if blocked_until is None:
            blocked_until = server_now + timedelta(
                seconds=self.config.circuit_cooldown_seconds
            )
            state = CircuitState(state.failures, blocked_until, state.last_reason)
            self.circuit_journal.write(state)
        if server_now < blocked_until:
            self.audit.emit(
                "rotation_circuit_open",
                failures=state.failures,
                blocked_until=blocked_until.isoformat(),
                server_now=server_now.isoformat(),
            )
            return True
        self.audit.emit(
            "rotation_circuit_half_open",
            failures=state.failures,
            server_now=server_now.isoformat(),
        )
        return False

    def record_failure(self, error: Exception) -> None:
        state = self.circuit_journal.load()
        failures = state.failures + 1
        blocked_until = state.blocked_until
        if failures >= self.config.circuit_failure_threshold:
            server_now = self._server_now_estimate()
            if server_now is not None:
                blocked_until = server_now + timedelta(
                    seconds=self.config.circuit_cooldown_seconds
                )
        updated = CircuitState(failures, blocked_until, type(error).__name__)
        self.circuit_journal.write(updated)
        self.audit.emit(
            "rotation_circuit_failure_recorded",
            failures=failures,
            blocked_until=blocked_until.isoformat() if blocked_until else None,
            reason=type(error).__name__,
        )

    def _reset_circuit(self) -> None:
        state = self.circuit_journal.load()
        if state.failures:
            self.circuit_journal.clear()
            self.audit.emit("rotation_circuit_closed", prior_failures=state.failures)

    def _post_rotation_budget(self, config: PostgresConfig) -> tuple[float, bool]:
        """Return the complete activation+rollback budget and restart policy.

        Each lane may be attempted once during activation and once during
        rollback. A hot attempt is conservatively bounded as reload +
        readiness + auth probe. The extra auth probe verifies the durable file
        before lane activation starts. A restart fallback is enabled only when
        *every* attempt could take that path and the entire sequence would
        still fit the database's fixed credential lifetime.
        """
        waves = len(self._activation_waves())
        authority_timeout = self._authority_timeout(config)
        hot_budget = (
            4 * waves * self.config.reload_timeout
            + (2 * waves + 1) * authority_timeout
            + CREDENTIAL_SAFETY_MARGIN_SECONDS
        )
        # Restart fallback is intentionally only a canary operation. The fleet
        # transaction never serialises long graceful stops, and a singleton is
        # never restarted because that would create a zero-ready interval.
        restart_budget = hot_budget + 2 * self.config.restart_timeout
        # Rotation can issue the credential at any point during its bounded DB
        # operation, so reserve one more authority operation before the
        # returned expiry becomes visible to the controller.
        if SELF_CREDENTIAL_TTL_SECONDS >= authority_timeout + restart_budget:
            return restart_budget, True
        if SELF_CREDENTIAL_TTL_SECONDS >= authority_timeout + hot_budget:
            return hot_budget, False
        raise RotationError(
            "database credential TTL is below hot activation/rollback budget"
        )

    def _resume_budget(self, config: PostgresConfig) -> float:
        authority_timeout = self._authority_timeout(config)
        return len(self._activation_waves()) * (
            2 * self.config.reload_timeout + authority_timeout
        )

    def _require_current_attempt_budget(
        self, current: PostgresConfig, remaining: float, required: float
    ) -> None:
        available = self._bounded_timeout(
            required,
            "current credential pre-mutation attempt",
            credential=True,
        )
        if available < required:
            self.audit.emit(
                "rotation_deferred_current_expiry",
                remaining=remaining,
                required=required + CREDENTIAL_SAFETY_MARGIN_SECONDS,
            )
            raise RotationError(
                "current credential expires before pre-mutation attempt budget"
            )

    def _bounded_timeout(
        self, requested: float, description: str, *, credential: bool = False
    ) -> float:
        budgets = [requested]
        if self._controller_deadline is not None:
            budgets.append(self._controller_deadline - self.monotonic())
        if credential and self._credential_deadline is not None:
            budgets.append(self._credential_deadline - self.monotonic())
        timeout = min(budgets)
        if timeout <= 0:
            raise RotationError(f"no safe time budget remains for {description}")
        return timeout

    @staticmethod
    def _port_probe(host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            pass

    def _wait(
        self,
        description: str,
        timeout: float,
        predicate: Callable[[], bool],
    ) -> None:
        deadline = self.monotonic() + timeout
        delay = self.config.poll_initial
        last_error: Exception | None = None
        while self.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception as error:  # noqa: BLE001 - retained for final diagnosis
                last_error = error
            remaining = deadline - self.monotonic()
            if remaining > 0:
                self.sleep(min(delay, remaining))
            delay = min(delay * 2, self.config.poll_max)
        suffix = f": {last_error}" if last_error is not None else ""
        raise RotationError(f"timed out waiting for {description}{suffix}")

    @staticmethod
    def _healthy(state: UnitState) -> bool:
        return (
            state.active == "active"
            and state.sub == "running"
            and state.main_pid > 0
            and state.status.startswith(_READY)
        )

    def _wait_tunnel(self, *, credential: bool = False) -> None:
        def ready() -> bool:
            state = self.systemd.state(self.config.tunnel_unit)
            if state.active != "active" or state.sub != "running":
                return False
            self.port_probe(self.config.tunnel_host, self.config.tunnel_port, 2.0)
            return True

        self._wait(
            "PostgreSQL tunnel readiness",
            self._bounded_timeout(
                self.config.prerequisite_timeout,
                "PostgreSQL tunnel readiness",
                credential=credential,
            ),
            ready,
        )
        self.audit.emit("tunnel_ready", unit=self.config.tunnel_unit)

    def _wait_workers_healthy(self) -> None:
        pending = set(self.config.worker_units)
        deadline = self.monotonic() + self._bounded_timeout(
            self.config.prerequisite_timeout, "fleet readiness"
        )
        delay = self.config.poll_initial
        last_errors: dict[str, str] = {}
        while pending and self.monotonic() < deadline:
            for unit in tuple(pending):
                try:
                    if self._healthy(self.systemd.state(unit)):
                        pending.remove(unit)
                        last_errors.pop(unit, None)
                except Exception as error:  # noqa: BLE001 - final diagnosis below
                    last_errors[unit] = str(error)
            if pending:
                remaining = deadline - self.monotonic()
                if remaining > 0:
                    self.sleep(min(delay, remaining))
                delay = min(delay * 2, self.config.poll_max)
        if pending:
            details = "; ".join(
                f"{unit}: {last_errors.get(unit, 'not ready')}"
                for unit in self.config.worker_units
                if unit in pending
            )
            raise RotationError(f"worker readiness failed: {details}")

    def _activate_lane(
        self, unit: str, new_config: PostgresConfig, *, allow_restart: bool = True
    ) -> None:
        try:
            reload_timeout = self._bounded_timeout(
                self.config.reload_timeout,
                f"{unit} credential reload",
                credential=True,
            )
            self.systemd.reload(unit, reload_timeout)
            method = "reload"
        except Exception as reload_error:
            self.audit.emit("worker_reload_failed", unit=unit, error=str(reload_error))
            if not allow_restart:
                self.audit.emit("worker_restart_refused_fleet_batch", unit=unit)
                raise RotationError(
                    f"{unit}: restart fallback is outside fleet batch rollout"
                ) from reload_error
            # A restart may wait for a 3600-second job. Start it only when both
            # the controller and the newly issued credential can cover the
            # entire restart plus readiness proof. Otherwise keep this lane
            # drained and recover the remaining fleet while the secret is valid.
            if not self._restart_fallback_allowed:
                self.audit.emit(
                    "worker_restart_refused_budget",
                    unit=unit,
                    required="complete fleet activation/rollback",
                    available="credential TTL",
                )
                raise RotationError(
                    f"{unit}: restart fallback exceeds credential/controller budget"
                ) from reload_error
            required = (
                self.config.restart_timeout
                + self.config.reload_timeout
                + self._authority_timeout(new_config)
            )
            available = self._bounded_timeout(
                required, f"{unit} restart fallback", credential=True
            )
            if available < required:
                self.audit.emit(
                    "worker_restart_refused_budget",
                    unit=unit,
                    required=required,
                    available=available,
                )
                raise RotationError(
                    f"{unit}: restart fallback exceeds credential/controller budget"
                ) from reload_error
            restart_timeout = self._bounded_timeout(
                self.config.restart_timeout,
                f"{unit} restart",
                credential=True,
            )
            if restart_timeout < self.config.restart_timeout:
                raise RotationError(
                    f"{unit}: restart fallback exceeds credential/controller budget"
                ) from reload_error
            self.systemd.restart(unit, restart_timeout)
            method = "restart"

        def ready() -> bool:
            return self._healthy(self.systemd.state(unit))

        self._wait(
            f"{unit} post-rotation readiness",
            self._bounded_timeout(
                self.config.reload_timeout,
                f"{unit} post-rotation readiness",
                credential=True,
            ),
            ready,
        )
        self.authority.probe(new_config)
        self.audit.emit("worker_credential_active", unit=unit, method=method)

    def _activate_fleet(
        self,
        config: PostgresConfig,
        *,
        success_event: str | None = None,
        failure_event: str = "worker_activation_failed",
        stop_after_canary_failure: bool = False,
    ) -> list[str]:
        """Hot-reload a canary, then the remaining registry in one transaction.

        No claim gate closes for the hot path: ``replace_pool`` authenticates a
        replacement before swapping it and lets established sessions finish.
        Thus a singleton never has a zero-ready interval, while a larger fleet
        preserves canary semantics without a fleet-wide drain.
        """
        failures: list[str] = []
        for wave_number, wave in enumerate(self._activation_waves(), 1):
            self.audit.emit(
                "worker_activation_wave_started",
                wave=wave_number,
                lanes=list(wave),
            )
            errors: dict[str, Exception] = {}
            if len(wave) == 1:
                unit = wave[0]
                try:
                    self._activate_lane(
                        unit,
                        config,
                        allow_restart=(
                            wave_number == 1 and len(self.config.worker_units) > 1
                        ),
                    )
                except Exception as error:  # noqa: BLE001 - audited below
                    errors[unit] = error
            else:
                try:
                    timeout = self._bounded_timeout(
                        self.config.reload_timeout,
                        "fleet credential reload",
                        credential=True,
                    )
                    self.systemd.reload_many(wave, timeout)
                    self._wait_workers_healthy()
                    self.authority.probe(config)
                    for unit in wave:
                        self.audit.emit(
                            "worker_credential_active", unit=unit, method="reload"
                        )
                except Exception as error:  # noqa: BLE001 - audited per lane below
                    errors.update(dict.fromkeys(wave, error))
            for unit in wave:
                if unit in errors:
                    failures.append(f"{unit}: {errors[unit]}")
                    self.audit.emit(failure_event, unit=unit, error=str(errors[unit]))
                elif success_event is not None:
                    self.audit.emit(success_event, unit=unit)
            self.audit.emit(
                "worker_activation_wave_finished",
                wave=wave_number,
                lanes=list(wave),
                failures=len(errors),
            )
            if wave_number == 1 and errors and stop_after_canary_failure:
                self.audit.emit("worker_activation_fleet_deferred")
                break
        return failures

    def _resume_lanes(self, config: PostgresConfig) -> list[str]:
        """Best-effort rollback, but call a lane ready only after auth proof."""
        return self._activate_fleet(
            config,
            success_event="worker_drain_rolled_back",
            failure_event="worker_drain_rollback_failed",
        )

    def _recovery_candidates(
        self, phase: RotationPhase
    ) -> list[tuple[Path, PostgresConfig]]:
        paths = [self.config.env_file]
        if phase.recovery_file is not None:
            recovery = phase.recovery_file
            expected_parent = self.config.env_file.parent.resolve()
            if (
                recovery.parent.resolve() != expected_parent
                or not recovery.name.startswith(f".{self.config.env_file.name}.")
            ):
                raise RotationError(
                    "phase journal recovery file is outside its boundary"
                )
            paths.append(recovery)

        candidates: list[tuple[Path, PostgresConfig]] = []
        for path in paths:
            # The open that is validated must be the open that is read:
            # lstat-then-read reintroduced the TOCTOU race every other
            # journal reader here avoids (review finding on 00e5fda).
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags)
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat_module.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                ):
                    raise RotationError(
                        "rotation recovery credential ownership/mode is unsafe"
                    )
                with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                    descriptor = -1
                    text = handle.read()
                config = _postgres_config(parse_environment_text(text))
            finally:
                if descriptor != -1:
                    os.close(descriptor)
            # Dedupe by constant-time comparison against the (at most one)
            # already-collected candidate. No password-derived digest is ever
            # computed or stored: hashing a live credential with a fast hash
            # only creates a second, weaker secret representation.
            if any(
                hmac.compare_digest(config.password, existing.password)
                for _path, existing in candidates
            ):
                continue
            candidates.append((path, config))
        return candidates

    def _sweep_stale_prepared_files(self) -> None:
        """Remove secret-bearing temp files no longer named by the journal.

        Failed rotations deliberately retain their prepared file (it may be
        the only copy of a live secret); once a later rotation SUCCEEDS the
        journal no longer references older leftovers and they are dead
        plaintext accumulating at 0640 (review note on 40a08bb). Bounded and
        best-effort: only this rotator's own naming pattern, only while no
        recovery is pending.
        """
        if self.phase_journal.load() is not None:
            return
        pattern = f".{self.config.env_file.name}.*"
        for stale in sorted(self.config.env_file.parent.glob(pattern)):
            try:
                stale.unlink()
                self.audit.emit("rotation_stale_prepared_removed", file=stale.name)
            except OSError:
                continue

    def recover_interrupted(self) -> bool:
        """Reopen claim gates from a durable interrupted-rotation phase."""
        if self._controller_deadline is None:
            self._controller_deadline = (
                self.monotonic() + self.config.controller_timeout
            )
        phase = self.phase_journal.load()
        if phase is None:
            return False
        self.audit.emit("rotation_recovery_started", phase=phase.phase)
        # Recovery is also invoked by ExecStopPost during boot.  The tunnel
        # unit can be active before its forwarded socket accepts connections,
        # so keep the durable journal and wait through that bounded race before
        # deciding that neither credential is usable.
        self._wait_tunnel()
        working: list[tuple[Path, PostgresConfig, datetime, float]] = []
        for path, config in self._recovery_candidates(phase):
            try:
                expiry, remaining = self.authority.current_expiry(config)
            except Exception as error:  # noqa: BLE001 - ambiguity must be audited
                self.audit.emit(
                    "rotation_recovery_candidate_refused",
                    source="environment"
                    if path == self.config.env_file
                    else "recovery",
                    error=str(error),
                )
                continue
            working.append((path, config, expiry, remaining))
        if len(working) != 1:
            raise RotationError(
                "interrupted rotation has no unique working credential candidate"
            )

        source, config, expiry, remaining = working[0]
        if source != self.config.env_file:
            PreparedCredentialFile(
                target=self.config.env_file, temporary=source
            ).commit()
            self.audit.emit("rotation_recovery_credential_committed")
        elif phase.recovery_file is not None:
            # The old credential won: the temp file holds a generated but
            # never-enrolled plaintext secret, and clearing the phase journal
            # below would orphan it forever (review finding on 00e5fda).
            try:
                phase.recovery_file.unlink()
            except FileNotFoundError:
                pass
            self.audit.emit("rotation_recovery_stale_credential_removed")
        self._set_credential_deadline(
            expiry, remaining, "interrupted rotation credential"
        )
        self._restart_fallback_allowed = False
        failures = self._resume_lanes(config)
        if failures:
            raise RotationError(
                "interrupted rotation failed to reopen lanes: " + "; ".join(failures)
            )
        self.phase_journal.clear()
        self._reset_circuit()
        self.audit.emit("rotation_recovery_succeeded", phase=phase.phase)
        return True

    def rotate(self) -> None:
        if not self.config.worker_units:
            raise RotationError("at least one worker lane is required")
        if len(set(self.config.worker_units)) != len(self.config.worker_units):
            raise RotationError("worker units must be distinct")
        if self.config.controller_timeout <= CREDENTIAL_SAFETY_MARGIN_SECONDS:
            raise RotationError("controller timeout must exceed its safety margin")
        if self.config.circuit_failure_threshold < 1:
            raise RotationError("circuit failure threshold must be positive")
        if (
            not math.isfinite(self.config.circuit_cooldown_seconds)
            or self.config.circuit_cooldown_seconds <= 0
        ):
            raise RotationError("circuit cooldown must be positive")
        if (
            not math.isfinite(self.config.rotation_threshold_seconds)
            or self.config.rotation_threshold_seconds <= 0
        ):
            raise RotationError("rotation threshold must be positive")
        if (
            not math.isfinite(self.config.failure_retry_window_seconds)
            or self.config.failure_retry_window_seconds < 0
        ):
            raise RotationError("failure retry window must not be negative")
        self._controller_deadline = self.monotonic() + self.config.controller_timeout
        self._sweep_stale_prepared_files()
        self._credential_deadline = None
        self._last_server_now = None
        self._last_server_monotonic = None
        self._restart_fallback_allowed = False

        # A previous SIGTERM/SIGKILL is recovered as its own audited operation.
        # Do not immediately rotate the just-reloaded workers a second time.
        if self.recover_interrupted():
            return

        values = read_environment_file(self.config.env_file)
        current = _postgres_config(values)
        self._wait_tunnel()
        safe_post_rotation, restart_fallback_allowed = self._post_rotation_budget(
            current
        )
        authority_timeout = self._authority_timeout(current)
        _, current_remaining = self._load_credential_deadline(
            current, "current credential"
        )
        minimum_rotation_threshold = (
            self._retry_lifetime_budget(current) + CREDENTIAL_SAFETY_MARGIN_SECONDS
        )
        if self.config.rotation_threshold_seconds < minimum_rotation_threshold:
            raise RotationError(
                "rotation threshold is below the fleet recovery safety budget"
            )
        if self._circuit_blocks():
            # Exit non-zero so Restart=on-failure keeps checking on its bounded
            # two-minute cadence. The CLI deliberately does not record this
            # refusal as a new failure, otherwise each check would slide the
            # cooldown forever and the credential could expire before retry.
            raise CircuitOpen("rotation circuit cooldown is active")
        if current_remaining > self.config.rotation_threshold_seconds:
            self.audit.emit(
                "rotation_deferred_not_due",
                remaining=current_remaining,
                threshold=self.config.rotation_threshold_seconds,
            )
            # A complete server-authoritative preflight is a successful
            # invocation. Clear sub-threshold transient failures so the
            # durable breaker counts consecutive failures, not lifetime
            # failures accumulated across healthy timer ticks.
            self._reset_circuit()
            return
        # The threshold covers every configured failed pre-mutation attempt and
        # all systemd retry delays. No fleet state changes in that phase. After
        # PostgreSQL may have accepted a verifier, the new credential's own TTL
        # independently covers activation plus rollback.
        self._require_current_attempt_budget(
            current,
            current_remaining,
            self.config.prerequisite_timeout + 2 * authority_timeout,
        )

        remaining_protocol = (
            2 * self.config.prerequisite_timeout
            + 3 * authority_timeout
            + safe_post_rotation
        )
        if self._bounded_timeout(remaining_protocol, "complete rotation") < (
            remaining_protocol
        ):
            raise RotationError("controller budget is below complete rotation budget")
        self._wait_workers_healthy()
        # Worker readiness may legitimately consume most of its bounded wait.
        # Refresh the server-clock proof immediately before the mutation.
        _, current_remaining = self._load_credential_deadline(
            current, "pre-mutation current credential"
        )
        self._require_current_attempt_budget(
            current,
            current_remaining,
            authority_timeout,
        )
        self.audit.emit("rotation_preflight_ok", lanes=len(self.config.worker_units))

        prepared: PreparedCredentialFile | None = None
        committed = False
        database_rotated = False
        mutation_attempted = False
        # Until PostgreSQL accepts a new verifier, the old file is durable and
        # is the safe rollback source.  Between DB rotation and the atomic file
        # commit there is deliberately no safe automatic resume: reloading the
        # old file would advertise readiness with a dead credential.
        resume_config: PostgresConfig | None = current
        try:
            # The credential mutation is the point of no return. Refuse it if
            # preflight consumed so much of the controller deadline that
            # a full activation plus rollback no longer fits. This guarantees
            # systemd's outer deadline cannot kill us mid-recovery.
            mutation_budget = authority_timeout + safe_post_rotation
            controller_budget = self._bounded_timeout(
                mutation_budget, "credential mutation and recovery"
            )
            if controller_budget < mutation_budget:
                raise RotationError(
                    "controller budget exhausted before credential mutation"
                )
            current_mutation_budget = authority_timeout
            current_budget = self._bounded_timeout(
                current_mutation_budget,
                "current credential mutation rollback",
                credential=True,
            )
            if current_budget < current_mutation_budget:
                raise RotationError(
                    "current credential budget exhausted before credential mutation"
                )
            new_secret = secrets.token_hex(32)
            prepared = prepare_password_update(self.config.env_file, new_secret)
            new_values = dict(values)
            new_values["AICC_PG_PASSWORD"] = new_secret
            new_config = _postgres_config(new_values)
            mutation_attempted = True
            # From this line the OLD credential may already be dead on the
            # server even if rotate() raises (lost acknowledgement over the
            # SSH tunnel). Rolling the fleet back onto it would restart-loop
            # every lane against a verifier that no longer matches; recovery
            # must instead fall through to recover_interrupted(), which
            # probes BOTH candidates (independent-review finding on 13b7738).
            resume_config = None
            self.phase_journal.write("mutation_started", prepared.temporary)
            expires = self.authority.rotate(
                current, new_secret, scram_verifier(new_secret)
            )
            database_rotated = True
            self.phase_journal.write("database_rotated", prepared.temporary)
            try:
                prepared.commit()
            except OSError as error:
                # This file is now the only durable copy of the accepted new
                # plaintext secret. Never clean it as an ordinary temp file.
                # After os.replace() the temporary path no longer exists --
                # the new credential is already live at the target and only
                # the directory fsync failed. Point the operator at the path
                # that actually holds the secret in each window
                # (independent-review finding on 61c73e7).
                location = (
                    prepared.temporary
                    if prepared.temporary.exists()
                    else prepared.target
                )
                raise RotationError(
                    "credential file commit failed after database rotation; "
                    f"accepted credential retained at {location}"
                ) from error
            committed = True
            self.phase_journal.write("credential_committed")
            persisted = _postgres_config(read_environment_file(self.config.env_file))
            if not hmac.compare_digest(persisted.password, new_config.password):
                raise RotationError(
                    "committed credential file does not contain new secret"
                )
            expiry, remaining = self._load_credential_deadline(
                new_config, "new credential"
            )
            if remaining < safe_post_rotation:
                # Deliberately NOT a resume path (reviewed on 17ca910 and
                # kept): resume_config is still None here, so the except
                # block below neither resumes with the dead old password nor
                # activates the fleet on a credential that would expire
                # before the safe activation/rollback budget completes.
                # It re-raises, leaving the lanes drained with the phase
                # journal durable ("credential_committed") -- exactly the
                # state recover_interrupted() is built for: it re-probes
                # BOTH credential candidates and resumes on the one that
                # actually authenticates and has usable lifetime. Assigning
                # resume_config = new_config before this check would trade
                # that audited recovery for an immediate fleet activation on
                # a provably short-lived secret.
                # committed is unconditionally True on this path (the
                # commit above precedes the expiry check); the durable phase
                # is stated literally rather than via dead branches.
                assert committed and database_rotated
                durable_phase = "credential_committed"
                raise RotationError(
                    "issued credential expires before safe activation/rollback "
                    f"budget; lanes stay drained with durable phase "
                    f"{durable_phase!r} for recover_interrupted()"
                )
            resume_config = new_config
            self._restart_fallback_allowed = restart_fallback_allowed
            self.phase_journal.write("activating")
            self.audit.emit(
                "credential_rotated",
                expires=expiry.isoformat(),
                authority_result=str(expires),
            )

            failures = self._activate_fleet(new_config, stop_after_canary_failure=True)
            if failures:
                raise RotationError("; ".join(failures))
        except Exception as error:
            if resume_config is None:
                self.audit.emit(
                    "worker_resume_refused",
                    error="current database credential is not durably published",
                )
                raise
            rollback_failures = self._resume_lanes(resume_config)
            if rollback_failures:
                raise RotationError(
                    f"{error}; drain rollback failed: {'; '.join(rollback_failures)}"
                ) from error
            self.phase_journal.clear()
            raise
        finally:
            # Discard is correct only while the DB mutation has provably NOT
            # been attempted. A lost acknowledgement (network drop after the
            # server applied ALTER ROLE) leaves database_rotated False while
            # the DB already holds the new secret -- discarding the temp file
            # then destroys the only copy and locks the fleet out until a DB
            # admin intervenes (independent-review finding on fc0c391). A
            # stale unused secret file is harmless: recovery probes both
            # candidates.
            if prepared is not None and not committed and not mutation_attempted:
                prepared.discard()
        self.phase_journal.clear()
        self._reset_circuit()
        self.audit.emit("rotation_succeeded", lanes=len(self.config.worker_units))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--phase-file", type=Path, required=True)
    parser.add_argument("--circuit-file", type=Path, required=True)
    parser.add_argument("--audit-file", type=Path)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--tunnel-unit", required=True)
    parser.add_argument("--tunnel-host", default="127.0.0.1")
    parser.add_argument("--tunnel-port", type=int, default=5433)
    parser.add_argument("--lane-registry", type=Path, required=True)
    parser.add_argument("--prerequisite-timeout", type=float, default=120.0)
    parser.add_argument("--reload-timeout", type=float, default=120.0)
    parser.add_argument("--restart-timeout", type=float, default=3720.0)
    parser.add_argument("--controller-timeout", type=float, default=7200.0)
    parser.add_argument("--stop-budget", type=float, default=1140.0)
    parser.add_argument("--rotation-threshold", type=float, default=2100.0)
    parser.add_argument("--failure-retry-window", type=float, default=360.0)
    parser.add_argument("--circuit-failure-threshold", type=int, default=3)
    parser.add_argument("--circuit-cooldown", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        worker_units = load_lane_registry(args.lane_registry)
    except RotationError as error:
        Audit(args.audit_file).emit("rotation_refused", error=str(error))
        return 78
    # ExecStopPost recovery must finish inside systemd's stop window. Running
    # it under min(controller, stop) budget makes every existing bounded-
    # timeout check enforce that window; an over-large fleet fails closed with
    # the journal intact and the next timer start recovers under the full
    # controller budget.
    controller_timeout = (
        min(args.controller_timeout, args.stop_budget)
        if args.recover_only
        else args.controller_timeout
    )
    config = RotationConfig(
        env_file=args.env_file,
        lock_file=args.lock_file,
        phase_file=args.phase_file,
        circuit_file=args.circuit_file,
        audit_file=args.audit_file,
        tunnel_unit=args.tunnel_unit,
        tunnel_host=args.tunnel_host,
        tunnel_port=args.tunnel_port,
        worker_units=worker_units,
        prerequisite_timeout=args.prerequisite_timeout,
        reload_timeout=args.reload_timeout,
        restart_timeout=args.restart_timeout,
        controller_timeout=controller_timeout,
        stop_budget=args.stop_budget,
        rotation_threshold_seconds=args.rotation_threshold,
        failure_retry_window_seconds=args.failure_retry_window,
        circuit_failure_threshold=args.circuit_failure_threshold,
        circuit_cooldown_seconds=args.circuit_cooldown,
    )
    audit = Audit(config.audit_file)
    config.lock_file.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with config.lock_file.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A concurrent invocation (ExecStopPost racing a manual run) is a
            # benign overlap, not a unit failure: under Restart=on-failure a
            # non-zero code here would feed a restart loop for a condition
            # that resolves itself (independent-review finding on 61c73e7).
            audit.emit("rotation_refused", error="rotation already running")
            return 0
        controller: RotationController | None = None
        try:
            controller = RotationController(
                config, SubprocessSystemd(), PsycopgCredentialAuthority(), audit
            )
            if args.recover_only:
                controller.recover_interrupted()
            else:
                controller.rotate()
        except Exception as error:  # noqa: BLE001 - CLI boundary, audited non-zero
            if controller is not None and not isinstance(error, CircuitOpen):
                try:
                    controller.record_failure(error)
                except Exception as circuit_error:  # noqa: BLE001 - preserve primary
                    audit.emit(
                        "rotation_circuit_write_failed",
                        reason=type(circuit_error).__name__,
                    )
            audit.emit("rotation_failed", error=str(error))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
