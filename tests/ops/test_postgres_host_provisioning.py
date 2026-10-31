"""Guards for the no-Docker / native-PostgreSQL host route (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

The owner decision is native install, not Docker-group membership. These checks
pin that decision into the scripts and docs, and exercise the read-only probe
against the current host without requiring sudo or package changes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "scripts" / "check_postgres_host.sh"
PROVISION = REPO_ROOT / "deploy" / "provision-postgres-host.sh"
DOC = REPO_ROOT / "docs" / "operations" / "DATABASE_HOST_PROVISIONING.md"
FOUNDATION = REPO_ROOT / "docs" / "postgres-foundation.md"


def test_decision_artifacts_exist_and_are_executable() -> None:
    assert CHECK.is_file() and os.access(CHECK, os.X_OK)
    assert PROVISION.is_file() and os.access(PROVISION, os.X_OK)
    assert DOC.is_file()


def test_decision_is_native_install_not_docker_group() -> None:
    check = CHECK.read_text()
    provision = PROVISION.read_text()
    doc = DOC.read_text()
    foundation = FOUNDATION.read_text()

    assert "VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER" in check
    assert "VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER" in provision
    assert "add `voynadmin` to the `docker` group" in doc
    assert "Do not" in doc
    assert "native install, not Docker-group membership" in doc
    assert "After=postgresql.service" in provision
    assert "AICC_POSTGRES_HOST_PROVISIONED" in provision
    assert "must run as root" in provision
    # Refuse the larger Docker-group grant as the remediation path.
    assert "usermod" not in provision
    assert "DATABASE_HOST_PROVISIONING.md" in foundation
    assert "check_postgres_host.sh" in foundation
    assert "provision-postgres-host.sh" in foundation


def test_check_script_treats_online_cluster_as_native_route() -> None:
    """systemctl alone is not enough — agent sessions often cannot reach it."""
    text = CHECK.read_text()
    assert 'pg_lsclusters -h' in text
    assert '$4 == "online"' in text
    assert "postgresql.service is active" in text
    assert "at least one PostgreSQL cluster is online" in text


def test_provision_script_is_idempotent_when_already_online() -> None:
    text = PROVISION.read_text()
    assert "already_up" in text
    assert "pg_lsclusters -h" in text
    assert '$4 == "online"' in text


def test_check_postgres_host_runs_read_only() -> None:
    result = subprocess.run(
        [str(CHECK)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "== container routes ==" in result.stdout
    assert "== native PostgreSQL server ==" in result.stdout
    assert "== verdict ==" in result.stdout
    # Never mutates the host.
    assert "apt-get" not in combined
    assert "usermod" not in combined


@pytest.mark.skipif(
    shutil.which("pg_lsclusters") is None,
    reason="pg_lsclusters not installed on this host",
)
def test_check_postgres_host_accepts_online_cluster_on_this_host() -> None:
    """On voyn-worker-01 the cluster is online even when systemctl is opaque."""
    listing = subprocess.run(
        ["pg_lsclusters", "-h"],
        capture_output=True,
        text=True,
        check=False,
    )
    has_online = any(
        parts[3] == "online"
        for line in listing.stdout.splitlines()
        if (parts := line.split()) and len(parts) >= 4
    )
    if not has_online:
        pytest.skip("no online PostgreSQL cluster on this host")

    result = subprocess.run(
        [str(CHECK)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "at least one route to a real PostgreSQL server is usable" in result.stdout
