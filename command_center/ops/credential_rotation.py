"""Rolling, drain-safe rotation for the two AICC worker lanes.

The old host-local script changed PostgreSQL's verifier and restarted both
workers immediately.  This controller instead uses an explicit protocol:

1. prove the tunnel, current credential and both lanes are healthy;
2. ask *both* lanes to close their claim gates while current jobs continue;
3. prepare the new EnvironmentFile, rotate through the authenticated session,
   and atomically publish the already-fsynced file;
4. hot-reload and authenticate one lane at a time. Existing database checkouts
   retire only when returned, so a running job and its heartbeat are not cut.

There is never a concurrent restart. A failed in-process reload may fall back
to a graceful restart whose 3660-second stop budget lets the current job
finish; the other lane is untouched until the first is ready. Every transition
is emitted as structured JSON and failures return non-zero.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from command_center.db.config import PostgresConfig, load_config
from command_center.worker.credential_file import (
    PreparedCredentialFile,
    prepare_password_update,
    read_environment_file,
)

_DRAINED = "aicc-drained"
_READY = "aicc-ready"


class RotationError(RuntimeError):
    """A fail-closed operational error safe to show in an audit event."""


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
    audit_file: Path | None
    tunnel_unit: str
    tunnel_host: str
    tunnel_port: int
    worker_units: tuple[str, ...]
    prerequisite_timeout: float = 120.0
    drain_timeout: float = 3660.0
    reload_timeout: float = 120.0
    restart_timeout: float = 3720.0
    poll_initial: float = 0.25
    poll_max: float = 5.0


class Systemd(Protocol):
    def state(self, unit: str) -> UnitState: ...

    def drain(self, unit: str) -> None: ...

    def reload(self, unit: str, timeout: float) -> None: ...

    def restart(self, unit: str, timeout: float) -> None: ...


class CredentialAuthority(Protocol):
    def probe(self, config: PostgresConfig) -> None: ...

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
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o640,
        )
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


class SubprocessSystemd:
    def __init__(
        self, command: tuple[str, ...] = ("sudo", "-n", "/usr/bin/systemctl")
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
        output = self._run(
            "show",
            unit,
            "--property=ActiveState,SubState,StatusText,MainPID",
            "--no-pager",
        )
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
        self._run("kill", "--kill-whom=main", "--signal=SIGUSR1", unit)

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

    def _wait_tunnel(self) -> None:
        def ready() -> bool:
            state = self.systemd.state(self.config.tunnel_unit)
            if state.active != "active" or state.sub != "running":
                return False
            self.port_probe(self.config.tunnel_host, self.config.tunnel_port, 2.0)
            return True

        self._wait(
            "PostgreSQL tunnel readiness", self.config.prerequisite_timeout, ready
        )
        self.audit.emit("tunnel_ready", unit=self.config.tunnel_unit)

    def _wait_workers_healthy(self) -> None:
        for unit in self.config.worker_units:

            def healthy(current_unit: str = unit) -> bool:
                return self._healthy(self.systemd.state(current_unit))

            self._wait(
                f"{unit} readiness",
                self.config.prerequisite_timeout,
                healthy,
            )

    def _drain_all(self) -> None:
        signalled: list[str] = []
        try:
            for unit in self.config.worker_units:
                self.systemd.drain(unit)
                signalled.append(unit)
                self.audit.emit("worker_drain_requested", unit=unit)
            for unit in self.config.worker_units:

                def claim_gate_closed(current_unit: str = unit) -> bool:
                    return self.systemd.state(current_unit).status == _DRAINED

                self._wait(
                    f"{unit} drain",
                    self.config.drain_timeout,
                    claim_gate_closed,
                )
                self.audit.emit("worker_claim_gate_closed", unit=unit)
        except Exception:
            # No credential changed yet. Reloading the old file is the defined
            # resume operation and prevents a partial preflight from parking a
            # healthy lane in drain forever.
            for unit in signalled:
                try:
                    self.systemd.reload(unit, self.config.reload_timeout)
                    self.audit.emit("worker_drain_rolled_back", unit=unit)
                except Exception as error:  # noqa: BLE001 - audit all recovery failures
                    self.audit.emit(
                        "worker_drain_rollback_failed", unit=unit, error=str(error)
                    )
            raise

    def _activate_lane(self, unit: str, new_config: PostgresConfig) -> None:
        try:
            self.systemd.reload(unit, self.config.reload_timeout)
            method = "reload"
        except Exception as reload_error:  # noqa: BLE001 - safe drained fallback
            self.audit.emit("worker_reload_failed", unit=unit, error=str(reload_error))
            self.systemd.restart(unit, self.config.restart_timeout)
            method = "restart"

        def ready() -> bool:
            return self._healthy(self.systemd.state(unit))

        self._wait(
            f"{unit} post-rotation readiness",
            self.config.reload_timeout,
            ready,
        )
        self.authority.probe(new_config)
        self.audit.emit("worker_credential_active", unit=unit, method=method)

    def _resume_lanes(self, config: PostgresConfig) -> list[str]:
        """Best-effort rollback, but call a lane ready only after auth proof."""
        failures: list[str] = []
        for unit in self.config.worker_units:
            try:
                self._activate_lane(unit, config)
                self.audit.emit("worker_drain_rolled_back", unit=unit)
            except Exception as error:  # noqa: BLE001 - audit every unsafe lane
                failures.append(f"{unit}: {error}")
                self.audit.emit(
                    "worker_drain_rollback_failed", unit=unit, error=str(error)
                )
        return failures

    def rotate(self) -> None:
        if len(self.config.worker_units) < 2:
            raise RotationError("at least two worker lanes are required")
        if len(set(self.config.worker_units)) != len(self.config.worker_units):
            raise RotationError("worker units must be distinct")

        self._wait_tunnel()
        values = read_environment_file(self.config.env_file)
        current = _postgres_config(values)
        self.authority.probe(current)
        self._wait_workers_healthy()
        self.audit.emit("rotation_preflight_ok", lanes=len(self.config.worker_units))
        self._drain_all()

        prepared: PreparedCredentialFile | None = None
        committed = False
        database_rotated = False
        # Until PostgreSQL accepts a new verifier, the old file is durable and
        # is the safe rollback source.  Between DB rotation and the atomic file
        # commit there is deliberately no safe automatic resume: reloading the
        # old file would advertise readiness with a dead credential.
        resume_config: PostgresConfig | None = current
        try:
            # Re-prove the prerequisite after both claim gates close; an in-flight
            # job is deliberately still running and does not delay this step.
            self._wait_tunnel()
            self.authority.probe(current)
            new_secret = secrets.token_hex(32)
            prepared = prepare_password_update(self.config.env_file, new_secret)
            new_values = dict(values)
            new_values["AICC_PG_PASSWORD"] = new_secret
            new_config = _postgres_config(new_values)
            expires = self.authority.rotate(
                current, new_secret, scram_verifier(new_secret)
            )
            database_rotated = True
            resume_config = None
            try:
                prepared.commit()
            except OSError as error:
                # This file is now the only durable copy of the accepted new
                # plaintext secret. Never clean it as an ordinary temp file.
                raise RotationError(
                    "credential file commit failed after database rotation; "
                    f"recovery file retained at {prepared.temporary}"
                ) from error
            committed = True
            resume_config = new_config
            persisted = _postgres_config(read_environment_file(self.config.env_file))
            if not hmac.compare_digest(persisted.password, new_config.password):
                raise RotationError(
                    "committed credential file does not contain new secret"
                )
            self.authority.probe(new_config)
            self.audit.emit("credential_rotated", expires=str(expires))

            failures: list[str] = []
            for unit in self.config.worker_units:
                try:
                    self._activate_lane(unit, new_config)
                except Exception as error:  # noqa: BLE001 - recover other lane first
                    failures.append(f"{unit}: {error}")
                    self.audit.emit(
                        "worker_activation_failed", unit=unit, error=str(error)
                    )
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
            raise
        finally:
            if prepared is not None and not committed and not database_rotated:
                prepared.discard()
        self.audit.emit("rotation_succeeded", lanes=len(self.config.worker_units))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--audit-file", type=Path)
    parser.add_argument("--tunnel-unit", required=True)
    parser.add_argument("--tunnel-host", default="127.0.0.1")
    parser.add_argument("--tunnel-port", type=int, default=5433)
    parser.add_argument("--worker-unit", action="append", required=True)
    parser.add_argument("--prerequisite-timeout", type=float, default=120.0)
    parser.add_argument("--drain-timeout", type=float, default=3660.0)
    parser.add_argument("--reload-timeout", type=float, default=120.0)
    parser.add_argument("--restart-timeout", type=float, default=3720.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = RotationConfig(
        env_file=args.env_file,
        lock_file=args.lock_file,
        audit_file=args.audit_file,
        tunnel_unit=args.tunnel_unit,
        tunnel_host=args.tunnel_host,
        tunnel_port=args.tunnel_port,
        worker_units=tuple(args.worker_unit),
        prerequisite_timeout=args.prerequisite_timeout,
        drain_timeout=args.drain_timeout,
        reload_timeout=args.reload_timeout,
        restart_timeout=args.restart_timeout,
    )
    audit = Audit(config.audit_file)
    config.lock_file.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with config.lock_file.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            audit.emit("rotation_refused", error="rotation already running")
            return 75
        try:
            RotationController(
                config, SubprocessSystemd(), PsycopgCredentialAuthority(), audit
            ).rotate()
        except Exception as error:  # noqa: BLE001 - CLI boundary, audited non-zero
            audit.emit("rotation_failed", error=str(error))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
