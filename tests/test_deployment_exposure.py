"""Network-exposure invariants for every launch path (VOYN-W0-AICC-STREAMLIT-EXPOSED-NO-AUTH).

The application has no authentication layer yet performs privileged git/gh and
subprocess operations, so *no* launch artifact may put it on a reachable
interface without the operator explicitly asking for that.

Four launch paths exist and each needs its own guard, because a fix applied to
one of them has already failed to protect the others:

  1. bare `streamlit run app.py`   -> `.streamlit/config.toml` pins the address
  2. `scripts/start-ui.sh`         -> injects a loopback default
  3. container entrypoint          -> `scripts/aml-entrypoint.sh`
  4. `docker compose`              -> the *published* port in the compose file

Paths 1 and 2 were hardened by an earlier audit (BLOCKER-1) but were never
covered by a test; paths 3 and 4 then reintroduced the exact same exposure.
This module owns the invariant for all four so that a regression in any one of
them fails the gate.

Scope note: these tests assert only that the surface is not *exposed*. They do
not — and cannot — assert that it is *authenticated*, because it is not. HTTP
authentication is designed separately (AUTH-HTTP-01).
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

ENTRYPOINT = ROOT / "scripts" / "aml-entrypoint.sh"
COMPOSE = ROOT / "docker-compose.aml.yml"

# `${NAME}`, `${NAME:-default}` and `${NAME-default}` as used by compose interpolation.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def _is_loopback(address: str) -> bool:
    """True when `address` can only be reached from the host itself."""
    if address in {"localhost", "localhost4", "localhost6"}:
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _resolve_defaults(value: str) -> str:
    """Interpolate a compose value the way an operator who set nothing would see it.

    An unset variable with no default renders as the empty string, which is
    exactly the fail-open case the port assertions must catch.
    """
    return _INTERPOLATION.sub(lambda m: m.group(2) or "", value)


def _published_host_address(port_spec: str) -> str | None:
    """Return the host interface a compose short-syntax port publishes on.

    `None` means the spec is unqualified — Docker then binds every interface,
    and does so by writing its own rules, bypassing a host firewall.
    """
    spec = port_spec.rsplit("/", 1)[0]  # drop an optional /tcp|/udp suffix
    if spec.startswith("["):  # [::1]:8501:8501 — bracketed IPv6 host
        host, _, _rest = spec[1:].partition("]")
        return host or None
    parts = spec.split(":")
    if len(parts) < 3:  # "8501" or "8501:8501" — no host address at all
        return None
    return parts[0] or None


def _run_entrypoint(tmp_path: Path, address: str | None) -> tuple[int, str, str]:
    """Execute the real entrypoint with `python`/`streamlit` stubbed out.

    Returns the exit code, whatever argv the stub `streamlit` was handed (empty
    when it was never reached) and stderr, so the assertions below are about
    what the script *does*, not about what it contains.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launched = tmp_path / "streamlit-argv.txt"

    (bin_dir / "streamlit").write_text(f'#!/usr/bin/env bash\necho "$@" > "{launched}"\n')
    (bin_dir / "python").write_text("#!/usr/bin/env bash\nexit 0\n")
    for stub in ("streamlit", "python"):
        (bin_dir / stub).chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AICC_DATA_DIR": str(tmp_path / "data"),
    }
    env.pop("STREAMLIT_SERVER_ADDRESS", None)
    if address is not None:
        env["STREAMLIT_SERVER_ADDRESS"] = address

    completed = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    argv = launched.read_text() if launched.exists() else ""
    return completed.returncode, argv, completed.stderr


# --- Path 1: a bare `streamlit run app.py` -----------------------------------


def test_streamlit_config_pins_a_loopback_address() -> None:
    config = (ROOT / ".streamlit" / "config.toml").read_text()
    match = re.search(r"^\s*address\s*=\s*\"([^\"]+)\"", config, re.MULTILINE)
    assert match is not None, ".streamlit/config.toml must pin [server] address"
    assert _is_loopback(match.group(1)), (
        f".streamlit/config.toml binds {match.group(1)!r}; a bare `streamlit run app.py` "
        "would then expose an unauthenticated privileged console off-host"
    )


