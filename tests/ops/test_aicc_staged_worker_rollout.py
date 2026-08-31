from __future__ import annotations

import importlib.util
import os
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

    # Temporary registries are created with the host user's metadata (on
    # macOS that is not root:root). Simulate the installed canonical file's
    # owner so ordinary parser tests exercise content handling; dedicated
    # tests below override this seam to prove ownership rejection.
    real_fstat = module._registry_fstat

    class RootRegistryStat:
        def __init__(self, value):
            self._value = value
            self.st_uid = 0
            self.st_gid = 0

        def __getattr__(self, name):
            return getattr(self._value, name)

    module._registry_fstat = lambda fd: RootRegistryStat(real_fstat(fd))
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
                "Environment": "AICC_AGENT_PRINCIPAL_ISOLATION=required",
                "EnvironmentFiles": (
                    "/etc/aicc/lease.env (ignore_errors=no) "
                    "/var/lib/voyn-aicc-credential-rotation/worker.env (ignore_errors=no) "
                    "/etc/aicc/executors.env (ignore_errors=yes) "
                    f"/etc/aicc/worker-{unit.removeprefix('voyn-aicc-worker@').removesuffix('.service')}.env (ignore_errors=yes) "
                    "/etc/aicc/workspace-authority.env (ignore_errors=no)"
                ),
                "ExecStart": (
                    "/opt/aicc/current/.venv/bin/python -m command_center.worker"
                ),
                "ActiveState": "active",
                "SubState": "running",
                "NoNewPrivileges": "yes",
                "ProtectSystem": "strict",
                "ProtectHome": "yes",
                "ProtectControlGroups": "yes",
                "PrivateTmp": "yes",
                "PrivateDevices": "yes",
                "ProtectProc": "invisible",
                "ProcSubset": "pid",
                "UMask": "0077",
                "SupplementaryGroups": "aicc-workspace aicc-publisher",
                "User": "aicc-worker",
                "Group": "aicc-worker",
                "WorkingDirectory": "/opt/aicc/current",
                "ProtectKernelTunables": "yes",
                "ProtectKernelModules": "yes",
                "ProtectKernelLogs": "yes",
                "ProtectClock": "yes",
                "ProtectHostname": "yes",
                "RestrictSUIDSGID": "yes",
                "LockPersonality": "yes",
                "KeyringMode": "private",
                "CapabilityBoundingSet": "",
                "AmbientCapabilities": "",
                "MainPID": str(2000 + index),
            }
            for index, unit in enumerate(units)
        }
        # The legacy single-lane units are REAL rollout targets (drained and
        # disabled before template start); a strict fake must declare them
        # with the full key set instead of silently no-opping mutations on
        # unknown names (review on fd5de6b). Retired state: not running.
        for legacy in (
            "voyn-aicc-worker.service",
            "voyn-aicc-worker-2.service",
            "aicc-worker.service",
        ):
            self.states.setdefault(
                legacy,
                {
                    "enabled": False,
                    "LoadState": "loaded",
                    "FragmentPath": f"/etc/systemd/system/{legacy}",
                    "DropInPaths": "",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "NoNewPrivileges": "yes",
                    "ProtectHome": "read-only",
                    "ProtectControlGroups": "yes",
                    "SupplementaryGroups": "aicc-workspace aicc-publisher",
                    "User": "aicc-worker",
                    "MainPID": "0",
                },
            )

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
            if unit not in self.states:
                # Real systemctl show on an absent unit reports
                # ActiveState=inactive (LoadState carries not-found); an
                # ActiveState=not-found fake inverts any
                # `!= "inactive"` predicate (review on fd5de6b).
                if name == "MainPID":
                    return "0"
                if name == "ActiveState":
                    return "inactive"
                if name == "LoadState":
                    return "not-found"
                return "not-found"
            return str(self.states[unit][name])
        unit = args[-1]
        if unit not in self.states:
            if action == "is-enabled":
                return "disabled"
            if action == "is-active":
                return "inactive"
            # Mutating a unit this fake never declared is a typo'd rollout
            # target, not a no-op (review on fd5de6b).
            if action in {"start", "stop", "enable", "disable", "restart"}:
                raise KeyError(unit)
            return ""
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


