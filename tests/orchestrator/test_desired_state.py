from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center.orchestrator.desired_state import load

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "deploy" / "config" / "aicc-desired-state.json"


def test_shipped_registry_is_complete_and_supports_multiple_worker_lanes():
    desired = load(REGISTRY)

    assert len(desired.control.units) >= 10
    assert len(desired.worker_fleet.units) >= 2
    assert desired.worker_fleet.minimum_ready_lanes < len(desired.worker_fleet.units)
    assert (
        desired.worker_fleet.minimum_stop_seconds > desired.worker_fleet.max_job_seconds
    )
    assert len(desired.sha256) == 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value["worker_fleet"].update(units=["../../evil.service"]),
        lambda value: value["worker_fleet"].update(units=["worker@1.service"]),
        lambda value: value["worker_fleet"].update(minimum_ready_lanes=99),
        lambda value: value["worker_fleet"].update(recovery_timeout_seconds=1),
    ],
)
def test_registry_rejects_unknown_malformed_or_unsafe_policy(tmp_path, mutate):
    value = json.loads(REGISTRY.read_text())
    mutate(value)
    path = tmp_path / "desired.json"
    path.write_text(json.dumps(value))

    with pytest.raises(ValueError):
        load(path)