# --- Path 2: scripts/start-ui.sh ---------------------------------------------


def test_start_ui_defaults_to_a_loopback_address() -> None:
    # Comment lines mention the flag while documenting the override, so read
    # only the executable ones.
    code = "\n".join(
        line
        for line in (ROOT / "scripts" / "start-ui.sh").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    match = re.search(r"--server\.address\s+(\S+)", code)
    assert match is not None, "start-ui.sh must inject a default --server.address"
    address = match.group(1).strip("\"'")
    assert _is_loopback(address), (
        f"start-ui.sh defaults to {address!r} instead of a loopback address"
    )


# --- Path 3: the container entrypoint ----------------------------------------


def test_entrypoint_refuses_to_start_without_an_explicit_address(tmp_path: Path) -> None:
    """No default at all: an operator cannot *forget* to choose the bind address.

    A safe default would still be a default — reachable by silently inheriting
    it. Refusing to start makes the omission impossible rather than unlikely,
    and the failure is loud, immediate and costs nothing but a restart.

    The exit code is asserted exactly, and the message with it, because "exited
    non-zero" is too weak to prove the guard exists: with `set -u` any stray
    reference to the unset variable also aborts the script. That accident would
    satisfy a `!= 0` assertion while leaving the deployment's fail-closed
    behaviour resting on an `echo` that a later edit could remove without
    noticing. `78` is EX_CONFIG and is only produced deliberately.
    """
    returncode, argv, stderr = _run_entrypoint(tmp_path, address=None)
    assert returncode == 78, (
        "entrypoint must refuse to start deliberately (exit 78) when "
        f"STREAMLIT_SERVER_ADDRESS is unset; it exited {returncode} with: {stderr!r}"
    )
    assert "STREAMLIT_SERVER_ADDRESS" in stderr, (
        f"the refusal must name the variable the operator has to set; got: {stderr!r}"
    )
    assert argv == "", f"entrypoint launched streamlit anyway, with: {argv!r}"


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0"])
def test_entrypoint_binds_exactly_the_requested_address(tmp_path: Path, address: str) -> None:
    """The operator's explicit choice is passed through verbatim, never widened."""
    returncode, argv, _stderr = _run_entrypoint(tmp_path, address=address)
    assert returncode == 0, f"entrypoint failed for an explicit address: {address}"
    assert f"--server.address {address}" in argv, (
        f"entrypoint did not bind the requested {address!r}; it ran: {argv!r}"
    )


# --- Path 4: the published port in docker compose ----------------------------


def _compose_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["aml"]


def test_compose_publishes_ports_only_on_a_loopback_default() -> None:
    """Every published port must name a host interface, defaulting to loopback.

    An unqualified `"8501:8501"` binds every interface on the host, and Docker
    installs the rule below a host firewall, so the exposure is not visible in
    the firewall's own configuration.
    """
    ports = _compose_service().get("ports", [])
    assert ports, "the compose service must declare its published ports explicitly"

    for entry in ports:
        assert isinstance(entry, str), f"unsupported long-syntax port entry: {entry!r}"
        host_address = _published_host_address(_resolve_defaults(entry))
        assert host_address is not None, (
            f"port {entry!r} is published without a host address, which binds every "
            "interface on the host"
        )
        assert _is_loopback(host_address), (
            f"port {entry!r} defaults to publishing on {host_address!r}; the default "
            "must be loopback and any wider exposure must be an explicit operator choice"
        )


def test_compose_sets_the_container_bind_address_explicitly() -> None:
    """The counterpart to the fail-closed entrypoint.

    Inside the container's own network namespace the service must listen on all
    interfaces or the published port cannot reach it. That is safe *because*
    the namespace is private and the publish above is loopback-qualified — but
    it only holds while the value is stated here, so assert it stays stated.
    """
    environment = _compose_service().get("environment", {})
    assert environment.get("STREAMLIT_SERVER_ADDRESS"), (
        "compose must set STREAMLIT_SERVER_ADDRESS explicitly; the entrypoint has no "
        "default and the service would otherwise refuse to start"
    )