def test_uninstall_snapshot_audit_refuses_a_lane_created_after_snapshot():
    module = _module()
    systemd = FakeSystemd(
        ("voyn-aicc-worker@blue.service", "voyn-aicc-worker@late.service")
    )
    state = {
        "version": 3,
        "units": {
            "voyn-aicc-worker@blue.service": {
                "exists": True,
                "enabled": True,
                "active": True,
                "properties": {},
            }
        },
    }

    with pytest.raises(module.RolloutError, match="outside service snapshot"):
        module.verify_snapshot_closure(systemd, state)


def test_uninstall_snapshot_audit_accepts_the_exact_discovered_lane_set():
    module = _module()
    systemd = FakeSystemd(("voyn-aicc-worker@blue.service",))
    state = {
        "version": 3,
        "units": {
            "voyn-aicc-worker@blue.service": {
                "exists": True,
                "enabled": True,
                "active": True,
                "properties": {},
            }
        },
    }

    module.verify_snapshot_closure(systemd, state)


def test_registry_expands_arbitrary_lane_and_rejects_duplicates(tmp_path):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text("gpu-east-7\nvoyn-aicc-worker@batch_9.service\n", encoding="utf-8")

    assert module._configured_units(lanes) == {
        "voyn-aicc-worker@gpu-east-7.service",
        "voyn-aicc-worker@batch_9.service",
    }

    lanes.write_text(
        "gpu-east-7\nvoyn-aicc-worker@gpu-east-7.service\n", encoding="utf-8"
    )
    with pytest.raises(module.RolloutError, match="duplicate worker lane"):
        module._configured_units(lanes)


@pytest.mark.parametrize("entry", ["", "bad/lane", "voyn-aicc-worker@bad/lane.service"])
def test_registry_rejects_invalid_lane_entries(tmp_path, entry):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text(f"{entry}\n", encoding="utf-8")
    if not entry:
        # An empty registry is rejected by discovery, while blank lines alone
        # remain valid input for the registry parser.
        assert module._configured_units(lanes) == set()
        return
    with pytest.raises(module.RolloutError, match="invalid worker lane"):
        module._configured_units(lanes)


def test_registry_rejects_symlink(tmp_path):
    module = _module()
    target = tmp_path / "real-lanes"
    target.write_text("1\n", encoding="utf-8")
    lanes = tmp_path / "lanes"
    lanes.symlink_to(target)

    with pytest.raises(module.RolloutError, match="registry is a symlink"):
        module._configured_units(lanes)


def test_registry_rejects_group_world_writable_mode(tmp_path):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text("1\n", encoding="utf-8")
    lanes.chmod(0o666)

    with pytest.raises(module.RolloutError, match="root:root regular"):
        module._configured_units(lanes)


def test_registry_rejects_non_root_owner(tmp_path, monkeypatch):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text("1\n", encoding="utf-8")
    real_fstat = module._registry_fstat

    class NonRootStat:
        def __init__(self, value):
            self._value = value
            self.st_uid = 4242

        def __getattr__(self, name):
            return getattr(self._value, name)

    monkeypatch.setattr(module, "_registry_fstat", lambda fd: NonRootStat(real_fstat(fd)))
    with pytest.raises(module.RolloutError, match="root:root regular"):
        module._configured_units(lanes)


def test_registry_replacement_during_read_fails_closed(tmp_path, monkeypatch):
    module = _module()
    lanes = tmp_path / "lanes"
    lanes.write_text("1\n", encoding="utf-8")
    displaced = tmp_path / "displaced"
    real_stat = module.os.stat
    real_fstat = module._registry_fstat
    replaced = False

    class RootStat:
        def __init__(self, value):
            self._value = value
            self.st_uid = 0
            self.st_gid = 0

        def __getattr__(self, name):
            return getattr(self._value, name)

    def replace_before_named_stat(path, *args, **kwargs):
        nonlocal replaced
        if path == "lanes" and not replaced:
            replaced = True
            lanes.rename(displaced)
            lanes.write_text("2\n", encoding="utf-8")
            lanes.chmod(0o600)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", replace_before_named_stat)
    monkeypatch.setattr(
        module, "_registry_fstat", lambda fd: RootStat(real_fstat(fd))
    )
    with pytest.raises(module.RolloutError, match="changed while being read"):
        module._configured_units(lanes)


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
        "properties": {},
    }
    module.restore(systemd, state)
    assert ("stop", unit) in systemd.calls
    assert ("disable", unit) in systemd.calls


