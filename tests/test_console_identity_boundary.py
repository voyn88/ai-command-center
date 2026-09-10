"""ADR-0011's decision is a gate, not a paragraph (VOYN-W0-AICC-CONSOLE-NO-AUTH).

`tests/test_deployment_exposure.py` owns the *static* half: every launch
artifact must default to loopback. That gate is satisfied by a file that says
`localhost` — and every one of those files also documents how to override it,
because until now overriding was legal. ADR-0011 decided it is not, absent an
identity-aware reverse proxy consuming the platform identity surface
`command_center/http_auth/` already consumes.

This module owns the *runtime* half of that decision: whatever the artifacts
default to, a console that is actually reachable off-host must refuse to serve
until a separately reviewed and end-to-end verified proxy deployment exists.
The three seams that can observe the real address are covered here — the rule
itself, the container entrypoint that runs it before seeding anything, and
`app.py`, the only place an explicit `--server.address` is visible after it has
taken effect.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from command_center import console_identity

ROOT = Path(__file__).resolve().parent.parent
APP_PATH = str(ROOT / "app.py")
ENTRYPOINT = ROOT / "scripts" / "aml-entrypoint.sh"
COMPOSE = ROOT / "docker-compose.aml.yml"

# --- the rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["localhost", "127.0.0.1", "127.0.1.1", "::1", "localhost6"],
)
def test_a_loopback_reach_is_allowed(address: str) -> None:
    """The single-operator, single-host install keeps working untouched."""
    assert console_identity.check(address, env={}).address == address


@pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.10", "::", "2001:db8::1"])
def test_an_off_host_reach_is_refused_by_default(address: str) -> None:
    with pytest.raises(console_identity.ConsoleBoundaryError) as refusal:
        console_identity.check(address, env={})
    message = str(refusal.value)
    assert address in message, f"the refusal must name the address it refused: {message!r}"
    assert console_identity.ADR_PATH in message, (
        f"the refusal must point at the decision it enforces, not just say no: {message!r}"
    )


def test_proxy_claims_cannot_bypass_the_local_only_policy() -> None:
    """Environment declarations cannot prove an identity boundary exists."""
    env = {
        "AICC_CONSOLE_IDENTITY_PROXY": "https://console-proxy.internal",
        "AICC_PLATFORM_URL": "https://platform.internal",
    }
    with pytest.raises(console_identity.ConsoleBoundaryError):
        console_identity.check("0.0.0.0", env=env)


def test_the_published_address_wins_over_the_listening_one() -> None:
    """The container topology, which is correct and must stay allowed.

    `0.0.0.0` inside a private network namespace reaches nothing by itself; the
    published host interface is the exposure boundary, and it is loopback here.
    """
    env = {console_identity.PUBLISH_ADDRESS_ENV: "127.0.0.1"}
    exposure = console_identity.check("0.0.0.0", env=env)
    assert exposure.address == "127.0.0.1"
    assert exposure.source == console_identity.PUBLISH_ADDRESS_ENV


def test_a_loopback_listen_does_not_excuse_an_off_host_publish() -> None:
    """The inverse, and the reason the publish address is consulted at all."""
    env = {console_identity.PUBLISH_ADDRESS_ENV: "0.0.0.0"}
    with pytest.raises(console_identity.ConsoleBoundaryError) as refusal:
        console_identity.check("127.0.0.1", env=env)
    assert console_identity.PUBLISH_ADDRESS_ENV in str(refusal.value), (
        "the refusal must name the setting that decided the address, or the "
        "operator will edit the wrong one"
    )


def test_an_unstated_address_is_unknown_rather_than_exposed() -> None:
    """Documented residual: in-process, unstated cannot be told apart from a
    harness that binds no socket at all. The two launch paths that *can* see it
    fail closed on their own (`.streamlit/config.toml` pins loopback, the
    entrypoint refuses to start), so this seam does not guess."""
    assert console_identity.check(None, env={}).address is None


def test_the_cli_gate_exits_ex_config_on_refusal() -> None:
    """The launch scripts read the exit code, not the message."""
    completed = subprocess.run(
        [sys.executable, "-m", "command_center.console_identity", "0.0.0.0"],
        cwd=ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 78, f"expected EX_CONFIG; got {completed.returncode}"
    assert console_identity.ADR_PATH in completed.stderr


# --- the container entrypoint ------------------------------------------------


def _run_entrypoint(tmp_path: Path, env_overrides: dict[str, str]) -> tuple[int, str, str]:
    """Run the real entrypoint with seeding stubbed and the gate left real.

    `python` is stubbed because the 115-ФЗ seeding step is irrelevant here — but
    the stub delegates the boundary gate to the actual interpreter, since a stub
    that answered for the gate would make this test assert nothing.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launched = tmp_path / "streamlit-argv.txt"

    (bin_dir / "streamlit").write_text(f'#!/usr/bin/env bash\necho "$@" > "{launched}"\n')
    (bin_dir / "python").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-m" && "$2" == "command_center.console_identity" ]]; then\n'
        f'  exec "{sys.executable}" "$@"\n'
        "fi\n"
        "exit 0\n"
    )
    for stub in ("streamlit", "python"):
        (bin_dir / stub).chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "AICC_DATA_DIR": str(tmp_path / "data"),
        "STREAMLIT_SERVER_ADDRESS": "0.0.0.0",
    }
    for name in (console_identity.PUBLISH_ADDRESS_ENV,):
        env.pop(name, None)
    env.update(env_overrides)

    completed = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    argv = launched.read_text() if launched.exists() else ""
    return completed.returncode, argv, completed.stderr


