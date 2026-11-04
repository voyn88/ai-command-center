"""ADR-0011's decision, enforced: the console refuses an off-host reach it cannot account for.

[ADR-0011](../docs/adr/0011-streamlit-console-identity-boundary.md) decided that
external deployment of the Streamlit console is not authorized until an
identity-aware reverse proxy stands in front of it (consuming the same AIOS
identity surface ``command_center/http_auth/identity.py`` already consumes), or
until an already-authenticated client reaches parity and remote access moves
there. Written as prose that decision protected nothing: every launch path
*defaults* to loopback, and every one of them documents how to override the
default — ``--server.address 0.0.0.0``, ``AML_BIND_HOST=0.0.0.0`` — so the
widening the ADR calls unauthorized was still a single flag away, indistinguishable
at runtime from a deliberate, reviewed exposure. This module is that sentence
made mechanical: off-loopback reach now fails closed. A future reviewed proxy
deployment must replace this local-only rule with a boundary that can be
verified end to end; a process-local declaration is not sufficient.

**What is checked is reach, not the listening socket.** The two differ in the
container, and conflating them would break the one topology that is already
correct: inside a private network namespace ``0.0.0.0`` reaches nothing by
itself, and the real exposure boundary is the address the port is *published*
on. So :func:`resolve_exposure` reads ``AICC_CONSOLE_PUBLISH_ADDRESS`` when the
launch path declares one (``docker-compose.aml.yml`` sets it from the same
``AML_BIND_HOST`` it publishes on) and falls back to the listening address when
nothing does — which is the correct answer for a bare ``streamlit run`` on a
host, where the process is reachable at exactly the interface it binds.

**No attestation bypass.** A process cannot establish that a reverse proxy is
actually in front of it, that every route passes through the proxy, or that the
proxy authenticated a principal.  An environment variable claiming that such
a proxy exists would therefore be a bypass, not an identity boundary.  Until a
real proxy deployment lands with tests for those properties, this gate always
refuses off-host reach.

**Residual, stated rather than papered over.** An unstated address is treated
as unknown, not as exposure: in-process it cannot be distinguished from a
harness that never binds a socket at all (``AppTest``), and the two launch
paths that *can* see it already fail closed — ``.streamlit/config.toml`` pins
loopback for a bare ``streamlit run`` (gated by
``tests/test_deployment_exposure.py``) and ``scripts/aml-entrypoint.sh``
refuses to start with no address at all. The case this leaves is
``streamlit run /path/to/app.py`` launched from a directory where neither that
config file nor an explicit flag applies: Streamlit then binds every interface
and reports no address, and this gate stays silent. Use ``scripts/start-ui.sh``,
which cannot produce that state.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ADR_PATH",
    "ConsoleBoundaryError",
    "Exposure",
    "LISTEN_ADDRESS_ENV",
    "PUBLISH_ADDRESS_ENV",
    "check",
    "is_loopback",
    "resolve_exposure",
]

#: The decision this module enforces. Named in every refusal: a gate that says
#: "not authorized" without saying where that was decided invites the reader to
#: treat it as an obstacle rather than a decision, and the first thing they
#: reach for is the way around it.
ADR_PATH = "docs/adr/0011-streamlit-console-identity-boundary.md"

#: Streamlit's own listening-address variable, honored by the CLI and by the
#: container entrypoint.
LISTEN_ADDRESS_ENV = "STREAMLIT_SERVER_ADDRESS"

#: The host interface the console is actually published on, when the launch
#: path knows it and the listening address does not answer the question — i.e.
#: the container, whose listening address is namespace-internal.
PUBLISH_ADDRESS_ENV = "AICC_CONSOLE_PUBLISH_ADDRESS"

#: Exit code for a refusal on the CLI path — ``EX_CONFIG``, the same deliberate
#: code ``scripts/aml-entrypoint.sh`` already uses for its own fail-closed
#: refusal, so a wrapper can tell a policy refusal from a crash.
EX_CONFIG = 78


class ConsoleBoundaryError(RuntimeError):
    """The console would be reachable off-host with nothing authenticating it."""


@dataclass(frozen=True)
class Exposure:
    """Where the console can be reached, and which setting decided that."""

    #: ``None`` when no launch path stated an address (see the module docstring:
    #: unknown, deliberately not treated as exposure).
    address: str | None
    #: The environment variable the address came from, for the refusal message.
    source: str | None

    @property
    def off_host(self) -> bool:
        """True only for a *stated* address that is not loopback."""
        return self.address is not None and not is_loopback(self.address)


def is_loopback(address: str) -> bool:
    """True when `address` can only be reached from the host itself.

    Deliberately the same rule as ``tests/test_deployment_exposure.py`` applies
    to the launch artifacts: the runtime gate and the static gate must agree on
    what "off-host" means, or one of them is decorative.
    """
    if address in {"localhost", "localhost4", "localhost6", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _stated(source: Mapping[str, str], name: str) -> str | None:
    """A variable set to whitespace is unset: compose renders an unset
    interpolation as the empty string, which must not read as an address."""
    value = source.get(name, "").strip()
    return value or None


def resolve_exposure(
    listen_address: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Exposure:
    """Where the console is reachable from, given how it was launched.

    `listen_address` is what the process itself binds — Streamlit's
    ``server.address`` in-process, ``$STREAMLIT_SERVER_ADDRESS`` on the CLI
    path. A declared publish address wins over it, because when the two differ
    the publish address is the one an outside caller can dial.
    """
    source = _env(env)
    published = _stated(source, PUBLISH_ADDRESS_ENV)
    if published is not None:
        return Exposure(address=published, source=PUBLISH_ADDRESS_ENV)

    listening = listen_address.strip() if isinstance(listen_address, str) else None
    if listening:
        return Exposure(address=listening, source=LISTEN_ADDRESS_ENV)
    return Exposure(address=_stated(source, LISTEN_ADDRESS_ENV), source=LISTEN_ADDRESS_ENV)


def check(
    listen_address: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Exposure:
    """Return where the console is reachable, or refuse.

    Raises `ConsoleBoundaryError` when that reach is off-host under the current
    local-only deployment policy. There is deliberately no environment variable
    that can attest a proxy into existence.
    """
    exposure = resolve_exposure(listen_address, env)
    if not exposure.off_host:
        return exposure

    raise ConsoleBoundaryError(
        f"Refusing to serve: the console would be reachable at {exposure.address!r} "
        f"(from {exposure.source}), which is not a loopback address. It performs "
        "privileged git/gh and subprocess operations and has no authentication of "
        f"its own, so off-host reach requires the identity-aware reverse proxy "
        f"decided in {ADR_PATH}. Keep Streamlit on loopback. Remote access may only "
        "be enabled by a future change that implements and verifies that boundary "
        "or retires remote Streamlit in favor of an authenticated client."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI gate for the launch scripts: silent when allowed, `EX_CONFIG` when not.

    Usage: ``python -m command_center.console_identity [LISTEN_ADDRESS]``. With
    no argument the listening address comes from the environment, which is how
    ``scripts/aml-entrypoint.sh`` calls it.
    """
    args = sys.argv[1:] if argv is None else argv
    listen_address = args[0] if args else None
    try:
        check(listen_address)
    except ConsoleBoundaryError as exc:
        print(f"[console] FATAL: {exc}", file=sys.stderr)
        return EX_CONFIG
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the entrypoint
    raise SystemExit(main())