def test_absent_baseline_unit_restore_fails_if_unit_remains_active():
    module = _module()
    unit = "aicc-principal-recovery.service"

    class StubbornSystemd(FakeSystemd):
        def run(self, *args: str, check: bool = True) -> str:
            if args == ("stop", unit):
                self.calls.append(args)
                return ""
            return super().run(*args, check=check)

    systemd = StubbornSystemd((unit,))
    state = {
        "version": 2,
        "units": {unit: {"exists": False, "enabled": False, "active": False}},
    }
    with pytest.raises(module.RolloutError, match="did not restore exactly"):
        module.restore(systemd, state)


def test_versioned_restore_refuses_property_drift_before_restart():
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    state = module.snapshot(systemd, (unit,))
    systemd.states[unit]["DropInPaths"] = ""
    systemd.states[unit]["ActiveState"] = "inactive"
    systemd.states[unit]["MainPID"] = "0"
    systemd.calls.clear()

    with pytest.raises(module.RolloutError, match="unsafe snapshot restart"):
        module.restore(systemd, state)

    assert ("start", unit) not in systemd.calls


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
        process_environment=lambda pid: ("AICC_AGENT_PRINCIPAL_ISOLATION=required",),
    )

    mutations = [
        call for call in systemd.calls if call[0] in {"enable", "stop", "start"}
    ]
    # Legacy claimers retire BEFORE the canary lane starts claiming -- no
    # coexistence window (review on 0f4d77e; runbook step 5 ordering).
    assert mutations == [
        ("stop", "voyn-aicc-worker.service"),
        ("stop", "voyn-aicc-worker-2.service"),
        ("stop", "aicc-worker.service"),
        ("enable", units[0]),
        ("stop", units[0]),
        ("start", units[0]),
        ("enable", units[1]),
        ("stop", units[1]),
        ("start", units[1]),
    ]


def test_verifier_accepts_real_systemctl_execstart_serialization():
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    # Post-unbake serialization: plain ExecStart, so path= EQUALS argv[0]
    # (the /usr/bin/env prefix is gone). A path= that diverges from argv[0]
    # is the @-decouple attack the verifier now rejects.
    systemd.states[unit]["ExecStart"] = (
        "{ path=/opt/aicc/current/.venv/bin/python ; "
        "argv[]=/opt/aicc/current/.venv/bin/python -m command_center.worker ; "
        "ignore_errors=no ; pid=321 ; code=(null) ; status=0/0 }"
    )

    module.verify_unit_configuration(systemd, unit)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"LoadState": "masked"}, "LoadState"),
        ({"DropInPaths": ""}, "principal boundary"),
        (
            {"Environment": "AICC_AGENT_PRINCIPAL_ISOLATION=optional"},
            "flag is not required",
        ),
        (
            {
                "Environment": (
                    "AICC_AGENT_PRINCIPAL_ISOLATION=required "
                    "AICC_AGENT_PRINCIPAL_ISOLATION=optional"
                )
            },
            "flag is not required",
        ),
        ({"ExecStart": "/bin/true"}, "ExecStart"),
        # A required authority file downgraded to optional (`-` prefix, i.e.
        # ignore_errors=yes) keeps the same path but would let a missing
        # lease.env be silently skipped: the verifier must reject it, not accept
        # it on path membership alone.
        (
            {
                "EnvironmentFiles": (
                    "/etc/aicc/lease.env (ignore_errors=yes) "
                    "/var/lib/voyn-aicc-credential-rotation/worker.env (ignore_errors=no) "
                    "/etc/aicc/executors.env (ignore_errors=yes) "
                    "/etc/aicc/worker-2.env (ignore_errors=yes) "
                    "/etc/aicc/workspace-authority.env (ignore_errors=no)"
                )
            },
            "optional",
        ),
    ],
)
def test_rollout_refuses_fail_open_lane_before_first_mutation(change, message):
    module = _module()
    units = ("voyn-aicc-worker@1.service", "voyn-aicc-worker@2.service")
    systemd = FakeSystemd(units)
    systemd.states[units[1]].update(change)

    with pytest.raises(module.RolloutError, match=message):
        module.rollout(
            systemd,
            units,
            agent_user="aicc-agent",
            privileged_users=("root", "voynadmin"),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
        )

    assert not any(
        call[0] in {"start", "stop", "enable", "disable"} for call in systemd.calls
    )


