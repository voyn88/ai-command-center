"""`scripts/provision_postgres_host.sh` (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "provision_postgres_host.sh"
REAL_LIB = REPO_ROOT / "scripts" / "lib"
COMPOSE = REPO_ROOT / "docker-compose.server.yml"
RUN_TIMEOUT = 30
MINIMAL_PATH = "/usr/bin:/bin"


def _write(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _published_host_port(args: list[str]) -> str | None:
    """Parse host port from docker run -p arguments (one or two argv tokens)."""
    i = 0
    while i < len(args):
        if args[i] == "-p" and i + 1 < len(args):
            mapping = args[i + 1]
            parts = mapping.split(":")
            if len(parts) >= 2:
                return parts[-2] if len(parts) >= 3 else parts[0]
            return None
        if args[i].startswith("-p") and len(args[i]) > 2:
            mapping = args[i][2:]
            parts = mapping.split(":")
            if len(parts) >= 2:
                return parts[-2] if len(parts) >= 3 else parts[0]
        i += 1
    return None


def _docker_stub(*, ready: bool, listen: bool) -> str:
    ready_exit = "0" if ready else "1"
    listener = r"""
hostport=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
  case "${args[$i]}" in
    -p)
      if [ $((i+1)) -lt ${#args[@]} ]; then
        mapping="${args[$((i+1))]}"
        IFS=: read -r _ hostport _ <<< "${mapping}"
        i=$((i+2))
        continue
      fi
      ;;
    -p*)
      mapping="${args[$i]#-p}"
      IFS=: read -r _ hostport _ <<< "${mapping}"
      ;;
  esac
  i=$((i+1))
done
if [ -n "${hostport}" ]; then
  python3 - "${hostport}" <<'PY' >/dev/null 2>&1 &
import socket, sys, time
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(5)
s.settimeout(0.2)
deadline = time.time() + 8
while time.time() < deadline:
    try:
        conn, _ = s.accept()
        conn.close()
    except socket.timeout:
        pass
s.close()
PY
  disown 2>/dev/null || true
fi
"""
    if not listen:
        listener = ""
    return (
        'case "$1" in\n'
        f'  run) {listener}echo fake-container-id; exit 0 ;;\n'
        f"  exec) exit {ready_exit} ;;\n"
        "  rm) exit 0 ;;\n"
        "  ps) exit 0 ;;\n"
        '  *) echo "unexpected docker invocation: $*" >&2; exit 1 ;;\n'
        "esac\n"
    )


def _fake_repo(tmp_path: Path, *, route_output: str, route_exit: int = 0) -> Path:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir(parents=True)

    shutil.copy(REAL_SCRIPT, scripts_dir / "provision_postgres_host.sh")
    (scripts_dir / "provision_postgres_host.sh").chmod(0o755)
    for lib_file in REAL_LIB.glob("*.sh"):
        shutil.copy(lib_file, lib_dir / lib_file.name)
        (lib_dir / lib_file.name).chmod(0o755)

    shutil.copy(COMPOSE, repo / "docker-compose.server.yml")

    route = scripts_dir / "select_postgres_host_route.sh"
    if route_output:
        _write(route, f'echo "{route_output}"\nexit {route_exit}')
    else:
        _write(route, f"exit {route_exit}")

    _write(
        scripts_dir / "aicc_pg_harness.sh",
        (
            'case "$1" in\n'
            '  start) echo "postgres://postgres:secret@127.0.0.1:59999/postgres?sslmode=disable" ;;\n'
            "  stop) : ;;\n"
            "  status) exit 1 ;;\n"
            "esac\n"
        ),
    )
    return scripts_dir / "provision_postgres_host.sh"


def _run(
    script: Path,
    *args: str,
    env_extra: dict | None = None,
    timeout: float = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_harness_route_delegates_and_reports_provisioned(tmp_path: Path) -> None:
    script = _fake_repo(tmp_path, route_output="harness")
    state = tmp_path / "state"
    result = _run(
        script,
        "start",
        env_extra={"AICC_PG_PROVISION_STATE_DIR": str(state)},
    )
    assert result.returncode == 0
    assert "AICC_POSTGRES_HOST_DSN=postgres://" in result.stdout
    assert "AICC_POSTGRES_HOST_PROVISIONED=1" in result.stdout


def test_no_usable_route_is_refused_not_provisioned(tmp_path: Path) -> None:
    script = _fake_repo(tmp_path, route_output="", route_exit=1)
    result = _run(script, "start")
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout


def test_docker_container_that_never_becomes_ready_is_not_provisioned(tmp_path: Path) -> None:
    script = _fake_repo(tmp_path, route_output="docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=False, listen=False))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{MINIMAL_PATH}",
            "AICC_PG_PROVISION_TIMEOUT": "2",
            "AICC_PG_PROVISION_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout
    assert "never became reachable" in result.stderr


def test_docker_container_ready_internally_but_host_port_unreachable_is_not_provisioned(
    tmp_path: Path,
) -> None:
    script = _fake_repo(tmp_path, route_output="docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=True, listen=False))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{MINIMAL_PATH}",
            "AICC_PG_PROVISION_TIMEOUT": "2",
            "AICC_PG_PROVISION_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert result.returncode != 0
    assert "AICC_POSTGRES_HOST_PROVISIONED" not in result.stdout
    assert "never became reachable" in result.stderr


def test_docker_container_that_becomes_ready_is_provisioned(tmp_path: Path) -> None:
    script = _fake_repo(tmp_path, route_output="docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(bin_dir / "docker", _docker_stub(ready=True, listen=True))

    result = _run(
        script,
        "start",
        env_extra={
            "PATH": f"{bin_dir}:{MINIMAL_PATH}",
            "AICC_PG_PROVISION_TIMEOUT": "15",
            "AICC_PG_PROVISION_STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "AICC_POSTGRES_HOST_PROVISIONED=1" in result.stdout
    assert "AICC_POSTGRES_HOST_DSN=postgres://" in result.stdout


def test_provision_start_respects_external_timeout(tmp_path: Path) -> None:
    """If wait_ready regresses and never returns, the test harness must fail fast."""
    script = _fake_repo(tmp_path, route_output="docker")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write(
        bin_dir / "docker",
        'case "$1" in run) echo id; exit 0 ;; exec) sleep 60 ;; esac\n',
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _run(
            script,
            "start",
            env_extra={
                "PATH": f"{bin_dir}:{MINIMAL_PATH}",
                "AICC_PG_PROVISION_TIMEOUT": "60",
                "AICC_PG_PROVISION_STATE_DIR": str(tmp_path / "state"),
            },
            timeout=5,
        )