"""`scripts/check_postgres_host.sh` route detection (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

Every route is exercised through stub binaries that fully shadow `docker`,
`podman`, `pg_lsclusters` and `pg_isready` on `PATH`, rather than relying on
any of them being genuinely absent. The machine running this suite routinely
has a real Docker daemon and a real, online PostgreSQL cluster (that is
exactly the "docker"/"native" case CI itself uses) -- a test that only
stubbed the tool it cared about would let that real state pick a different
route than the one under test.

The negative controls are the point: a prior version of this script declared
Docker usable from group membership alone and a native cluster usable from
`systemctl is-active postgresql` alone (review on
https://github.com/voyn88/ai-command-center/pull/435). Both shapes are
reproduced here as a *present-but-non-functional* stub, and each must be
rejected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_postgres_host.sh"

# Fails whichever subcommand a real probe would use, so an unstubbed default
# never accidentally reports usable.
_UNUSABLE = "exit 1"


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    docker: str = _UNUSABLE,
    podman: str = _UNUSABLE,
    pg_lsclusters: str = "true",  # prints nothing: no cluster line at all
    pg_isready: str = _UNUSABLE,
    server_binaries: bool = False,
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "docker", docker)
    _write_stub(bin_dir, "podman", podman)
    _write_stub(bin_dir, "pg_lsclusters", pg_lsclusters)
    _write_stub(bin_dir, "pg_isready", pg_isready)

    script_text = SCRIPT.read_text()
    if server_binaries:
        pg_root = tmp_path / "pg-server" / "16" / "bin"
        pg_root.mkdir(parents=True)
        for tool in ("initdb", "postgres"):
            (pg_root / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
            (pg_root / tool).chmod(0o755)
        glob_root = tmp_path / "pg-server" / "*" / "bin"
    else:
        # Redirected to a directory that is guaranteed empty, rather than the
        # real /usr/lib/postgresql -- this test host has the server package
        # installed for real.
        glob_root = tmp_path / "no-server-binaries" / "*" / "bin"
    script_text = script_text.replace("/usr/lib/postgresql/*/bin", str(glob_root))

    patched = tmp_path / "check_postgres_host.sh"
    patched.write_text(script_text)
    patched.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(patched)], env=env, capture_output=True, text=True, check=False
    )


def test_docker_present_but_daemon_unreachable_is_not_usable(tmp_path) -> None:
    """Group membership without a reachable daemon is exactly PR #435's bug."""
    result = _run(tmp_path, docker='if [ "$1" = info ]; then exit 1; fi\nexit 0')
    assert "AICC_POSTGRES_HOST_ROUTE=docker" not in result.stdout


def test_docker_with_a_reachable_daemon_is_usable(tmp_path) -> None:
    result = _run(tmp_path, docker="exit 0")
    assert result.returncode == 0
    assert result.stdout.strip() == "AICC_POSTGRES_HOST_ROUTE=docker"


def test_podman_binary_present_but_not_functional_is_not_usable(tmp_path) -> None:
    result = _run(tmp_path, podman='if [ "$1" = info ]; then exit 1; fi\nexit 0')
    assert "AICC_POSTGRES_HOST_ROUTE=podman" not in result.stdout


def test_podman_functional_is_usable_when_docker_is_unusable(tmp_path) -> None:
    result = _run(tmp_path, podman="exit 0")
    assert result.returncode == 0
    assert result.stdout.strip() == "AICC_POSTGRES_HOST_ROUTE=podman"


def test_native_cluster_marked_online_but_not_accepting_connections_is_not_usable(
    tmp_path,
) -> None:
    """The umbrella-unit bug: something reports "online" that cannot be reached."""
    result = _run(
        tmp_path,
        pg_lsclusters=(
            'echo "16 main 5432 online postgres '
            '/var/lib/postgresql/16/main /var/log/postgresql/x.log"'
        ),
        pg_isready="exit 2",  # 2 = no response
    )
    assert "AICC_POSTGRES_HOST_ROUTE=native" not in result.stdout


def test_native_cluster_online_and_accepting_connections_is_usable(tmp_path) -> None:
    result = _run(
        tmp_path,
        pg_lsclusters=(
            'echo "16 main 5432 online postgres '
            '/var/lib/postgresql/16/main /var/log/postgresql/x.log"'
        ),
        pg_isready="exit 0",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "AICC_POSTGRES_HOST_ROUTE=native"


def test_harness_route_used_when_only_server_binaries_are_present(tmp_path) -> None:
    """No daemon, no engine, no running cluster -- but the server package is installed."""
    result = _run(tmp_path, server_binaries=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "AICC_POSTGRES_HOST_ROUTE=harness"


def test_no_route_usable_fails_closed_with_a_remediation(tmp_path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "operator must join the docker group or install" in result.stderr


def test_priority_prefers_docker_over_every_other_route(tmp_path) -> None:
    result = _run(
        tmp_path,
        docker="exit 0",
        podman="exit 0",
        pg_lsclusters=(
            'echo "16 main 5432 online postgres '
            '/var/lib/postgresql/16/main /var/log/postgresql/x.log"'
        ),
        pg_isready="exit 0",
        server_binaries=True,
    )
    assert result.stdout.strip() == "AICC_POSTGRES_HOST_ROUTE=docker"
