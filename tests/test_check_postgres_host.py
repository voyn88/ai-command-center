"""scripts/check_postgres_host.sh's route-selection logic.

Each route is stubbed independently so the choice among docker/podman/harness
is pinned down without depending on what happens to be installed on the host
running the tests.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_postgres_host.sh"
TIMEOUT = 15


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _run(bin_dir: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "AICC_PG_CHECK_TIMEOUT": "2",
        **(extra_env or {}),
    }
    return subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )


@pytest.fixture()
def empty_bin(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_reports_failure_with_no_usable_route(empty_bin: Path) -> None:
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(empty_bin / "does-not-exist")})
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no usable route" in result.stderr


def test_picks_docker_when_docker_daemon_is_reachable(empty_bin: Path) -> None:
    _write_stub(empty_bin / "docker", 'if [ "$1" = info ]; then exit 0; fi\nexit 1\n')
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(empty_bin / "does-not-exist")})
    assert result.returncode == 0
    assert result.stdout.strip() == "docker"


def test_falls_back_to_podman_when_docker_is_not_usable(empty_bin: Path) -> None:
    # docker present but the daemon is unreachable -- the no-docker-group case.
    _write_stub(empty_bin / "docker", 'if [ "$1" = info ]; then exit 1; fi\nexit 1\n')
    _write_stub(empty_bin / "podman", 'if [ "$1" = info ]; then exit 0; fi\nexit 1\n')
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(empty_bin / "does-not-exist")})
    assert result.returncode == 0
    assert result.stdout.strip() == "podman"


def test_falls_back_to_harness_when_no_container_engine_works(empty_bin: Path, tmp_path: Path) -> None:
    pg_bindir = tmp_path / "pg-bin"
    pg_bindir.mkdir()
    for name in ("initdb", "pg_ctl", "postgres"):
        _write_stub(pg_bindir / name, "exit 0\n")

    _write_stub(empty_bin / "docker", 'if [ "$1" = info ]; then exit 1; fi\nexit 1\n')
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(pg_bindir)})
    assert result.returncode == 0
    assert result.stdout.strip() == "harness"


def test_harness_route_requires_all_three_binaries(empty_bin: Path, tmp_path: Path) -> None:
    partial_bindir = tmp_path / "pg-bin-partial"
    partial_bindir.mkdir()
    _write_stub(partial_bindir / "initdb", "exit 0\n")
    # pg_ctl and postgres are missing.

    result = _run(empty_bin, {"AICC_PG_BINDIR": str(partial_bindir)})
    assert result.returncode == 1
    assert result.stdout == ""


def test_docker_is_preferred_over_podman_and_harness(empty_bin: Path, tmp_path: Path) -> None:
    pg_bindir = tmp_path / "pg-bin"
    pg_bindir.mkdir()
    for name in ("initdb", "pg_ctl", "postgres"):
        _write_stub(pg_bindir / name, "exit 0\n")

    _write_stub(empty_bin / "docker", 'if [ "$1" = info ]; then exit 0; fi\nexit 1\n')
    _write_stub(empty_bin / "podman", 'if [ "$1" = info ]; then exit 0; fi\nexit 1\n')
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(pg_bindir)})
    assert result.returncode == 0
    assert result.stdout.strip() == "docker"


def test_a_hanging_docker_info_does_not_hang_the_check(empty_bin: Path) -> None:
    """`docker info` against a socket nobody answers can hang rather than
    erroring; the check must still resolve within AICC_PG_CHECK_TIMEOUT.
    """
    _write_stub(empty_bin / "docker", 'if [ "$1" = info ]; then sleep 30; fi\nexit 1\n')
    result = _run(empty_bin, {"AICC_PG_BINDIR": str(empty_bin / "does-not-exist")})
    assert result.returncode == 1
    assert "no usable route" in result.stderr
