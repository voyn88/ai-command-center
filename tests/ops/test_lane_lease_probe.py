"""Rollout lane-lease probe (VOYN-W0-AICC-LEASE-PGPASS-OVERWRITTEN-ON-WORKER-01)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_staged_worker_rollout.py"
    spec = importlib.util.spec_from_file_location("aicc_staged_worker_rollout", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_lane_lease_probe_requires_dsn_and_pgpass():
    module = _module()
    assert module._lane_lease_authority_reachable(
        1, 1002, ("AICC_AGENT_PRINCIPAL_ISOLATION=required",)
    ) == "VOYN_LEASE_DSN is absent from the lane environment"
    assert module._lane_lease_authority_reachable(
        1,
        1002,
        ("VOYN_LEASE_DSN=host=10.20.0.2",),
    ) == "PGPASSFILE is absent from the lane environment"


def test_lane_lease_probe_runs_voyn_lease_list(monkeypatch, tmp_path):
    module = _module()
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_process_gid", lambda pid: 1002)

    env = (
        "VOYN_LEASE_DSN=host=10.20.0.2 port=5432 dbname=voyn_control "
        "user=voyn_lease_client",
        "PGPASSFILE=/run/aicc-worker-lanes/1/pgpass",
    )
    assert module._lane_lease_authority_reachable(4242, 1002, env) is None
    assert calls and calls[0][-2:] == ["voyn-lease", "list"]
    assert "env" in calls[0]
    assert any(item.startswith("PGPASSFILE=") for item in calls[0])


def test_lane_lease_probe_surfaces_auth_failure(monkeypatch):
    module = _module()

    class Result:
        returncode = 1
        stdout = ""
        stderr = "password authentication failed for user voyn_lease_client"

    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: Result()
    )
    monkeypatch.setattr(module, "_process_gid", lambda pid: 1002)

    failure = module._lane_lease_authority_reachable(
        1,
        1002,
        (
            "VOYN_LEASE_DSN=host=10.20.0.2",
            "PGPASSFILE=/run/aicc-worker-lanes/1/pgpass",
        ),
    )
    assert failure is not None
    assert "password authentication failed" in failure
