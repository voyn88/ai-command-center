"""Fail-closed configuration for the OTLP ingest AICC ships telemetry to.

SRV-08 asks for metrics, logs and traces off the control host. This module
owns the question that decides whether switching that export on is safe --
*who may write into the ingest, and how does AICC prove it is that writer* --
and it admits exactly two answers::

    export is off  ->  no endpoint, no credential, no client, no cost
    export is on   ->  a credential is mandatory

There is deliberately no third state, no ``AICC_OTLP_INSECURE`` and no "warn
and continue" branch. An ingest AICC can post to without authenticating is an
open write path into the trace store, and the trace store is the instrument an
operator reads *during* an incident: forged spans can attribute work to a
worker that never ran it, and volume can bury the real incident. A configured
endpoint whose credential is missing or unreadable raises :class:`ConfigError`
at load, so the process refuses to start rather than exporting anonymously.

THE TLS RULE
============
``https`` is required, and the only exemption is a loopback host -- the same
narrow rule, for the same reason, as ``AICC_PG_SSLMODE`` in
``command_center/db/config.py``, and it matches the topology this host
actually runs: ``voyn-aicc-pgtunnel.service`` terminates an SSH tunnel on
127.0.0.1, which is how a control-plane dependency is reached here. A bearer
token sent over plaintext ``http`` to a routed address is disclosed to every
hop on the path, and unlike a password it is replayable by whoever reads it.

Note what the exemption does *not* relax: loopback still requires a
credential. The exemption is about transport confidentiality, which the tunnel
already provides, not about authentication, which nothing else provides.

WHAT ELSE THE ENDPOINT MUST NOT BE
==================================
* **Userinfo in the URL** (``https://user:pass@host``) refuses. urllib would
  turn it into a second, ambient ``Authorization`` header competing with the
  one this package builds, and it puts a secret in a value that is logged as
  configuration.
* **A query string or fragment** refuses. The OTLP/HTTP endpoint is a base URL
  that ``/v1/<signal>`` is appended to; a query on it silently produces
  ``...?x=1/v1/traces``, a 404 that reads as "the collector is down".

WHY ONLY THE SIGNAL NAMES OTLP DEFINES
======================================
:data:`SIGNALS` is closed. The path is built by interpolation, so an arbitrary
caller-supplied signal is a path-traversal parameter; and a typo'd signal that
merely 404s is another silent-telemetry-loss failure, which is the class of
bug SRV-08 exists to remove.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from dataclasses import dataclass

from command_center.otlp.credential import Credential, CredentialError

__all__ = [
    "ConfigError",
    "OtlpIngestConfig",
    "SIGNALS",
    "ENDPOINT_ENV",
    "TOKEN_FILE_ENV",
    "TIMEOUT_ENV",
    "load_config",
]

#: Base URL of the collector's OTLP/HTTP receiver, e.g.
#: ``https://collector.internal:4318``. Unset disables export entirely.
ENDPOINT_ENV = "AICC_OTLP_ENDPOINT"

#: Path to the file holding the bearer token. Required whenever the endpoint
#: is set. See :mod:`command_center.otlp.credential` for why it is a file.
TOKEN_FILE_ENV = "AICC_OTLP_TOKEN_FILE"

#: Connect + read budget for one export, in seconds.
TIMEOUT_ENV = "AICC_OTLP_TIMEOUT_SECONDS"

#: The three signals OTLP/HTTP defines, and the only path segments accepted.
SIGNALS = frozenset({"traces", "metrics", "logs"})

_DEFAULT_TIMEOUT = 10.0
_MIN_TIMEOUT = 0.1
_MAX_TIMEOUT = 120.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class ConfigError(RuntimeError):
    """The environment does not describe a usable, authenticated ingest."""


@dataclass(frozen=True, slots=True, repr=False)
class OtlpIngestConfig:
    """A validated, authenticated OTLP/HTTP ingest target.

    Construct via :func:`load_config`. ``repr`` is suppressed and written by
    hand: the credential is reachable from here, and a dataclass-generated
    repr in a traceback is the classic way a secret reaches a log file.
    """

    endpoint: str
    credential: Credential
    timeout_seconds: float

    def url_for(self, signal: str) -> str:
        """The OTLP/HTTP URL for one signal, e.g. ``<endpoint>/v1/traces``."""
        if signal not in SIGNALS:
            raise ConfigError(
                f"{signal!r} is not an OTLP signal; expected one of "
                + ", ".join(sorted(SIGNALS))
            )
        return f"{self.endpoint}/v1/{signal}"

    def auth_headers(self) -> dict[str, str]:
        """The authentication header for one request.

        Built per call rather than cached, so a rotated credential is picked
        up by the next export without a restart.
        """
        return {"Authorization": f"Bearer {self.credential.token}"}

    def redact(self, text: str) -> str:
        """Remove the token from anything on its way to a log or exception."""
        return self.credential.redact(text)

    def redacted(self) -> str:
        """Safe-to-log description: names the target, never the secret."""
        return f"{self.endpoint} (bearer credential at {self.credential.path})"

    def __repr__(self) -> str:
        return f"OtlpIngestConfig({self.redacted()})"


def load_config(env: dict[str, str] | None = None) -> OtlpIngestConfig | None:
    """Read ``AICC_OTLP_*``.

    Returns ``None`` when export is switched off, which is the default and
    costs nothing. Raises :class:`ConfigError` for every other way the
    environment can fail to describe an authenticated ingest -- including the
    half-configured cases, because "the operator meant to enable this and it
    silently stayed off" is the failure this module is here to prevent.
    """
    source = os.environ if env is None else env

    endpoint_raw = (source.get(ENDPOINT_ENV) or "").strip()
    token_file = (source.get(TOKEN_FILE_ENV) or "").strip()

    if not endpoint_raw:
        if token_file:
            raise ConfigError(
                f"{TOKEN_FILE_ENV} is set but {ENDPOINT_ENV} is not, so nothing "
                "is exported. Set the endpoint, or unset both -- a credential "
                "configured for a destination that does not exist is an "
                "operator expecting telemetry that will never arrive."
            )
        return None

    if not token_file:
        raise ConfigError(
            f"{ENDPOINT_ENV} is set but {TOKEN_FILE_ENV} is not. AICC does not "
            "export telemetry anonymously: an unauthenticated ingest is an open "
            "write path into the store an operator reads during an incident."
        )

    endpoint = _validate_endpoint(endpoint_raw)

    try:
        credential = Credential.from_path(token_file)
    except CredentialError as exc:
        # Re-raised as ConfigError so a caller loading configuration catches
        # one exception type; the credential's message is already specific and
        # already free of the token.
        raise ConfigError(str(exc)) from exc

    return OtlpIngestConfig(
        endpoint=endpoint,
        credential=credential,
        timeout_seconds=_timeout(source),
    )


def _validate_endpoint(raw: str) -> str:
    parts = urllib.parse.urlsplit(raw)

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ConfigError(
            f"{ENDPOINT_ENV}={raw!r} has scheme {scheme or '(none)'!r}; the OTLP "
            "ingest must be an http or https URL."
        )

    if not parts.hostname:
        raise ConfigError(f"{ENDPOINT_ENV}={raw!r} has no host.")

    if parts.username is not None or parts.password is not None:
        raise ConfigError(
            f"{ENDPOINT_ENV} must not carry userinfo. Credentials belong in "
            f"{TOKEN_FILE_ENV}, not in a URL that is logged as configuration."
        )

    if parts.query or parts.fragment:
        raise ConfigError(
            f"{ENDPOINT_ENV}={raw!r} must be a bare base URL: '/v1/<signal>' is "
            "appended to it, and a query or fragment would move that suffix "
            "past the path and turn every export into a 404."
        )

    if scheme == "http" and not _is_loopback(parts.hostname):
        raise ConfigError(
            f"{ENDPOINT_ENV}={raw!r} sends a bearer token over plaintext http to "
            f"a non-loopback host ({parts.hostname}). Use https, or terminate a "
            "tunnel on 127.0.0.1 and point at that."
        )

    # A trailing slash would produce '<endpoint>//v1/traces'. Collectors
    # differ on whether they normalize it; not emitting it is free.
    return f"{scheme}://{parts.netloc}{parts.path}".rstrip("/")


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name that is not an address literal. It may well resolve to
        # loopback, but resolution is not configuration: DNS can change under
        # a running process, so a name is treated as routed.
        return False


def _timeout(source) -> float:
    raw = (source.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{TIMEOUT_ENV}={raw!r} is not a number.") from exc
    if not _MIN_TIMEOUT <= value <= _MAX_TIMEOUT:
        raise ConfigError(
            f"{TIMEOUT_ENV}={value} is outside [{_MIN_TIMEOUT}, {_MAX_TIMEOUT}] "
            "seconds. An export that can hang for longer than this holds a "
            "worker thread across an incident."
        )
    return value