def test_entrypoint_starts_when_the_port_is_published_on_loopback(tmp_path: Path) -> None:
    """The shipped compose topology: namespace-internal 0.0.0.0, loopback publish."""
    returncode, argv, stderr = _run_entrypoint(
        tmp_path, {console_identity.PUBLISH_ADDRESS_ENV: "127.0.0.1"}
    )
    assert returncode == 0, f"the supported deployment must still start: {stderr!r}"
    assert "--server.address 0.0.0.0" in argv


def test_entrypoint_refuses_an_off_host_publish_without_a_proxy(tmp_path: Path) -> None:
    """Widening `AML_BIND_HOST` is the exact act ADR-0011 declines to authorize."""
    returncode, argv, stderr = _run_entrypoint(
        tmp_path, {console_identity.PUBLISH_ADDRESS_ENV: "0.0.0.0"}
    )
    assert returncode == 78, f"expected a deliberate EX_CONFIG refusal; got {returncode}"
    assert argv == "", f"the entrypoint started streamlit anyway, with: {argv!r}"
    assert console_identity.ADR_PATH in stderr


def test_entrypoint_refuses_before_it_seeds_anything(tmp_path: Path) -> None:
    """A refused deployment must not have left state behind: the data directory
    is created by the seeding step, which the gate stands in front of."""
    _run_entrypoint(tmp_path, {console_identity.PUBLISH_ADDRESS_ENV: "0.0.0.0"})
    assert not (tmp_path / "data").exists(), "the gate ran after the seeding step"


# --- the compose file --------------------------------------------------------


def _compose_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["aml"]


def test_compose_hands_the_gate_the_address_it_publishes_on() -> None:
    """The declared publish address must be the published one, verbatim.

    Inside the container the published host interface is invisible, so the gate
    can only check what compose tells it. `tests/test_deployment_exposure.py`
    owns "every port names a host address, defaulting to loopback"; this owns
    "it is the same address the container is told about" — because if the two
    drift (a literal here, an interpolation there) the gate would clear an
    address nobody is reachable at while `ports` publishes another.
    """
    service = _compose_service()
    declared = service.get("environment", {}).get(console_identity.PUBLISH_ADDRESS_ENV)
    assert declared, (
        f"compose must pass {console_identity.PUBLISH_ADDRESS_ENV} into the container; "
        "otherwise the ADR-0011 gate inside it cannot see the exposure boundary"
    )

    for entry in service["ports"]:
        # Compared as written, not as parsed: a published port is
        # "<host>:<host-port>:<container-port>" and its host part carries a
        # `${VAR:-default}` interpolation with colons of its own, so the
        # invariant worth pinning is that the entry *begins* with exactly the
        # expression handed to the container — same variable, same default.
        assert entry.startswith(f"{declared}:"), (
            f"port {entry!r} does not publish on {declared!r}, which is the "
            "address the container's ADR-0011 gate will check"
        )


# --- app.py ------------------------------------------------------------------


def test_the_console_refuses_to_render_when_it_is_reachable_off_host(monkeypatch) -> None:
    """The seam no static check can reach: a widened bind on a *running* console.

    Asserting the absence of the shell matters more than the error text — a
    refusal that still rendered the sidebar would leave every privileged widget
    live behind a warning.
    """
    monkeypatch.setenv(console_identity.PUBLISH_ADDRESS_ENV, "0.0.0.0")

    app = AppTest.from_file(APP_PATH, default_timeout=60).run()

    assert not app.exception, f"the refusal must be a message, not a traceback: {app.exception}"
    assert app.error, "the console rendered without refusing an off-host reach"
    assert console_identity.ADR_PATH in app.error[0].value
    assert not app.sidebar.button, "privileged controls were rendered behind the refusal"


def test_the_console_renders_normally_on_loopback(monkeypatch) -> None:
    """The gate's cost to the supported install is zero — asserted, not assumed."""
    monkeypatch.setenv(console_identity.PUBLISH_ADDRESS_ENV, "127.0.0.1")

    app = AppTest.from_file(APP_PATH, default_timeout=60).run()

    assert not app.error, f"loopback must not be refused: {[e.value for e in app.error]}"
    assert app.sidebar.button, "the console did not render its controls"


def test_the_guard_runs_before_the_console_loads_anything(monkeypatch) -> None:
    """Ordering is the whole protection, so it is pinned in the source.

    `app.py` reads tasks and renders the shell at module level; a guard placed
    after either would run once the console had already touched the operator's
    data and built its controls.
    """
    source = Path(APP_PATH).read_text()
    guard = source.index("console_identity.check(")
    for later in ("tasks = load_tasks()", "shell.render_shell("):
        assert guard < source.index(later), f"the ADR-0011 guard runs after {later!r}"