def test_rollout_revalidates_configuration_after_drain_before_start():
    module = _module()
    unit = "voyn-aicc-worker@1.service"

    class DriftAfterDrain(FakeSystemd):
        def run(self, *args: str, check: bool = True) -> str:
            value = super().run(*args, check=check)
            if args == ("stop", unit):
                self.states[unit]["DropInPaths"] = ""
            return value

    systemd = DriftAfterDrain((unit,))
    with pytest.raises(module.RolloutError, match="principal boundary"):
        module.rollout(
            systemd,
            (unit,),
            agent_user="aicc-agent",
            privileged_users=("root", "voynadmin"),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
        )

    assert ("start", unit) not in systemd.calls


def test_rollout_refuses_to_start_template_lane_while_legacy_pid_survives():
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    systemd.states["voyn-aicc-worker.service"] = {
        "enabled": False,
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": "4242",
    }

    original_run = systemd.run

    def run(*args, **kwargs):
        if args == ("stop", "voyn-aicc-worker.service"):
            systemd.calls.append(args)
            return ""
        return original_run(*args, **kwargs)

    systemd.run = run
    with pytest.raises(module.RolloutError, match="legacy worker"):
        module.rollout(
            systemd,
            (unit,),
            agent_user="aicc-agent",
            privileged_users=("root", "voynadmin"),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
        )
    # Legacy drain precedes the canary start; the surviving legacy PID must
    # abort the rollout BEFORE the first template lane ever starts.
    assert ("stop", "voyn-aicc-worker.service") in systemd.calls
    assert ("start", unit) not in systemd.calls


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
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
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
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
        )


def test_verifier_rejects_environment_file_override_in_live_mainpid():
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    with pytest.raises(module.RolloutError, match="MainPID principal-isolation"):
        module.verify_unit(
            systemd,
            unit,
            agent_uid=1001,
            privileged_uids=frozenset({0, 1000}),
            uid_for_user=_uid,
            process_uid=lambda pid: 1002,
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=optional",
            ),
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
            process_environment=lambda pid: (
                "AICC_AGENT_PRINCIPAL_ISOLATION=required",
            ),
        )
    # Failed generation remains fail-closed. The outer file transaction
    # restores files before it restores the captured service snapshot.
    lanes = {name: state for name, state in systemd.states.items() if "@" in name}
    assert all(not state["enabled"] for state in lanes.values())
    assert all(state["ActiveState"] == "inactive" for state in lanes.values())


def test_verifier_rejects_execstart_path_decoupled_from_argv():
    """systemd's @ prefix runs path= while argv[] still matches the expected
    command; the verifier must reject a path= that diverges from argv[0]
    (independent-review finding on 4a0a878)."""
    module = _module()
    unit = "voyn-aicc-worker@1.service"
    systemd = FakeSystemd((unit,))
    systemd.states[unit]["ExecStart"] = (
        "{ path=/usr/bin/evil ; "
        "argv[]=/opt/aicc/current/.venv/bin/python -m command_center.worker ; "
        "ignore_errors=no ; pid=321 ; code=(null) ; status=0/0 }"
    )
    with pytest.raises(module.RolloutError, match="path="):
        module.verify_unit_configuration(systemd, unit)


# ---------------------------------------------------------------------------
# verify_immutable_release: the gate that refused every real release.
#
# A virtualenv interpreter is never a regular file at `.venv/bin/python` --
# it is a symlink chain that leaves the release entirely:
#
#     .venv/bin/python -> python3 -> /usr/bin/python3 -> python3.12
#
# The chain below is built to that exact shape, because the shape is what the
# defect turned on: Linux gives every symlink mode `lrwxrwxrwx` and ignores it,
# so a `st_mode & 0o022` test applied to the symlink itself is true for every
# release that has ever existed. This function had no coverage at all, which is
# why the gate reached production able to refuse only itself.
# ---------------------------------------------------------------------------


