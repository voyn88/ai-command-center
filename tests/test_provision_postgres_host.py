"""`scripts/provision_postgres_host.sh` (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

A prior version of this script printed `AICC_POSTGRES_HOST_PROVISIONED` as
soon as the launch command (`docker run` / `pg_ctl start`) exited zero --
proof only that the command was accepted, not that anything was listening
(review on https://github.com/voyn88/ai-command-center/pull/435). The
container-route tests below stub `docker` to launch "successfully" but never
answer `pg_isready`, which must still fail the whole provisioning attempt.

A later version fixed that but declared success from `docker exec ...
pg_isready` alone, which only proves the daemon answers over its in-container
Unix socket -- it stays blind to whether the published `127.0.0.1:$port`
forward the printed DSN actually depends on works at all (review on
https://github.com/voyn88/ai-command-center/pull/568). The docker stub's
`run` subcommand below binds a real listener on the host port it was asked to
publish, so a passing "becomes ready" test only passes when something is
genuinely reachable at that address, and `test_docker_container_ready_internally_but_host_port_unreachable_is_not_provisioned`
reproduces the blind spot directly: the in-container probe always succeeds
but nothing ever listens on the host's side of the forward.

Each test gets its own throwaway `scripts/` directory containing a stub
`check_postgres_host.sh` (to pin the route without depending on this host's
real Docker/Podman/PostgreSQL state) and a stub `aicc_pg_harness.sh`, plus the
real `provision_postgres_host.sh` copied in -- `REPO_ROOT` inside that script
is derived from its own path, so this reproduces the real layout without
touching the actual repo's scripts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "provision_postgres_host.sh"


def _write(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _fake_repo(tmp_path: Path, *, route_output: str, route_exit: int = 0) -> Path:
    scripts_dir = tmp_path / "repo" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, scripts_dir / "provision_postgres_host.sh")
    (scripts_dir / "provision_postgres_host.sh").chmod(0o755)

    check = scripts_dir / "check_postgres_host.sh"
    body = f'echo "{route_output}"\nexit {route_exit}' if route_output else f"exit {route_exit}"
    _write(check, body)

    _write(
        scripts_dir / "aicc_pg_harness.sh",
        (
            'case "$1" in\n'
            '  start) printf "export AICC_TEST_PG_ADMIN_DSN=%q\\n" "fake-harness-dsn" ;;\n'
            "  stop) : ;;\n"
            "esac\n"
        ),
    )
    return scripts_dir / "provision_postgres_host.sh"


def _run(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [str(script), *args], env=env, capture_output=True, text=True, check=False
    )


def test_harness_route_delegates_and_reports_provisioned(tmp_path) -> None:
    script = _fake_repo(tmp_path, route_output="AICC_POSTGRES_HOST_ROUTE=harness")
    result = _run(script, "start")
    assert result.returncode == 0
    assert "AICC_TEST_PG_ADMIN_DSN=fake-harness-dsn" in result.stdout
    assert "AICC_POSTGRES_HOST_PROVISIONED=1" in result.stdout


def test_native_route_is_refused_not_provisioned(tmp_path) -> None:
    """This script has no credentials for an operator-managed cluster."""
    script = _fake_repo(tmp_path, route_output="AICC_POSTGRES_HOST_ROUTE=native")
    result = _run(script, "start")
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout
    assert "operator-managed" in result.stderr


def test_no_usable_route_is_refused_not_provisioned(tmp_path) -> None:
    script = _fake_repo(tmp_path, route_output="", route_exit=1)
    result = _run(script, "start")
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout


def _docker_stub(ready: bool, *, listen: bool) -> str:
    """A fake `docker` whose `run` optionally binds the published host port.

    `listen` reproduces the actual `-p 127.0.0.1:$port:5432` forward: it
    parses the port out of the `-p` argument and, in the background, binds a
    real listener there for a few seconds -- long enough for the script's
    `wait_ready` loop (which runs with a 1s timeout in these tests) to
    observe it. `listen=False` reproduces the forward being broken: the
    daemon-side probe can still be told to always succeed, but nothing is
    ever reachable at the host address the DSN points at.
    """
    ready_exit = "0" if ready else "1"
    listener = (
        "hostport=\"$(printf '%s\\n' \"$@\" | grep -A1 -- '^-p$' | tail -n1 | "
        "cut -d: -f2)\"\n"
        "nohup python3 -c \"\n"
        "import socket, time\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', $hostport))\n"
        "s.listen(5)\n"
        "s.settimeout(0.2)\n"
        "deadline = time.time() + 5\n"
        "while time.time() < deadline:\n"
        "    try:\n"
        "        conn, _ = s.accept()\n"
        "        conn.close()\n"
        "    except socket.timeout:\n"
        "        pass\n"
        "s.close()\n"
        '" >/dev/null 2>&1 &\n'
        "disown\n"
        if listen
        else ""
    )
    return (
        'case "$1" in\n'
        f'  run) {listener}echo fake-container-id; exit 0 ;;\n'
        f"  exec) exit {ready_exit} ;;\n"
        "  rm) exit 0 ;;\n"
        '  *) echo "unexpected docker invocation: $*" >&2; exit 1 ;;\n'
        "esac\n"
    )


def test_docker_container_that_never_becomes_ready_is_not_provisioned(tmp_path) -> None:
    """The exact defect under review: launch succeeds, readiness never does."""
    script = _fake_repo(tmp_path, route_output="AICC_POSTGRES_HOST_ROUTE=docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=False, listen=False))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AICC_PG_PROVISION_TIMEOUT": "1",
            "AICC_PG_PROVISION_STATE": str(tmp_path / "state"),
        },
    )
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout
    assert "AICC_TEST_PG_ADMIN_DSN" not in result.stdout
    assert "never became ready" in result.stderr


def test_docker_container_ready_internally_but_host_port_unreachable_is_not_provisioned(
    tmp_path,
) -> None:
    """PR #568's defect: `docker exec ... pg_isready` always succeeds, but the
    published `127.0.0.1:$port` forward the printed DSN depends on is broken.
    """
    script = _fake_repo(tmp_path, route_output="AICC_POSTGRES_HOST_ROUTE=docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=True, listen=False))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AICC_PG_PROVISION_TIMEOUT": "1",
            "AICC_PG_PROVISION_STATE": str(tmp_path / "state"),
        },
    )
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout
    assert "AICC_TEST_PG_ADMIN_DSN" not in result.stdout
    assert "never became ready" in result.stderr


def test_docker_container_that_becomes_ready_is_provisioned(tmp_path) -> None:
    script = _fake_repo(tmp_path, route_output="AICC_POSTGRES_HOST_ROUTE=docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=True, listen=True))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AICC_PG_PROVISION_TIMEOUT": "1",
            "AICC_PG_PROVISION_STATE": str(tmp_path / "state"),
        },
    )
    assert result.returncode == 0
    assert "AICC_POSTGRES_HOST_PROVISIONED=1" in result.stdout
    assert "export AICC_TEST_PG_ADMIN_DSN=" in result.stdout
