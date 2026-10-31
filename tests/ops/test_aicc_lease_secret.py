"""Lease-secret install/verify (VOYN-W0-AICC-LEASE-PGPASS-OVERWRITTEN-ON-WORKER-01)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_lease_secret.py"
    spec = importlib.util.spec_from_file_location("aicc_lease_secret", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Unit tests are not root; ownership checks use the running principal.
    module._REQUIRED_OWNER_UID = os.getuid()
    module._REQUIRED_OWNER_GID = os.getgid()
    return module


def _line(module, *, user="voyn_lease_client"):
    return module.LeasePgpassLine(
        host="10.20.0.2",
        port="5432",
        database="voyn_control",
        user=user,
        password="lease-secret",
    )


def test_install_and_verify_round_trip(tmp_path):
    module = _module()
    pgpass = tmp_path / "voyn_lease_pgpass"
    provenance = tmp_path / "voyn_lease_pgpass.provenance"
    worker_env = tmp_path / "worker.env"
    worker_env.write_text(
        "AICC_PG_USER=aicc_w_wrk_voyn_worker_01\nAICC_PG_PASSWORD=x\n",
        encoding="utf-8",
    )

    module.install_lease_pgpass(
        line=_line(module),
        installed_by="unit-test",
        pgpass_path=pgpass,
        provenance_path=provenance,
        installed_at="2026-09-14T18:55:00Z",
    )

    assert pgpass.read_text(encoding="utf-8") == (
        "10.20.0.2:5432:voyn_control:voyn_lease_client:lease-secret\n"
    )
    assert oct(pgpass.stat().st_mode & 0o777) == "0o600"
    recorded = module.verify_lease_secret(
        pgpass_path=pgpass,
        provenance_path=provenance,
        worker_env_path=worker_env,
    )
    assert recorded["pg_user"] == "voyn_lease_client"
    assert recorded["installed_by"] == "unit-test"
    assert "password" not in provenance.read_text(encoding="utf-8")
    assert "lease-secret" not in provenance.read_text(encoding="utf-8")


def test_worker_db_user_is_refused_on_install(tmp_path):
    module = _module()
    with pytest.raises(module.LeaseSecretError, match="worker-DB role"):
        module.install_lease_pgpass(
            line=_line(module, user="aicc_w_wrk_voyn_worker_01"),
            installed_by="unit-test",
            pgpass_path=tmp_path / "voyn_lease_pgpass",
            provenance_path=tmp_path / "voyn_lease_pgpass.provenance",
        )


def test_verify_refuses_pgpass_that_equals_worker_user(tmp_path):
    module = _module()
    pgpass = tmp_path / "voyn_lease_pgpass"
    provenance = tmp_path / "voyn_lease_pgpass.provenance"
    # Bypass install's refuse so we can plant the incident shape.
    pgpass.write_text(
        "127.0.0.1:5433:*:aicc_w_wrk_voyn_worker_01:secret\n", encoding="utf-8"
    )
    os.chmod(pgpass, 0o600)
    provenance.write_text(
        "purpose=writer-lease-authority\n"
        "pg_user=aicc_w_wrk_voyn_worker_01\n"
        "pg_host=127.0.0.1\n"
        "pg_port=5433\n"
        "pg_dbname=*\n"
        "installed_by=bad\n"
        "installed_at=2026-09-10T15:50:00Z\n",
        encoding="utf-8",
    )
    os.chmod(provenance, 0o644)

    with pytest.raises(module.LeaseSecretError, match="worker-DB role"):
        module.verify_lease_secret(
            pgpass_path=pgpass,
            provenance_path=provenance,
            worker_env_path=None,
        )


def test_rotator_guard_refuses_lease_secret_basename(tmp_path):
    module = _module()
    with pytest.raises(module.LeaseSecretError, match="must not touch"):
        module.rotator_must_not_touch(tmp_path / "voyn_lease_pgpass")


def test_installer_harness_no_longer_seeds_worker_db_into_lease_secret():
    """The only tracked writer of the 2026-09-10 overwrite shape was the
    installer integration harness. It must seed a lease-shaped line via
    aicc_lease_secret.py, never printf an aicc_w_* pgpass into the path."""
    harness = (
        Path(__file__).parents[2] / "ops/ci/installer-integration/run.sh"
    ).read_text(encoding="utf-8")
    assert "aicc_lease_secret.py" in harness
    assert "voyn_lease_client" in harness
    assert "aicc_w_wrk_test:secret" not in harness
    assert "printf '127.0.0.1:5433:*:aicc_w_" not in harness


def test_lease_reap_prefers_durable_secret_over_legacy_runtime_path():
    script = (Path(__file__).parents[2] / "ops/lease_reap.sh").read_text(
        encoding="utf-8"
    )
    # The durable secret must be preferred; legacy runtime path is only a
    # last-resort fallback (control-01 has no /run/voyn-aicc-worker/pgpass).
    exports = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("export PGPASSFILE=")
    ]
    assert exports == [
        "export PGPASSFILE=/etc/voyn/secrets/voyn_lease_pgpass",
        "export PGPASSFILE=/run/aicc-worker-lanes/1/pgpass",
        "export PGPASSFILE=/run/voyn-aicc-worker/pgpass",
    ]
    assert "if [ -r /etc/voyn/secrets/voyn_lease_pgpass ]" in script