def _build_release(tmp_path, *, final_mode=0o755, bin_mode=0o555):
    """A release laid out like a real one, with the interpreter reached through
    a relative hop inside the release and an absolute hop out of it."""
    opt = tmp_path / "opt" / "aicc"
    releases = opt / "releases"
    # Deliberately not a real commit id, and deliberately low-entropy: a
    # genuine 40-hex literal trips detect-secrets as a "Hex High Entropy
    # String" and grows the secret baseline with test data. The gate only
    # requires `[0-9a-f]{40}`.
    sha = "a1" * 20
    release = releases / sha
    venv_bin = release / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    system_bin = tmp_path / "usr" / "bin"
    system_bin.mkdir(parents=True)
    real = system_bin / "python3.12"
    real.write_text("#!/bin/true\n", encoding="utf-8")
    real.chmod(final_mode)

    # Hop 2 leaves the release, exactly as a real venv does.
    (venv_bin / "python3").symlink_to(real)
    # Hop 1 is relative and stays inside it.
    (venv_bin / "python").symlink_to("python3")
    # Linux creates every symlink `lrwxrwxrwx` and cannot chmod it; macOS
    # creates them 0o755. Pin both to the Linux value, or this test silently
    # stops exercising the defect on a developer machine -- which is precisely
    # how a gate that could never pass on Linux shipped without anyone noticing.
    for link in (venv_bin / "python3", venv_bin / "python"):
        if hasattr(os, "lchmod"):
            try:
                os.lchmod(link, 0o777)
            except (OSError, NotImplementedError):
                pass

    current = opt / "current"
    current.symlink_to(Path("releases") / sha)

    for path in (venv_bin, release / ".venv"):
        path.chmod(bin_mode)
    release.chmod(0o555)
    releases.chmod(0o755)
    opt.chmod(0o755)
    return opt, releases, current


def _install_release(module, monkeypatch, tmp_path, **kwargs):
    opt, releases, current = _build_release(tmp_path, **kwargs)
    monkeypatch.setattr(module, "CURRENT_RELEASE", current)
    monkeypatch.setattr(module, "RELEASE_ROOT", releases)
    # The chain cannot be built root-owned without root; assert against the
    # user that actually owns it, so every other property stays under test.
    monkeypatch.setattr(module, "_REQUIRED_OWNER_UID", os.getuid())
    # The ancestor walk runs to the filesystem root in production. A temp tree
    # cannot satisfy that -- `/tmp` is world-writable and sticky by design --
    # and those directories are not what these tests examine, so the walk is
    # bounded at the tree the fixture actually built.
    monkeypatch.setattr(module, "_TRUSTED_ROOT", tmp_path)
    return opt


def test_a_release_whose_interpreter_is_a_symlink_is_accepted(tmp_path, monkeypatch):
    """The regression itself: this is what every real release looks like."""
    module = _module()
    _install_release(module, monkeypatch, tmp_path)

    module.verify_immutable_release()


def test_interpreter_target_outside_the_release_is_still_verified(tmp_path, monkeypatch):
    """The binary that actually runs lives in a system path. Verifying only
    what is inside the release proves nothing about it."""
    module = _module()
    _install_release(module, monkeypatch, tmp_path, final_mode=0o757)

    with pytest.raises(module.RolloutError, match="mutable"):
        module.verify_immutable_release()


def test_directory_holding_the_interpreter_symlink_must_not_be_writable(tmp_path, monkeypatch):
    """A symlink's own mode is meaningless; its directory's is what protects
    it from being repointed."""
    module = _module()
    _install_release(module, monkeypatch, tmp_path, bin_mode=0o775)

    with pytest.raises(module.RolloutError, match="mutable"):
        module.verify_immutable_release()


def test_a_symlink_cycle_is_refused_rather_than_followed(tmp_path, monkeypatch):
    module = _module()
    opt = _install_release(module, monkeypatch, tmp_path)
    venv_bin = opt / "releases" / ("a1" * 20) / ".venv" / "bin"
    venv_bin.chmod(0o755)
    (venv_bin / "python").unlink()
    (venv_bin / "python").symlink_to("python3")
    (venv_bin / "python3").unlink()
    (venv_bin / "python3").symlink_to("python")
    venv_bin.chmod(0o555)

    with pytest.raises(module.RolloutError, match="too deep"):
        module.verify_immutable_release()


def test_a_writable_directory_far_above_the_interpreter_is_refused(tmp_path, monkeypatch):
    """Not just the immediate parent. Write permission on any directory above
    the interpreter is enough to rename the whole subtree out and drop a
    replacement -- so checking `/usr/bin` while ignoring `/usr` would leave the
    binary swappable by anyone who can write to `/usr`.
    """
    module = _module()
    _install_release(module, monkeypatch, tmp_path)
    # Two levels above the real interpreter: `usr/bin/python3.12` lives under
    # `usr/`, which nothing but the ancestor walk looks at.
    (tmp_path / "usr").chmod(0o777)

    with pytest.raises(module.RolloutError, match="mutable"):
        module.verify_immutable_release()
