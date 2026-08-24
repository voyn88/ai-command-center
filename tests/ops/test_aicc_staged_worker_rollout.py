from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_staged_worker_rollout.py"
    spec = importlib.util.spec_from_file_location("aicc_staged_worker_rollout", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeSystemd:
    def __init__(self, units: tuple[str, ...]):
        self.calls: list[tuple[str, ...]] = []
        self.states = {
            unit: {
                "enabled": True,
                "LoadState": "loaded",
                "FragmentPath": "/etc/systemd/system/voyn-aicc-worker@.service",
                "DropInPaths": "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf",
                "ActiveState": "active",
                "SubState": "running",
                "NoNewPrivileges": "yes",
                "ProtectHome": "read-only",
                "ProtectControlGroups": "yes",
                "SupplementaryGroups": "aicc-workspace aicc-publisher",
                "User": "aicc-worker",
                "MainPID": str(2000 + index),
            }
            for index, unit in enumerate(units)
        }

    def run(self, *args: str, check: bool = True) -> str:
        self.calls.append(args)
        action = args[0]
        if action == "list-unit-files":
            return "\n".join(f"{unit} enabled" for unit in self.states)
        if action == "list-units":
            return ""
        if action == "show":
            unit = args[1]
            name = next(
                value.split("=", 1)[1]
                for value in args
                if value.startswith("--property=")
            )
            return str(self.states[unit][name])
        unit = args[-1]
        state = self.states[unit]
        if action == "is-enabled":
            return "enabled" if state["enabled"] else "disabled"
        if action == "is-active":
            return str(state["ActiveState"])
        if action == "enable":
            state["enabled"] = True
        elif action == "disable":
            state["enabled"] = False
        elif action == "stop":
            state["ActiveState"] = "inactive"
            state["SubState"] = "dead"
            state["MainPID"] = "0"
        elif action == "start":
            state["ActiveState"] = "active"
            state["SubState"] = "running"
            state["MainPID"] = "3000"
        return ""

    def property(self, unit: str, name: str) -> str:
        return str(self.states[unit][name])


def _uid(user: str) -> int:
    return {"root": 0, "voynadmin": 1000, "aicc-agent": 1001, "aicc-worker": 1002}[user]


def test_discovery_combines_configured_and_existing_lanes(tmp_path):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text("1\n2\n", encoding="utf-8")
    systemd = FakeSystemd(("voyn-aicc-worker@3.service",))
    assert module.discover_units(systemd, lanes) == (
        "voyn-aicc-worker@1.service",
        "voyn-aicc-worker@2.service",
        "voyn-aicc-worker@3.service",
    )


def test_privileged_principal_set_is_versioned_and_extensible(tmp_path):
    module = _module()
    principals = tmp_path / "principals"
    principals.write_text("root\nvoynadmin\nrelease-publisher\n", encoding="utf-8")
    assert module._configured_users(principals) == (
        "root",
        "voynadmin",
        "release-publisher",
    )
    principals.write_text("root\nnot/a-user\n", encoding="utf-8")
    with pytest.raises(module.RolloutError, match="invalid privileged"):
        module._configured_users(principals)


def test_snapshot_and_restore_tolerate_unit_absent_on_clean_host():
    module = _module()
    unit = "aicc-principal-recovery.service"
    systemd = FakeSystemd((unit,))
    systemd.states[unit].update(
        {"LoadState": "not-found", "enabled": False, "ActiveState": "inactive"}
    )

    state = module.snapshot(systemd, (unit,))

    assert state["units"][unit] == {
        "exists": False,
        "enabled": False,
        "active": False,
    }
    module.restore(systemd, state)
    assert ("stop", unit) in systemd.calls
    assert ("disable", unit) in systemd.calls


def test_staged_rollout_drains_and_proves_each_lane_before_next():
    module = _module()
    units = ("voyn-aicc-worker@1.service", "voyn-aicc-worker@2.service")
    systemd = FakeSystemd(units)

    module.rollout(
        systemd,
        units,
        agent_user="aicc-agent",
        privileged_users=("root", "voynadmin"),
        uid_for_user=_uid,
        process_uid=lambda pid: 1002,
    )

    mutations = [
        call for call in systemd.calls if call[0] in {"enable", "stop", "start"}
    ]
    assert mutations == [
        ("enable", units[0]),
        ("stop", units[0]),
        ("start", units[0]),
        ("enable", units[1]),
        ("stop", units[1]),
        ("start", units[1]),
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ActiveState": "inactive"}, "ActiveState"),
        ({"MainPID": "0"}, "MainPID"),
        ({"User": "aicc-agent"}, "isolated"),
    ],
)
def test_verifier_rejects_stopped_stale_or_uid_aliased_unit(change, message):
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    systemd.states[unit].update(change)
    with pytest.raises(module.RolloutError, match=message):
        module.verify_unit(
            systemd,
            unit,
            agent_uid=1001,
            privileged_uids=frozenset({0, 1000}),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
        )


def test_verifier_rejects_named_privileged_alias_of_agent_uid():
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))

    def aliased_uid(user: str) -> int:
        return {"root": 0, "voynadmin": 1001, "aicc-agent": 1001}[user]

    with pytest.raises(module.RolloutError, match="aliases a privileged"):
        module.verify_all(
            systemd,
            (unit,),
            agent_user="aicc-agent",
            privileged_users=("root", "voynadmin"),
            uid_for_user=aliased_uid,
            process_uid=lambda pid: 1002,
        )


def test_rollout_failure_restores_all_lane_states():
    module = _module()
    units = ("voyn-aicc-worker@1.service", "voyn-aicc-worker@2.service")

    class FailingSystemd(FakeSystemd):
        def run(self, *args: str, check: bool = True) -> str:
            value = super().run(*args, check=check)
            if args == ("start", units[1]):
                self.states[units[1]]["SubState"] = "failed"
            return value

    systemd = FailingSystemd(units)
    with pytest.raises(module.RolloutError, match="SubState"):
        module.rollout(
            systemd,
            units,
            agent_user="aicc-agent",
            privileged_users=("root", "voynadmin"),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
        )
    assert all(state["enabled"] for state in systemd.states.values())
    assert all(state["ActiveState"] == "active" for state in systemd.states.values())
