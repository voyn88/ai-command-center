"""Strict, fail-closed configuration for the AICC PostgreSQL server database.

Every value comes from the environment. There is deliberately no usable
default for host, database, user or password: a connection string that works
without the operator supplying credentials is a connection string that will
eventually point at something nobody meant it to point at. A missing or weak
value raises `ConfigError` at startup rather than degrading to a guess.

The TLS rule is the one piece of real policy here. `sslmode` defaults to
`verify-full` and the loopback exemption is narrow: only when the host is
literally a loopback address may the mode be relaxed, because that is the
supported topology for the single-host `docker-compose.server.yml` deployment
where PostgreSQL is published on 127.0.0.1 only. For any other host, a
non-verifying mode is rejected outright — `require` encrypts but authenticates
nothing, so accepting it on a routed network would leave the database open to
an active man-in-the-middle while looking configured.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

__all__ = [
    "ConfigError",
    "PostgresConfig",
    "load_config",
]

# Values that appear in tutorials, container defaults and leaked .env files.
# Rejecting them is not a substitute for a password policy; it is a guard
# against the specific failure of shipping a demo credential to a real server.
_REJECTED_PASSWORDS = frozenset(
    {
        "postgres",
        "password",
        "passwd",
        "changeme",
        "change_me",
        "secret",
        "admin",
        "root",
        "test",
        "aicc",
    }
)

_MIN_PASSWORD_LENGTH = 16

# Modes that actually verify the server certificate. `require` and below
# encrypt the transport without authenticating the peer.
_VERIFYING_SSLMODES = frozenset({"verify-ca", "verify-full"})
_KNOWN_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable, safe database."""


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """Validated connection settings. Construct via `load_config()`."""

    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str
    sslrootcert: str | None
    connect_timeout: int
    application_name: str
    pool_min_size: int
    pool_max_size: int
    pool_timeout_seconds: float
    statement_timeout_ms: int

    @property
    def is_loopback(self) -> bool:
        return _is_loopback(self.host)

    def conninfo(self) -> str:
        """libpq connection string. Never log the result — it holds the password."""
        parts = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "sslmode": self.sslmode,
            "connect_timeout": str(self.connect_timeout),
            "application_name": self.application_name,
            # Applied to every session from this pool; a runaway query on the
            # server database must not be able to pin a connection forever.
            "options": f"-c statement_timeout={self.statement_timeout_ms}",
        }
        if self.sslrootcert:
            parts["sslrootcert"] = self.sslrootcert
        return " ".join(f"{key}={_quote(value)}" for key, value in parts.items())

    def redacted(self) -> str:
        """Safe-to-log description: identifies the target, never the secret."""
        return (
            f"postgresql://{self.user}@{self.host}:{self.port}/{self.dbname}"
            f"?sslmode={self.sslmode}"
        )


def load_config(env: dict[str, str] | None = None) -> PostgresConfig:
    """Read and validate `AICC_PG_*` settings, or raise `ConfigError`."""
    source = os.environ if env is None else env

    host = _require(source, "AICC_PG_HOST")
    dbname = _require(source, "AICC_PG_DB")
    user = _require(source, "AICC_PG_USER")
    password = _require(source, "AICC_PG_PASSWORD")

    port = _int(source, "AICC_PG_PORT", default=5432, minimum=1, maximum=65535)
    _validate_password(password, user)

    sslmode = source.get("AICC_PG_SSLMODE", "verify-full").strip().lower()
    if sslmode not in _KNOWN_SSLMODES:
        raise ConfigError(
            f"AICC_PG_SSLMODE={sslmode!r} is not a libpq sslmode; expected one of "
            + ", ".join(sorted(_KNOWN_SSLMODES))
        )
    if sslmode not in _VERIFYING_SSLMODES and not _is_loopback(host):
        raise ConfigError(
            f"AICC_PG_SSLMODE={sslmode!r} does not verify the server certificate, "
            f"and AICC_PG_HOST={host!r} is not a loopback address. Use "
            "'verify-full' (or 'verify-ca' with a pinned root) for any host "
            "reachable over a network."
        )

    sslrootcert = source.get("AICC_PG_SSLROOTCERT") or None
    if sslmode in _VERIFYING_SSLMODES and not sslrootcert:
        # libpq would silently fall back to ~/.postgresql/root.crt, which is
        # invisible in a container and produces a confusing runtime failure.
        raise ConfigError(
            f"AICC_PG_SSLMODE={sslmode!r} requires AICC_PG_SSLROOTCERT to point at "
            "the CA bundle that signs the server certificate."
        )

    pool_min = _int(source, "AICC_PG_POOL_MIN", default=1, minimum=0, maximum=1000)
    pool_max = _int(source, "AICC_PG_POOL_MAX", default=10, minimum=1, maximum=1000)
    if pool_max < pool_min:
        raise ConfigError(
            f"AICC_PG_POOL_MAX ({pool_max}) is below AICC_PG_POOL_MIN ({pool_min})."
        )

    return PostgresConfig(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
        sslrootcert=sslrootcert,
        connect_timeout=_int(
            source, "AICC_PG_CONNECT_TIMEOUT", default=10, minimum=1, maximum=300
        ),
        application_name=source.get("AICC_PG_APPLICATION_NAME", "aicc") or "aicc",
        pool_min_size=pool_min,
        pool_max_size=pool_max,
        pool_timeout_seconds=float(
            _int(source, "AICC_PG_POOL_TIMEOUT", default=10, minimum=1, maximum=300)
        ),
        statement_timeout_ms=_int(
            source,
            "AICC_PG_STATEMENT_TIMEOUT_MS",
            default=30_000,
            minimum=1_000,
            maximum=3_600_000,
        ),
    )


def _require(source, name: str) -> str:
    value = (source.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required and must not be empty.")
    return value


def _int(source, name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name}={value} is outside [{minimum}, {maximum}].")
    return value


def _validate_password(password: str, user: str) -> None:
    if password.lower() in _REJECTED_PASSWORDS:
        raise ConfigError(
            "AICC_PG_PASSWORD is a well-known default value. Generate a unique "
            "secret (for example `openssl rand -base64 32`)."
        )
    if password.lower() == user.lower():
        raise ConfigError("AICC_PG_PASSWORD must not equal AICC_PG_USER.")
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ConfigError(
            f"AICC_PG_PASSWORD must be at least {_MIN_PASSWORD_LENGTH} characters; "
            f"got {len(password)}."
        )


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    if host.startswith("/"):  # Unix-domain socket directory.
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _quote(value: str) -> str:
    """Quote a libpq keyword/value token so spaces and quotes survive."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
