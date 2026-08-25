"""Rolling, drain-safe rotation for a registry-defined AICC worker fleet.

The old host-local script changed PostgreSQL's verifier and restarted both
workers immediately.  This controller instead uses an explicit protocol:

1. prove the tunnel, current credential and every registered lane are healthy;
2. ask every lane to close its claim gate while current jobs continue;
3. prepare the new EnvironmentFile, rotate through the authenticated session,
   and atomically publish the already-fsynced file;
4. hot-reload and authenticate a canary, then bounded cohorts. Existing database
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

_DRAINED = "aicc-drained"
_READY = "aicc-ready"

# enroll_rotate_self() grants a fixed one-hour credential. Keep this contract
# beside the protocol budget so a unit/test change cannot silently make a
# graceful 3600-second job incompatible with post-rotation recovery.
SELF_CREDENTIAL_TTL_SECONDS = 3600.0
CREDENTIAL_SAFETY_MARGIN_SECONDS = 300.0
SYSTEMD_EXIT_MARGIN_SECONDS = 600.0


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
    drain_timeout: float = 180.0
    reload_timeout: float = 120.0
    restart_timeout: float = 3720.0
    controller_timeout: float = 7200.0
    stop_budget: float = 1140.0
    activation_cohort_size: int = 8
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

    def restart(self, unit: str, timeout: float) -> None: ...


class CredentialAuthority(Protocol):
    def probe(self, config: PostgresConfig) -> None: ...

    def current_expiry(self, config: PostgresConfig) -> tuple[datetime, float]: ...

    def rotate(
        self, config: PostgresConfig, new_secret: str, verifier: str
    ) -> object: ...


class Audit:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def emit(self, event: str, **fields: object) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        print(line, flush=True)
        if self._path is None:
            return
        self._path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        # Same TOCTOU/symlink hardening as every other durable writer in this
        # module: O_NOFOLLOW refuses a symlinked audit path (which would
        # silently redirect the forensic trail), and O_NONBLOCK keeps a FIFO
        # planted at the path from blocking a synchronous emit() past the
        # rotation deadline; the fstat then requires a real regular file
        # before a byte is written (review finding on 732b765).
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | os.O_CLOEXEC
            | getattr(os, "O_NONBLOCK", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._path, flags, 0o640)
        try:
            info = os.fstat(fd)
            if not stat_module.S_ISREG(info.st_mode):
                raise RotationError("rotation audit path is not a regular file")
            # Clear O_NONBLOCK for the actual write now that the fd is proven
            # to be a regular file (nonblocking only mattered at open time).
            os.set_blocking(fd, True)
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


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
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise RotationError("rotation phase journal is unexpectedly large")
        except BaseException:
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
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(document + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except BaseException:
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
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise RotationError("rotation circuit journal is unexpectedly large")
        except BaseException:
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
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(document + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except BaseException:
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
        return config.connect_timeout + config.statement_timeout_ms / 1000.0

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
        """Canary first, then bounded cohorts from the registry order."""
        units = self.config.worker_units
        if not units:
            return ()
        cohort_size = self.config.activation_cohort_size
        if cohort_size < 1 or cohort_size > 64:
            raise RotationError("activation cohort size must be between 1 and 64")
        waves: list[tuple[str, ...]] = [(units[0],)]
        for start in range(1, len(units), cohort_size):
            waves.append(units[start : start + cohort_size])
        return tuple(waves)

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
        lanes = len(self.config.worker_units)
        waves = len(self._activation_waves())
        authority_timeout = self._authority_timeout(config)
        hot_budget = (
            4 * waves * self.config.reload_timeout
            + (2 * waves + 1) * authority_timeout
            + CREDENTIAL_SAFETY_MARGIN_SECONDS
        )
        restart_budget = hot_budget + 2 * lanes * self.config.restart_timeout
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

    def _current_pre_drain_budget(self, config: PostgresConfig) -> float:
        authority_timeout = self._authority_timeout(config)
        return (
            self.config.drain_timeout
            + self.config.prerequisite_timeout
            + 2 * authority_timeout
            + self._resume_budget(config)
        )

    def _require_current_pre_drain_budget(
        self, current: PostgresConfig, remaining: float
    ) -> None:
        required = self._current_pre_drain_budget(current)
        available = self._bounded_timeout(
            required,
            "current credential pre-drain recovery",
            credential=True,
        )
        if available < required:
            self.audit.emit(
                "rotation_deferred_current_expiry",
                remaining=remaining,
                required=required + CREDENTIAL_SAFETY_MARGIN_SECONDS,
            )
            raise RotationError(
                "current credential expires before drain/rotation recovery budget"
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
        def wait_one(unit: str) -> None:
            self._wait(
                f"{unit} readiness",
                self._bounded_timeout(
                    self.config.prerequisite_timeout, f"{unit} readiness"
                ),
                lambda: self._healthy(self.systemd.state(unit)),
            )

        for wave in self._activation_waves():
            if len(wave) == 1:
                wait_one(wave[0])
                continue
            with ThreadPoolExecutor(max_workers=len(wave)) as executor:
                futures = {executor.submit(wait_one, unit): unit for unit in wave}
                errors: dict[str, Exception] = {}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:  # noqa: BLE001 - aggregate the wave
                        errors[futures[future]] = error
                if errors:
                    raise RotationError(
                        "worker readiness failed: "
                        + "; ".join(
                            f"{unit}: {errors[unit]}" for unit in wave if unit in errors
                        )
                    )

    def _drain_all(self) -> None:
        drain_deadline = self.monotonic() + self._bounded_timeout(
            self.config.drain_timeout, "worker claim-gate drain", credential=True
        )
        for unit in self.config.worker_units:
            self.systemd.drain(unit)
            self.audit.emit("worker_drain_requested", unit=unit)
        for unit in self.config.worker_units:

            def claim_gate_closed(current_unit: str = unit) -> bool:
                return self.systemd.state(current_unit).status == _DRAINED

            self._wait(
                f"{unit} drain",
                self._bounded_timeout(
                    drain_deadline - self.monotonic(),
                    f"{unit} drain",
                    credential=True,
                ),
                claim_gate_closed,
            )
            self.audit.emit("worker_claim_gate_closed", unit=unit)

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
                self.audit.emit("worker_restart_refused_cohort", unit=unit)
                raise RotationError(
                    f"{unit}: restart fallback is serialized outside cohort rollout"
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
        """Activate a canary, then hot-reload bounded cohorts in parallel.

        Restart fallback is allowed only for the single-lane canary wave. A
        cohort never starts concurrent graceful restarts: any reload failure
        leaves that lane drained and the durable recovery path handles it.
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
                        allow_restart=wave_number == 1,
                    )
                except Exception as error:  # noqa: BLE001 - audited below
                    errors[unit] = error
            else:
                with ThreadPoolExecutor(max_workers=len(wave)) as executor:
                    futures = {
                        executor.submit(
                            self._activate_lane,
                            unit,
                            config,
                            allow_restart=False,
                        ): unit
                        for unit in wave
                    }
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as error:  # noqa: BLE001 - aggregate wave
                            errors[futures[future]] = error
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
                self.audit.emit("worker_activation_cohorts_deferred")
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
        if len(self.config.worker_units) < 2:
            raise RotationError("at least two worker lanes are required")
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
        self._credential_deadline = None
        self._last_server_now = None
        self._last_server_monotonic = None
        self._restart_fallback_allowed = False

        # A previous SIGTERM/SIGKILL is recovered as its own audited operation.
        # Do not immediately drain the just-reopened workers a second time.
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
            self._current_pre_drain_budget(current)
            + CREDENTIAL_SAFETY_MARGIN_SECONDS
            + self.config.failure_retry_window_seconds
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
        # The current credential must remain valid through a shared claim-gate
        # drain, post-drain tunnel/auth proof and the rotation statement. If any
        # one fails, enough old-credential lifetime remains to reopen and prove
        # every lane. The five-minute safety margin was already subtracted when
        # the monotonic credential deadline was installed.
        self._require_current_pre_drain_budget(current, current_remaining)

        # From this point the controller still owes per-lane health checks, one
        # shared drain, a second tunnel/expiry proof, the rotation query and the
        # complete post-rotation recovery budget.
        remaining_protocol = (
            len(self._activation_waves()) * self.config.prerequisite_timeout
            + self.config.drain_timeout
            + self.config.prerequisite_timeout
            + 3 * authority_timeout
            + safe_post_rotation
        )
        if self._bounded_timeout(remaining_protocol, "complete rotation") < (
            remaining_protocol
        ):
            raise RotationError("controller budget is below complete rotation budget")
        self._wait_workers_healthy()
        # Worker readiness may legitimately consume most of its bounded wait.
        # Refresh the server-clock proof immediately before the first gate is
        # touched, then repeat the full mutation+reopen budget check.
        _, current_remaining = self._load_credential_deadline(
            current, "pre-drain current credential"
        )
        self._require_current_pre_drain_budget(current, current_remaining)
        self.audit.emit("rotation_preflight_ok", lanes=len(self.config.worker_units))

        prepared: PreparedCredentialFile | None = None
        committed = False
        database_rotated = False
        # Until PostgreSQL accepts a new verifier, the old file is durable and
        # is the safe rollback source.  Between DB rotation and the atomic file
        # commit there is deliberately no safe automatic resume: reloading the
        # old file would advertise readiness with a dead credential.
        resume_config: PostgresConfig | None = current
        self.phase_journal.write("draining")
        try:
            self._drain_all()
            self.phase_journal.write("gates_closed")
            # Re-prove the prerequisite after all claim gates close; an in-flight
            # job is deliberately still running and does not delay this step.
            self._wait_tunnel(credential=True)
            self._load_credential_deadline(current, "post-drain current credential")
            # The credential mutation is the point of no return. Refuse it if
            # preflight/drain consumed so much of the controller deadline that
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
            current_mutation_budget = authority_timeout + self._resume_budget(current)
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
            self.phase_journal.write("mutation_started", prepared.temporary)
            expires = self.authority.rotate(
                current, new_secret, scram_verifier(new_secret)
            )
            database_rotated = True
            resume_config = None
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
                durable_phase = (
                    "credential_committed" if committed else "database_rotated"
                )
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
            if prepared is not None and not committed and not database_rotated:
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
    parser.add_argument("--drain-timeout", type=float, default=180.0)
    parser.add_argument("--reload-timeout", type=float, default=120.0)
    parser.add_argument("--restart-timeout", type=float, default=3720.0)
    parser.add_argument("--controller-timeout", type=float, default=7200.0)
    parser.add_argument("--stop-budget", type=float, default=1140.0)
    parser.add_argument("--activation-cohort-size", type=int, default=8)
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
        drain_timeout=args.drain_timeout,
        reload_timeout=args.reload_timeout,
        restart_timeout=args.restart_timeout,
        controller_timeout=controller_timeout,
        stop_budget=args.stop_budget,
        activation_cohort_size=args.activation_cohort_size,
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
