#!/usr/bin/python3
"""Lease-authority pgpass: install, provenance, and fail-closed verification.

``/etc/voyn/secrets/voyn_lease_pgpass`` is the LoadCredential= source for every
isolated worker lane's ``PGPASSFILE``. It authenticates ``voyn_lease_client``
against the control-plane lease database (``voyn_control`` on control-01),
never the worker's own ``aicc_w_*`` role against the local tunnel.

Live on worker-01 (2026-09-10 15:50 UTC) that file was overwritten with a
single ``127.0.0.1:5433:*:aicc_w_wrk_voyn_worker_01:...`` line -- the worker-DB
shape. The ``voyn_lease_client`` line vanished, no other copy existed, and
every mutating dispatch died with ``writer-lease authority refused to list
leases: password authentication failed for user voyn_lease_client``
(VOYN-W0-AICC-LEASE-PGPASS-OVERWRITTEN-ON-WORKER-01). The only tracked writer
that ever materialised that worker-DB shape onto this path was the installer
integration harness (``ops/ci/installer-integration/run.sh``); rotator and
bootstrap never owned this file and must not start. This module is the only
supported installer for the lease secret, records a non-secret provenance
sidecar the boundary verifier and rollout probe check, and refuses any
worker-role username.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LEASE_PGPASS_PATH = Path("/etc/voyn/secrets/voyn_lease_pgpass")
LEASE_PGPASS_PROVENANCE_PATH = Path(
    "/etc/voyn/secrets/voyn_lease_pgpass.provenance"
)
# Worker roles are enrolled as ``aicc_w_*``. The lease authority role is
# distinct and never shares that prefix.
WORKER_DB_USER_RE = re.compile(r"^aicc_w_")
EXPECTED_LEASE_USER = "voyn_lease_client"
PROVENANCE_KEYS = (
    "purpose",
    "pg_user",
    "pg_host",
    "pg_port",
    "pg_dbname",
    "installed_by",
    "installed_at",
)

# Test seam: production == 0 (root). Unit tests point this at the running
# user so a real file can be written without privileges; the shape under
# test is content + mode bits, not the host's uid 0.
_REQUIRED_OWNER_UID = 0
_REQUIRED_OWNER_GID = 0


class LeaseSecretError(RuntimeError):
    """The lease secret cannot be installed or verified without guessing."""


@dataclass(frozen=True, slots=True)
class LeasePgpassLine:
    host: str
    port: str
    database: str
    user: str
    # Password is retained only for install; verify never returns it.
    password: str


def parse_pgpass_line(text: str) -> LeasePgpassLine:
    """Parse one libpq pgpass line. Multi-line files are refused."""
    lines = [line for line in text.split("\n") if line and not line.startswith("#")]
    if len(lines) != 1:
        raise LeaseSecretError(
            "lease pgpass must contain exactly one credential line "
            f"(found {len(lines)})"
        )
    parts = lines[0].split(":")
    if len(parts) != 5:
        raise LeaseSecretError(
            "lease pgpass line must be host:port:database:user:password"
        )
    host, port, database, user, password = parts
    if not (host and port and user and password):
        raise LeaseSecretError("lease pgpass line is missing a required field")
    if not port.isdigit():
        raise LeaseSecretError(f"lease pgpass port is not numeric: {port!r}")
    return LeasePgpassLine(
        host=host, port=port, database=database, user=user, password=password
    )


def refuse_worker_db_user(user: str) -> None:
    """Fail closed when a worker-DB role is offered as the lease secret."""
    if WORKER_DB_USER_RE.match(user) or user.startswith("aicc_w_"):
        raise LeaseSecretError(
            f"refusing worker-DB role {user!r} as the lease secret "
            f"(expected {EXPECTED_LEASE_USER!r}; "
            "VOYN-W0-AICC-LEASE-PGPASS-OVERWRITTEN-ON-WORKER-01)"
        )
    if user != EXPECTED_LEASE_USER:
        raise LeaseSecretError(
            f"lease secret user must be {EXPECTED_LEASE_USER!r}, got {user!r}"
        )


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        fd = os.open(temporary, flags, mode)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.chmod(path, mode)


def render_provenance(
    *,
    line: LeasePgpassLine,
    installed_by: str,
    installed_at: str | None = None,
) -> str:
    refuse_worker_db_user(line.user)
    when = installed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Password never appears in provenance: the sidecar is checked by
    # non-privileged verifiers and must stay non-secret.
    return (
        "purpose=writer-lease-authority\n"
        f"pg_user={line.user}\n"
        f"pg_host={line.host}\n"
        f"pg_port={line.port}\n"
        f"pg_dbname={line.database}\n"
        f"installed_by={installed_by}\n"
        f"installed_at={when}\n"
    )


def parse_provenance(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise LeaseSecretError(f"provenance line {number}: expected key=value")
        key, _, value = raw.partition("=")
        if key in values:
            raise LeaseSecretError(f"provenance duplicates key {key!r}")
        values[key] = value
    missing = [key for key in PROVENANCE_KEYS if key not in values]
    if missing:
        raise LeaseSecretError(
            "lease secret provenance is missing: " + ", ".join(missing)
        )
    if values["purpose"] != "writer-lease-authority":
        raise LeaseSecretError(
            f"lease secret provenance purpose must be writer-lease-authority, "
            f"got {values['purpose']!r}"
        )
    refuse_worker_db_user(values["pg_user"])
    return values


def install_lease_pgpass(
    *,
    line: LeasePgpassLine,
    installed_by: str,
    pgpass_path: Path = LEASE_PGPASS_PATH,
    provenance_path: Path = LEASE_PGPASS_PROVENANCE_PATH,
    installed_at: str | None = None,
) -> None:
    """Write the lease secret and its provenance. Root-only on the host."""
    refuse_worker_db_user(line.user)
    rendered = (
        f"{line.host}:{line.port}:{line.database}:{line.user}:{line.password}\n"
    )
    provenance = render_provenance(
        line=line, installed_by=installed_by, installed_at=installed_at
    )
    _atomic_write(pgpass_path, rendered.encode(), 0o600)
    # Provenance carries no secret; 0644 lets the boundary verifier read it
    # without opening the 0600 pgpass itself when only shape is under test.
    # Production verify still opens the pgpass as root.
    _atomic_write(provenance_path, provenance.encode(), 0o644)


def _require_root_owned_secret(path: Path, *, mode_mask: int = 0o077) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_CLOEXEC)
    except OSError as exc:
        raise LeaseSecretError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LeaseSecretError(f"{path} is not a regular file")
        if info.st_uid != _REQUIRED_OWNER_UID or info.st_gid != _REQUIRED_OWNER_GID:
            raise LeaseSecretError(
                f"{path} must be owned by "
                f"uid {_REQUIRED_OWNER_UID} gid {_REQUIRED_OWNER_GID}"
            )
        if stat.S_IMODE(info.st_mode) & mode_mask:
            raise LeaseSecretError(
                f"{path} mode {stat.S_IMODE(info.st_mode):04o} allows "
                "group/other access"
            )
        return os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)


def verify_lease_secret(
    *,
    pgpass_path: Path = LEASE_PGPASS_PATH,
    provenance_path: Path = LEASE_PGPASS_PROVENANCE_PATH,
    worker_env_path: Path | None = Path(
        "/var/lib/voyn-aicc-credential-rotation/worker.env"
    ),
) -> dict[str, str]:
    """Prove the durable lease secret is not a worker-DB credential."""
    # pgpass: 0600 root:root (no group/other bits).
    raw = _require_root_owned_secret(pgpass_path, mode_mask=0o177)
    line = parse_pgpass_line(raw.decode())
    refuse_worker_db_user(line.user)
    # provenance: root:root, not group/other-writable.
    provenance = parse_provenance(
        _require_root_owned_secret(provenance_path, mode_mask=0o022).decode()
    )
    expected = {
        "pg_user": line.user,
        "pg_host": line.host,
        "pg_port": line.port,
        "pg_dbname": line.database,
    }
    for key, value in expected.items():
        if provenance[key] != value:
            raise LeaseSecretError(
                f"lease secret provenance {key}={provenance[key]!r} does not "
                f"match pgpass ({value!r})"
            )
    if worker_env_path is not None and worker_env_path.is_file():
        # Soft coupling: when worker.env is present, the lease user must not
        # equal AICC_PG_USER -- that equality is exactly the 2026-09-10
        # overwrite shape.
        try:
            text = worker_env_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for env_line in text.split("\n"):
            if env_line.startswith("AICC_PG_USER="):
                worker_user = env_line.partition("=")[2].strip()
                if worker_user and worker_user == line.user:
                    raise LeaseSecretError(
                        f"lease secret user {line.user!r} equals worker "
                        f"AICC_PG_USER; lease and worker-DB credentials must "
                        "stay distinct"
                    )
                break
    return provenance


def rotator_must_not_touch(path: Path) -> None:
    """Guard used by credential rotation: the lease secret is out of scope."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise LeaseSecretError(f"cannot resolve {path}: {exc}") from exc
    forbidden = {
        LEASE_PGPASS_PATH.resolve(),
        LEASE_PGPASS_PROVENANCE_PATH.resolve(),
    }
    # Also refuse the basename alone under any parent: a mis-pointed
    # --env-file that happens to be named voyn_lease_pgpass is the same bug.
    if resolved in forbidden or resolved.name in {
        LEASE_PGPASS_PATH.name,
        LEASE_PGPASS_PROVENANCE_PATH.name,
    }:
        raise LeaseSecretError(
            f"credential rotation must not touch the lease secret path {path} "
            "(VOYN-W0-AICC-LEASE-PGPASS-OVERWRITTEN-ON-WORKER-01)"
        )


def _install_from_args(args: argparse.Namespace) -> int:
    line = LeasePgpassLine(
        host=args.host,
        port=str(args.port),
        database=args.dbname,
        user=args.user,
        password=args.password,
    )
    install_lease_pgpass(
        line=line,
        installed_by=args.installed_by,
        pgpass_path=Path(args.pgpass_path),
        provenance_path=Path(args.provenance_path),
    )
    print(f"installed lease secret at {args.pgpass_path}")
    return 0


def _verify_from_args(args: argparse.Namespace) -> int:
    worker_env = Path(args.worker_env) if args.worker_env else None
    provenance = verify_lease_secret(
        pgpass_path=Path(args.pgpass_path),
        provenance_path=Path(args.provenance_path),
        worker_env_path=worker_env,
    )
    print(
        "LEASE_SECRET_OK "
        f"user={provenance['pg_user']} "
        f"host={provenance['pg_host']}:{provenance['pg_port']} "
        f"dbname={provenance['pg_dbname']} "
        f"installed_by={provenance['installed_by']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="write lease pgpass + provenance")
    install.add_argument("--host", required=True)
    install.add_argument("--port", type=int, required=True)
    install.add_argument("--dbname", required=True)
    install.add_argument("--user", default=EXPECTED_LEASE_USER)
    install.add_argument("--password", required=True)
    install.add_argument("--installed-by", required=True)
    install.add_argument("--pgpass-path", default=str(LEASE_PGPASS_PATH))
    install.add_argument(
        "--provenance-path", default=str(LEASE_PGPASS_PROVENANCE_PATH)
    )
    install.set_defaults(func=_install_from_args)

    verify = sub.add_parser("verify", help="fail closed on a bad lease secret")
    verify.add_argument("--pgpass-path", default=str(LEASE_PGPASS_PATH))
    verify.add_argument(
        "--provenance-path", default=str(LEASE_PGPASS_PROVENANCE_PATH)
    )
    verify.add_argument(
        "--worker-env",
        default="/var/lib/voyn-aicc-credential-rotation/worker.env",
    )
    verify.set_defaults(func=_verify_from_args)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LeaseSecretError as exc:
        print(f"LEASE_SECRET_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
