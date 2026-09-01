from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Every test here drives the privileged installer, so every test builds its
# fixture paths the way a host has them and runs under the permissive 0o002
# umask an operator may well have. See tests/ops/conftest.py.
pytestmark = pytest.mark.usefixtures("host_shaped_fixture_roots")


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_install_transaction.py"
    spec = importlib.util.spec_from_file_location("aicc_install_transaction", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _generator_module():
    path = Path(__file__).parents[2] / "ops" / "aicc_principal_recovery_generator.py"
    spec = importlib.util.spec_from_file_location(
        "aicc_principal_recovery_generator", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _spec(module, source: Path, target: str, mode: int = 0o640):
    return module.FileSpec(source, target, mode, os.geteuid(), os.getegid())


def test_mid_install_failure_restores_every_pretransaction_target(
    monkeypatch, tmp_path
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    existing = root / "etc/existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"before")
    existing.chmod(0o600)
    source_one = tmp_path / "one"
    source_two = tmp_path / "two"
    source_one.write_bytes(b"after")
    source_two.write_bytes(b"new")
    transaction = module.FileTransaction(root, state)
    transaction.prepare(
        (
            _spec(module, source_one, "/etc/existing"),
            _spec(module, source_two, "/etc/new"),
        )
    )
    assert transaction.pending.exists()
    real_atomic = module._atomic_bytes

    def fail_second_target(path, *args, **kwargs):
        if path == root / "etc/new":
            raise OSError("injected staged-install failure")
        return real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_bytes", fail_second_target)
    with pytest.raises(OSError, match="injected"):
        transaction.apply()

    assert existing.read_bytes() == b"before"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert not (root / "etc/new").exists()
    assert not transaction.pending.exists()


def test_two_generations_uninstall_to_preinstall_state_without_orphans(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    existing = root / "etc/existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"before")
    existing.chmod(0o600)
    source_one = tmp_path / "one"
    source_two = tmp_path / "two"
    source_one.write_bytes(b"after")
    source_two.write_bytes(b"new")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            _spec(module, source_one, "/etc/existing"),
            _spec(module, source_two, "/etc/new"),
        )
    )
    assert existing.read_bytes() == b"after"
    assert (root / "etc/new").read_bytes() == b"new"

    source_one.write_bytes(b"after-two")
    source_two.write_bytes(b"new-two")
    transaction.install(
        (
            _spec(module, source_one, "/etc/existing"),
            _spec(module, source_two, "/etc/new"),
        )
    )
    assert existing.read_bytes() == b"after-two"

    transaction.uninstall_all()

    assert existing.read_bytes() == b"before"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert not (root / "etc/new").exists()
    assert not transaction.current.exists()
    assert not transaction.pending.exists()
    assert not list(state.glob("generation-*"))


def test_sigkill_mid_apply_is_recovered_from_write_ahead_journal(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    existing = root / "etc/existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"before")
    existing.chmod(0o600)
    source_one = tmp_path / "one"
    source_two = tmp_path / "two"
    source_one.write_bytes(b"after")
    source_two.write_bytes(b"new")
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare(
        (
            _spec(module, source_one, "/etc/existing"),
            _spec(module, source_two, "/etc/new"),
        )
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    first = module.BackupRecord(**payload["records"][0])
    transaction._write_journal(manifest, "APPLYING", 0)
    module._atomic_bytes(
        transaction._target(first.target),
        Path(first.staged).read_bytes(),
        first.install_mode,
        first.install_uid,
        first.install_gid,
    )
    assert existing.read_bytes() == b"after"

    module.FileTransaction(root, state).recover()

    assert existing.read_bytes() == b"before"
    assert not (root / "etc/new").exists()
    assert not (state / "pending.json").exists()
    assert not list(state.glob("generation-*"))


def test_atomic_write_refuses_rename_writable_parent(tmp_path):
    module = _module()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)

    with pytest.raises(ValueError, match="rename-writable"):
        module._atomic_bytes(
            unsafe / "target",
            b"must-not-land",
            0o600,
            os.geteuid(),
            os.getegid(),
        )

    assert not (unsafe / "target").exists()


def test_open_directory_chain_closes_rejected_component(tmp_path, monkeypatch):
    module = _module()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    real_close = module.os.close
    rejected_fd: list[int] = []
    closed: list[int] = []
    real_validate = module._validate_directory_fd

    def tracking_validate(descriptor, path):
        try:
            real_validate(descriptor, path)
        except ValueError:
            rejected_fd.append(descriptor)
            raise

    def tracking_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(module, "_validate_directory_fd", tracking_validate)
    monkeypatch.setattr(module.os, "close", tracking_close)

    with pytest.raises(ValueError, match="rename-writable"):
        module._open_directory_chain(unsafe, create=False)

    assert len(rejected_fd) == 1
    assert rejected_fd[0] in closed


def test_applied_generation_is_not_committed_until_explicit_commit(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()
    assert (root / "etc/new").read_bytes() == b"installed"
    assert transaction.pending.exists()
    assert not transaction.current.exists()

    transaction.commit()

    assert transaction.current.exists()
    assert not transaction.pending.exists()


def test_first_install_boot_generator_is_early_pulled_and_dispatches_capsule(
    monkeypatch, tmp_path
):
    generator = _generator_module()
    state = tmp_path / "state"
    generation = state / "generation-0123456789abcdef"
    generation.mkdir(parents=True)
    recovery = generation / "recovery.py"
    recovery.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    recovery.chmod(0o700)
    (state / "pending.json").write_text(
        json.dumps({"recovery": str(recovery)}), encoding="utf-8"
    )
    (state / "pending.json").chmod(0o600)
    destination = tmp_path / "generator"

    assert generator.generate(destination, state)

    unit = (destination / "aicc-principal-recovery.service").read_text()
    assert f"ExecStart={generator.ANCHOR} --recover {state}" in unit
    assert "Before=sysinit.target basic.target" in unit
    assert "RemainAfterExit=yes" in unit
    assert "ReadWritePaths=-/opt/aicc " in unit
    dependency = destination / "sysinit.target.requires/aicc-principal-recovery.service"
    assert dependency.readlink() == Path("../aicc-principal-recovery.service")
    for claimer in generator.CLAIMERS:
        dropin = destination / f"{claimer}.d/10-aicc-recovery.conf"
        assert "Requires=aicc-principal-recovery.service" in dropin.read_text()

    called = []
    monkeypatch.setattr(generator.os, "execv", lambda *args: called.append(args))
    with pytest.raises(AssertionError, match="unreachable"):
        generator.recover(state, expected_uid=os.getuid())
    assert called[0][1][1] == str(recovery)
    assert called[0][1][2] == "recover-boot"

    alias = generation / "alias.py"
    alias.symlink_to(recovery)
    (state / "pending.json").write_text(
        json.dumps({"recovery": str(alias)}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="capsule path"):
        generator.recover(state, expected_uid=os.getuid())


def test_static_and_generated_recovery_units_have_identical_write_paths(tmp_path):
    generator = _generator_module()
    state = tmp_path / "state"
    generation = state / "generation-0123456789abcdef"
    generation.mkdir(parents=True)
    recovery = generation / "recovery.py"
    recovery.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    recovery.chmod(0o700)
    (state / "pending.json").write_text(
        json.dumps({"recovery": str(recovery)}), encoding="utf-8"
    )
    (state / "pending.json").chmod(0o600)
    destination = tmp_path / "generator"
    assert generator.generate(destination, state)

    static = (
        Path(__file__).parents[2] / "deploy/systemd/aicc-principal-recovery.service"
    ).read_text(encoding="utf-8")
    generated = (destination / "aicc-principal-recovery.service").read_text(
        encoding="utf-8"
    )

    def write_paths(unit: str) -> set[str]:
        line = next(
            value for value in unit.splitlines() if value.startswith("ReadWritePaths=")
        )
        return set(line.removeprefix("ReadWritePaths=").split())

    assert write_paths(static) == write_paths(generated)
    assert "-/opt/aicc" in write_paths(static)


def test_recovery_generator_main_uses_only_early_precedence_directory(
    monkeypatch, tmp_path
):
    generator = _generator_module()
    normal = tmp_path / "normal"
    early = tmp_path / "early"
    late = tmp_path / "late"
    monkeypatch.setattr(
        generator.sys,
        "argv",
        ["aicc-principal-recovery", str(normal), str(early), str(late)],
    )

    assert generator.main() == 0
    assert (early / "aicc-principal-recovery.service").is_file()
    assert not normal.exists()
    assert not late.exists()


def test_recovery_generator_runtime_is_noop_without_journal_and_fails_on_both(
    tmp_path,
):
    generator = _generator_module()
    state = tmp_path / "state"
    state.mkdir()
    assert generator.recover(state, expected_uid=os.getuid()) == 0

    (state / "pending.json").write_text("{}", encoding="utf-8")
    (state / "uninstall.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="coexist"):
        generator.recover(state, expected_uid=os.getuid())


def test_recovery_generator_rejects_selector_wal_without_install_journal(
    tmp_path,
):
    generator = _generator_module()
    state = tmp_path / "state"
    state.mkdir()
    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)

    with pytest.raises(RuntimeError, match="without install journal"):
        generator.recover(state, expected_uid=os.getuid())


@pytest.mark.parametrize("phase", ["PREPARED", "APPLYING"])
def test_recovery_generator_rejects_selector_before_install_is_applied(
    monkeypatch, tmp_path, phase
):
    generator = _generator_module()
    state = tmp_path / "state"
    generation = state / "generation-0123456789abcdef"
    generation.mkdir(parents=True)
    recovery = generation / "recovery.py"
    recovery.write_text("#!/usr/bin/python3\n", encoding="utf-8")
    recovery.chmod(0o700)
    pending = state / "pending.json"
    pending.write_text(
        json.dumps({"recovery": str(recovery), "phase": phase}),
        encoding="utf-8",
    )
    pending.chmod(0o600)
    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    called = []
    monkeypatch.setattr(generator.os, "execv", lambda *args: called.append(args))

    with pytest.raises(RuntimeError, match="not paired with an applied install"):
        generator.recover(state, expected_uid=os.getuid())
    assert called == []


@pytest.mark.parametrize(
    "journal_name", ["pending.json", "pending-release", "uninstall.json"]
)
def test_recovery_generator_rejects_dangling_journal_symlink(
    tmp_path, journal_name
):
    generator = _generator_module()
    state = tmp_path / "state"
    state.mkdir()
    (state / journal_name).symlink_to(state / "missing")

    with pytest.raises(RuntimeError):
        generator.recover(state, expected_uid=os.getuid())


def test_recovery_generator_dispatches_digest_bound_uninstall_capsule(
    monkeypatch, tmp_path
):
    generator = _generator_module()
    state = tmp_path / "state"
    transaction_id = "a" * 32
    capsule_dir = state / f"uninstall-{transaction_id}"
    capsule_dir.mkdir(parents=True)
    recovery = capsule_dir / "recovery.py"
    recovery.write_bytes(b"#!/usr/bin/python3\n")
    recovery.chmod(0o700)
    payload = {
        "version": 2,
        "transaction_id": transaction_id,
        "phase": "INTENT",
        "baseline_selector": "ABSENT",
        "start_selector": f"releases/{'b' * 40}",
        "registry_sha256": "c" * 64,
        "snapshot_sha256": None,
        "recovery": str(recovery),
        "recovery_sha256": hashlib.sha256(recovery.read_bytes()).hexdigest(),
    }
    journal = state / "uninstall.json"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    journal.chmod(0o600)
    called = []
    monkeypatch.setattr(generator.os, "execv", lambda *args: called.append(args))

    with pytest.raises(AssertionError, match="unreachable"):
        generator.recover(state, expected_uid=os.getuid())
    assert called[0][1][1] == str(recovery)
    assert called[0][1][2] == "recover-uninstall-boot"

    payload["recovery_sha256"] = "d" * 64
    journal.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="digest drifted"):
        generator.recover(state, expected_uid=os.getuid())


def test_quiesce_validates_complete_snapshot_before_first_systemd_call(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    },
                    "not/allowlisted.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    with pytest.raises(RuntimeError, match="invalid service snapshot unit"):
        module.quiesce_service_snapshot(
            snapshot, run=lambda *args, **kwargs: calls.append(args)
        )

    assert calls == []


def test_quiesce_requires_inactive_state_and_zero_main_pid(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "LoadState" in argv:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if argv[1] == "stop":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "ActiveState" in argv:
            return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="9123\n", stderr="")

    with pytest.raises(RuntimeError, match="did not quiesce exactly"):
        module.quiesce_service_snapshot(snapshot, run=run)

    assert ["/usr/bin/systemctl", "stop", "voyn-aicc-worker@blue.service"] in calls


def test_quiesce_accepts_a_proven_not_found_unit_without_extra_probes(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@retired.service": {
                        "exists": False,
                        "enabled": False,
                        "active": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="not-found\n", stderr="")

    module.quiesce_service_snapshot(snapshot, run=run)

    assert calls == [
        [
            "/usr/bin/systemctl",
            "show",
            "voyn-aicc-worker@retired.service",
            "--property=LoadState",
            "--value",
        ]
    ]


def test_quiesce_refuses_when_an_expected_active_unit_disappeared(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="not-found\n", stderr="")

    with pytest.raises(RuntimeError, match="expected unit disappeared"):
        module.quiesce_service_snapshot(snapshot, run=run)

    assert len(calls) == 1


def test_transaction_host_lock_contends_and_adopts_the_inherited_inode(tmp_path):
    module = _module()
    lock = tmp_path / "state" / "install-recovery.lock"
    lock.parent.mkdir(mode=0o700)
    uid, gid = os.geteuid(), os.getegid()
    first = module._install_lock_fd(
        lock, trusted_uid=uid, trusted_gid=gid
    )
    try:
        adopted = module._install_lock_fd(
            lock, first, trusted_uid=uid, trusted_gid=gid
        )
        try:
            assert os.fstat(adopted).st_ino == os.fstat(first).st_ino
        finally:
            os.close(adopted)
        with pytest.raises(RuntimeError, match="another install"):
            module._install_lock_fd(lock, trusted_uid=uid, trusted_gid=gid)
    finally:
        os.close(first)


def test_transaction_host_lock_handoff_is_cross_process_and_same_ofd(tmp_path):
    module = _module()
    lock = tmp_path / "state" / "install-recovery.lock"
    lock.parent.mkdir(mode=0o700)
    uid, gid = os.geteuid(), os.getegid()
    held = module._install_lock_fd(lock, trusted_uid=uid, trusted_gid=gid)
    script = str(Path(__file__).parents[2] / "ops/aicc_install_transaction.py")
    child = (
        "import os,runpy,sys; from pathlib import Path; "
        "m=runpy.run_path(sys.argv[1]); fd=int(sys.argv[3]); "
        "adopt=m['_install_lock_fd'](Path(sys.argv[2]),fd,"
        "trusted_uid=os.geteuid(),trusted_gid=os.getegid()); "
        "assert os.fstat(adopt).st_ino==os.fstat(fd).st_ino; os.close(adopt)"
    )
    contender = (
        "import os,runpy,sys; from pathlib import Path; "
        "m=runpy.run_path(sys.argv[1]); "
        "\ntry: m['_install_lock_fd'](Path(sys.argv[2]),"
        "trusted_uid=os.geteuid(),trusted_gid=os.getegid())"
        "\nexcept RuntimeError: raise SystemExit(0)"
        "\nraise SystemExit(9)"
    )
    try:
        adopted = subprocess.run(
            [sys.executable, "-c", child, script, str(lock), str(held)],
            pass_fds=(held,),
            capture_output=True,
            text=True,
            check=False,
        )
        assert adopted.returncode == 0, adopted.stderr
        blocked = subprocess.run(
            [sys.executable, "-c", contender, script, str(lock)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked.returncode == 0, blocked.stderr
    finally:
        os.close(held)


def test_bootstrap_and_transaction_use_one_fixed_host_lock_path():
    transaction = _module()
    bootstrap = runpy.run_path(
        str(Path(__file__).parents[2] / "ops/aicc_exact_sha_bootstrap.py")
    )
    assert transaction.INSTALL_LOCK == bootstrap["DEFAULT_INSTALL_LOCK"]


def test_uninstall_wal_blocks_install_and_resumes_after_registry_removal(tmp_path):
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    current = tmp_path / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = tmp_path / "worker-lanes"
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    snapshot = state / "uninstall-units.json"

    assert (
        module.begin_uninstall(
            state,
            baseline_selector="ABSENT",
            current_selector=current,
            lane_registry=lanes,
        )
        == "INTENT"
    )
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    snapshot.chmod(0o600)
    module.arm_uninstall(state, snapshot)
    lanes.unlink()

    assert (
        module.begin_uninstall(
            state,
            baseline_selector="ABSENT",
            current_selector=current,
            lane_registry=lanes,
        )
        == "ARMED"
    )
    args = SimpleNamespace(action="validate", state_dir=state)
    with pytest.raises(RuntimeError, match="unfinished uninstall"):
        module._dispatch(args, argparse.ArgumentParser())

    module.complete_uninstall(state, snapshot)
    assert not (state / "uninstall.json").exists()
    assert not snapshot.exists()


def test_uninstall_cli_emits_only_closed_phase_literals(capsys, tmp_path):
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    current = tmp_path / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = tmp_path / "worker-lanes-secret-name"
    lanes.write_text("secret-lane-name\n", encoding="utf-8")
    lanes.chmod(0o644)
    parser = argparse.ArgumentParser()
    begin_args = SimpleNamespace(
        action="uninstall-begin",
        state_dir=state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )

    assert module._dispatch(begin_args, parser) == 0
    assert capsys.readouterr().out == "INTENT\n"
    status_args = SimpleNamespace(action="uninstall-status", state_dir=state)
    assert module._dispatch(status_args, parser) == 0
    assert capsys.readouterr().out == "INTENT\n"


def test_uninstall_wal_refuses_registry_or_snapshot_drift(tmp_path):
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    current = tmp_path / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = tmp_path / "worker-lanes"
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    snapshot = state / "uninstall-units.json"
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    lanes.write_text("green\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="registry changed"):
        module.begin_uninstall(
            state,
            baseline_selector="ABSENT",
            current_selector=current,
            lane_registry=lanes,
        )
    lanes.write_text("blue\n", encoding="utf-8")
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    snapshot.chmod(0o600)
    module.arm_uninstall(state, snapshot)
    snapshot.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="snapshot drifted"):
        module.arm_uninstall(state, snapshot)


def test_boot_recovery_aborts_unarmed_uninstall_intent_with_wal_last(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = root / "etc/aicc/worker-lanes"
    lanes.parent.mkdir(parents=True)
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    capsule = Path(json.loads((state / "uninstall.json").read_text())["recovery"])
    (state / "uninstall-units.json").write_text("partial", encoding="utf-8")

    module.recover_uninstall(state, root=root, boot=True)

    assert current.readlink() == Path(f"releases/{'b' * 40}")
    assert lanes.read_text(encoding="utf-8") == "blue\n"
    assert not (state / "uninstall.json").exists()
    assert not (state / "uninstall-units.json").exists()
    assert not capsule.exists()


def test_boot_recovery_completes_armed_uninstall_from_capsule(
    monkeypatch, tmp_path
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.install((_spec(module, source, "/etc/aicc-installed"),))
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = root / "etc/aicc/worker-lanes"
    lanes.parent.mkdir(parents=True)
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    (state / "baseline-units.json").write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    snapshot = state / "uninstall-units.json"
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    snapshot.chmod(0o600)
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    module.arm_uninstall(state, snapshot)
    restored = []
    closure_checks = []
    monkeypatch.setattr(
        module,
        "verify_service_snapshot_closure",
        lambda path: closure_checks.append(path),
    )
    monkeypatch.setattr(module, "quiesce_service_snapshot", lambda path, **_kw: None)
    monkeypatch.setattr(
        module,
        "restore_service_snapshot",
        lambda path, *, defer_starts=False: restored.append(
            (path, defer_starts)
        ),
    )

    module.recover_uninstall(state, root=root, boot=True)

    assert not (root / "etc/aicc-installed").exists()
    assert not current.exists()
    assert not (state / "uninstall.json").exists()
    assert not snapshot.exists()
    assert restored == [(state / "baseline-units.json", True)]
    assert closure_checks == [snapshot, snapshot, snapshot]


@pytest.mark.parametrize("boot", [False, True])
def test_armed_uninstall_recovery_refuses_late_lane_before_mutation(
    monkeypatch, tmp_path, boot
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.install((_spec(module, source, "/etc/aicc-installed"),))
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = root / "etc/aicc/worker-lanes"
    lanes.parent.mkdir(parents=True)
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    snapshot = state / "uninstall-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    snapshot.chmod(0o600)
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    module.arm_uninstall(state, snapshot)

    late_lane_visible = False

    def run(argv, **kwargs):
        output = "voyn-aicc-worker@blue.service enabled\n"
        if late_lane_visible and argv[1] == "list-unit-files":
            output += "voyn-aicc-worker@late.service enabled\n"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    closure = module.verify_service_snapshot_closure

    def check_closure(path):
        nonlocal late_lane_visible
        closure(path, run=run)
        late_lane_visible = True

    monkeypatch.setattr(
        module,
        "verify_service_snapshot_closure",
        check_closure,
    )
    quiesced = []
    monkeypatch.setattr(
        module, "quiesce_service_snapshot", lambda path, **_kw: quiesced.append(path)
    )

    with pytest.raises(RuntimeError, match="outside service snapshot"):
        module.recover_uninstall(state, root=root, boot=boot)

    assert (root / "etc/aicc-installed").read_bytes() == b"installed"
    assert current.readlink() == Path(f"releases/{'b' * 40}")
    assert (state / "uninstall.json").exists()
    assert snapshot.exists()
    assert quiesced == [snapshot]


def test_snapshot_closure_fails_closed_when_systemd_inventory_fails(tmp_path):
    module = _module()
    snapshot = tmp_path / "uninstall-units.json"
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="inventory failed")

    with pytest.raises(RuntimeError, match="inventory failed"):
        module.verify_service_snapshot_closure(snapshot, run=run)


@pytest.mark.parametrize("boot", [False, True])
def test_install_recovery_rechecks_closure_after_quiesce_before_mutation(
    monkeypatch, tmp_path, boot
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()
    snapshot = state / "attempt-units.json"
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    checks = 0

    def closure(path):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("worker lanes exist outside service snapshot: late")

    quiesced = []
    monkeypatch.setattr(module, "verify_service_snapshot_closure", closure)
    monkeypatch.setattr(
        module, "quiesce_service_snapshot", lambda path, **_kw: quiesced.append(path)
    )

    with pytest.raises(RuntimeError, match="outside service snapshot"):
        transaction.recover(boot=boot)

    assert (root / "etc/new").read_bytes() == b"installed"
    assert transaction.pending.exists()
    assert snapshot.exists()
    assert quiesced == [snapshot]


def test_pending_release_without_install_journal_is_unreachable(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    state.mkdir()
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'a' * 40}")
    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'b' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    with pytest.raises(RuntimeError, match="without install journal"):
        module.FileTransaction(root, state).recover()

    assert current.readlink() == Path(f"releases/{'a' * 40}")
    assert pending_release.exists()

    source = tmp_path / "source"
    source.write_bytes(b"installed")
    with pytest.raises(RuntimeError, match="blocks a new install"):
        module.FileTransaction(root, state).prepare(
            (_spec(module, source, "/etc/new"),)
        )
    assert not (root / "etc/new").exists()
    assert pending_release.exists()


@pytest.mark.parametrize("phase", ["PREPARED", "APPLYING"])
@pytest.mark.parametrize("boot", [False, True])
def test_recovery_rejects_selector_marker_paired_with_unapplied_install(
    tmp_path, phase, boot
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction._write_journal(manifest, phase, 0)
    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'b' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'a' * 40}")

    with pytest.raises(RuntimeError, match="not paired with an applied install"):
        transaction.recover(boot=boot)

    assert current.readlink() == Path(f"releases/{'a' * 40}")
    assert not (root / "etc/new").exists()
    assert transaction.pending.exists()
    assert pending_release.exists()


@pytest.mark.parametrize("name", ["pending.json", "pending-release"])
def test_transaction_recovery_rejects_dangling_journal_path(tmp_path, name):
    module = _module()
    state = tmp_path / "state"
    state.mkdir()
    (state / name).symlink_to(state / "missing")

    with pytest.raises((RuntimeError, OSError)):
        module.FileTransaction(tmp_path / "root", state).recover()


def test_mutation_guards_reject_dangling_uninstall_journal(tmp_path):
    module = _module()
    state = tmp_path / "state"
    state.mkdir()
    (state / "uninstall.json").symlink_to(state / "missing")
    parser = argparse.ArgumentParser()

    for action in ("recovery-anchor-install", "validate"):
        args = SimpleNamespace(
            action=action,
            state_dir=state,
            repo_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="unfinished uninstall"):
            module._dispatch(args, parser)

    with pytest.raises((RuntimeError, OSError)):
        module.begin_uninstall(
            state,
            baseline_selector="ABSENT",
            current_selector=tmp_path / "current",
            lane_registry=tmp_path / "lanes",
        )


def test_uninstall_completion_keeps_wal_until_all_adjuncts_are_durable(
    monkeypatch, tmp_path
):
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    current = tmp_path / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = tmp_path / "worker-lanes"
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    snapshot = state / "uninstall-units.json"
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    snapshot.write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    snapshot.chmod(0o600)
    module.arm_uninstall(state, snapshot)
    for name in ("baseline-units.json", "baseline-release", "attempt-units.json"):
        (state / name).write_text("state", encoding="utf-8")
    real_unlink = module.Path.unlink

    def crash_mid_cleanup(self, *args, **kwargs):
        if self == state / "baseline-release":
            raise OSError("injected cleanup crash")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", crash_mid_cleanup)
    with pytest.raises(OSError, match="cleanup crash"):
        module.complete_uninstall(state, snapshot)
    monkeypatch.setattr(module.Path, "unlink", real_unlink)

    assert module.uninstall_phase(state) == "COMPLETING"
    args = SimpleNamespace(action="validate", state_dir=state)
    with pytest.raises(RuntimeError, match="unfinished uninstall"):
        module._dispatch(args, argparse.ArgumentParser())
    module.complete_uninstall(state, snapshot)
    assert not (state / "uninstall.json").exists()
    assert not any(
        (state / name).exists()
        for name in (
            "uninstall-units.json",
            "baseline-units.json",
            "baseline-release",
            "attempt-units.json",
        )
    )


def test_uninstall_baseline_selection_requires_matching_armed_journal(
    monkeypatch, tmp_path
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    (root / "opt/aicc").mkdir(parents=True)
    state.mkdir(mode=0o700)
    transaction = module.FileTransaction(root, state)
    monkeypatch.setattr(transaction, "verify_release_selection", lambda value: None)

    with pytest.raises((FileNotFoundError, RuntimeError)):
        transaction.select_uninstall_baseline("ABSENT")
    current = root / "opt/aicc/current"
    current.symlink_to(f"releases/{'b' * 40}")
    lanes = root / "etc/aicc/worker-lanes"
    lanes.parent.mkdir(parents=True)
    lanes.write_text("blue\n", encoding="utf-8")
    lanes.chmod(0o644)
    module.begin_uninstall(
        state,
        baseline_selector="ABSENT",
        current_selector=current,
        lane_registry=lanes,
    )
    with pytest.raises(RuntimeError, match="armed journal"):
        transaction.select_uninstall_baseline("ABSENT")


def test_release_selection_arms_pending_selector_without_clobber(monkeypatch, tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    release_id = "b" * 40
    release = root / "opt/aicc/releases" / release_id
    release.mkdir(parents=True)
    current = root / "opt/aicc/current"
    current.symlink_to(f"releases/{'a' * 40}")
    transaction = module.FileTransaction(root, state)
    state.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction.prepare((_spec(module, source, "/etc/aicc-installed"),))
    transaction.apply()
    monkeypatch.setattr(module, "verify_release_manifest", lambda *a, **k: [])

    assert transaction.select_release(release_id, tmp_path) == f"releases/{'a' * 40}"
    assert transaction.pending_release.read_text(encoding="ascii").strip() == (
        f"releases/{'a' * 40}"
    )
    assert current.readlink() == Path(f"releases/{release_id}")

    with pytest.raises(RuntimeError, match="pending release selector"):
        transaction.select_release(release_id, tmp_path)


def test_recovery_generator_is_a_permanent_pretransaction_anchor(tmp_path):
    module = _module()
    repo = Path(__file__).parents[2]
    specs = module.default_specs(
        repo,
        authority_env=tmp_path / "authority.env",
        claude_auth=tmp_path / "claude.json",
        codex_auth=tmp_path / "codex.json",
        resolve_identities=False,
    )
    assert module.RECOVERY_ANCHOR_TARGET not in {spec.target for spec in specs}
    installer = (
        Path(__file__).parents[2] / "deploy/install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")
    assert installer.index("run_transaction recovery-anchor-install") < installer.index(
        "run_transaction prepare"
    )
    by_target = {spec.target: spec for spec in specs}
    for target in (
        "/var/lib/aicc-agent/claude/.claude/.credentials.json",
        "/var/lib/aicc-agent/codex/.codex/auth.json",
    ):
        assert (
            by_target[target].uid,
            by_target[target].gid,
            by_target[target].mode,
        ) == (
            0,
            0,
            0o600,
        )


def test_same_boot_barrier_is_active_before_first_wal_or_claimer_start():
    installer = (
        Path(__file__).parents[2] / "deploy/install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")
    commands = [line.strip() for line in installer.splitlines()]
    anchor = commands.index("run_transaction recovery-anchor-install")
    inline_recover = commands.index("run_transaction recover", anchor)
    reload_units = commands.index("systemctl daemon-reload", inline_recover)
    activate = commands.index(
        "systemctl start aicc-principal-recovery.service", reload_units
    )
    prepare = commands.index("run_transaction prepare")
    launcher = commands.index("systemctl enable --now aicc-agent-launcher.socket")

    assert anchor < inline_recover < reload_units < activate < prepare < launcher
    assert "systemctl enable aicc-principal-recovery.service" not in installer


def test_boot_recovery_restores_dynamic_worker_and_auxiliary_unit_state(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    },
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": False,
                        "active": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        action = command[1]
        unit = command[2] if len(command) > 2 else ""
        stdout = ""
        if action == "is-active":
            stdout = "active\n" if unit.startswith("voyn-aicc-worker") else "inactive\n"
        elif action == "is-enabled":
            stdout = (
                "enabled\n" if unit.startswith("voyn-aicc-worker") else "disabled\n"
            )
        elif action == "show":
            stdout = (
                "loaded\n"
                if "LoadState" in command
                else ("1234\n" if unit.startswith("voyn-aicc-worker") else "0\n")
            )
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    module.restore_service_snapshot(snapshot, run=run)

    mutations = [
        command
        for command in calls
        if command[1] in {"daemon-reload", "enable", "disable", "start", "stop"}
    ]
    assert mutations == [
        ["/usr/bin/systemctl", "daemon-reload"],
        ["/usr/bin/systemctl", "enable", "voyn-aicc-worker@blue.service"],
        ["/usr/bin/systemctl", "start", "voyn-aicc-worker@blue.service"],
        ["/usr/bin/systemctl", "disable", "aicc-agent-launcher.socket"],
        ["/usr/bin/systemctl", "stop", "aicc-agent-launcher.socket"],
    ]


def test_failed_boot_service_restore_keeps_journal_for_retry(monkeypatch, tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()
    (state / "attempt-units.json").write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        module, "verify_service_snapshot_closure", lambda path: None
    )
    monkeypatch.setattr(
        module,
        "restore_service_snapshot",
        lambda path: (_ for _ in ()).throw(RuntimeError("systemd unavailable")),
    )

    with pytest.raises(RuntimeError, match="systemd unavailable"):
        transaction.recover()

    assert transaction.pending.exists()
    monkeypatch.setattr(module, "restore_service_snapshot", lambda path, **_kw: None)
    transaction.recover()
    assert not (root / "etc/new").exists()
    assert not transaction.pending.exists()


def test_compare_and_restore_refuses_changed_generation_target(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    target = root / "etc/target"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    target.chmod(0o600)
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/target", 0o640),))
    transaction.apply()
    target.write_bytes(b"unexpected third-party mutation")

    with pytest.raises(RuntimeError, match="compare-and-restore"):
        transaction.recover()

    assert transaction.pending.exists()
    assert target.read_bytes() == b"unexpected third-party mutation"
    target.write_bytes(b"installed")
    target.chmod(0o640)
    transaction.recover()
    assert target.read_bytes() == b"before"
    assert not transaction.pending.exists()


def test_compare_and_restore_refuses_generation_mode_drift(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    target = root / "etc/target"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    target.chmod(0o600)
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/target", 0o640),))
    transaction.apply()
    target.chmod(0o600)

    with pytest.raises(RuntimeError, match="compare-and-restore"):
        transaction.recover()

    assert transaction.pending.exists()
    target.chmod(0o640)
    transaction.recover()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_absent_unit_restore_verifies_final_stop_disable_state(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-principal-recovery.service": {
                        "exists": False,
                        "enabled": False,
                        "active": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def run(command, **kwargs):
        action = command[1]
        stdout = {
            "is-active": "active\n",
            "is-enabled": "enabled\n",
            "show": "loaded\n",
        }.get(action, "")
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    with pytest.raises(RuntimeError, match="did not restore exactly"):
        module.restore_service_snapshot(snapshot, run=run)


def test_boot_restore_queues_active_worker_without_dependency_deadlock(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--property=LoadState" in argv:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if "--property=MainPID" in argv:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        if argv[1] == "is-active":
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if argv[1] == "is-enabled":
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.restore_service_snapshot(snapshot, run=run, defer_starts=True)

    assert [
        "/usr/bin/systemctl",
        "--no-block",
        "start",
        "voyn-aicc-worker@blue.service",
    ] in calls
    assert [
        "/usr/bin/systemctl",
        "start",
        "voyn-aicc-worker@blue.service",
    ] not in calls


def test_boot_restore_never_synchronously_stops_its_own_recovery_service(
    tmp_path,
):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-principal-recovery.service": {
                        "exists": False,
                        "enabled": False,
                        "active": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--property=LoadState" in argv:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if "--property=MainPID" in argv:
            return SimpleNamespace(
                returncode=0, stdout=f"{os.getpid()}\n", stderr=""
            )
        if argv[1] == "is-active":
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if argv[1] == "is-enabled":
            return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.restore_service_snapshot(snapshot, run=run, defer_starts=True)

    assert [
        "/usr/bin/systemctl",
        "stop",
        "aicc-principal-recovery.service",
    ] not in calls


def test_boot_restore_existing_inactive_recovery_defers_self_stop(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-principal-recovery.service": {
                        "exists": True,
                        "enabled": False,
                        "active": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--property=LoadState" in argv:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if "--property=MainPID" in argv:
            return SimpleNamespace(
                returncode=0, stdout=f"{os.getpid()}\n", stderr=""
            )
        if argv[1] == "is-active":
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if argv[1] == "is-enabled":
            return SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.restore_service_snapshot(snapshot, run=run, defer_starts=True)

    assert not any(
        argv[1] in {"start", "stop"}
        and argv[-1] == "aicc-principal-recovery.service"
        for argv in calls
    )


def test_invalid_source_is_rejected_before_any_target_mutation(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    target = root / "etc/target"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before")
    real_source = tmp_path / "source"
    real_source.write_bytes(b"after")
    alias = tmp_path / "alias"
    alias.symlink_to(real_source)

    with pytest.raises(ValueError, match="regular file"):
        module.FileTransaction(root, state).install(
            (_spec(module, alias, "/etc/target"),)
        )

    assert target.read_bytes() == b"before"
    assert not state.exists()


def test_prepare_refuses_to_clobber_a_pending_transaction(tmp_path):
    """An interrupted install (pending.json still present) must make the next
    prepare() refuse instead of silently overwriting the journal and orphaning
    the prior generation's backups (independent-review finding on 363e91d)."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"one")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/target.bin"),))
    assert transaction.pending.exists()
    with pytest.raises(RuntimeError, match="pending install transaction"):
        transaction.prepare((_spec(module, source, "/etc/target.bin"),))


def test_target_rejects_or_contains_double_leading_slash(tmp_path):
    """ "//etc/x" minus one slash is still absolute and would discard root in
    the join, escaping the sandbox root entirely (review finding on d661d8f)."""
    module = _module()
    transaction = module.FileTransaction(tmp_path / "root", tmp_path / "state")
    contained = transaction._target("//etc/x")
    assert contained.is_relative_to(tmp_path / "root")
    with pytest.raises(ValueError):
        transaction._target("///")


def test_recover_finishes_an_interrupted_commit_instead_of_reverting_it(
    monkeypatch, tmp_path
):
    """A crash between commit()'s current.json write and pending.json unlink
    must not make the next recover() revert the already-live generation
    (independent-review finding on 8a881d3)."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    existing = root / "etc/existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"before")
    source = tmp_path / "one"
    source.write_bytes(b"after")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/existing"),))
    transaction.apply()
    real_unlink = module.Path.unlink

    def crash_on_pending_unlink(self, *args, **kwargs):
        if self == transaction.pending:
            raise OSError("injected crash between current.json and unlink")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", crash_on_pending_unlink)
    with pytest.raises(OSError, match="injected crash"):
        transaction.commit()
    monkeypatch.setattr(module.Path, "unlink", real_unlink)
    assert transaction.pending.exists() and transaction.current.exists()

    transaction.recover()

    assert existing.read_bytes() == b"after", "live generation must survive recover"
    assert not transaction.pending.exists()
    assert json.loads(transaction.current.read_text())["manifest"]


def test_commit_crash_after_pending_release_unlink_cannot_restore_old_selector(
    monkeypatch, tmp_path
):
    """The rollback selector is retired durably before the transaction WAL.

    A crash while unlinking pending.json must leave recovery in COMMITTING,
    never with an old pending-release that can revert the already-live code.
    """
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "payload"
    source.write_bytes(b"new")
    current_release = root / "opt/aicc/current"
    current_release.parent.mkdir(parents=True)
    current_release.symlink_to(f"releases/{'b' * 40}")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/payload"),))
    transaction.apply()
    transaction.pending_release.write_text(
        f"releases/{'a' * 40}\n", encoding="ascii"
    )
    transaction.pending_release.chmod(0o600)
    real_unlink = module.Path.unlink

    def crash_on_pending_unlink(self, *args, **kwargs):
        if self == transaction.pending:
            raise OSError("crash after selector retirement")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", crash_on_pending_unlink)
    with pytest.raises(OSError, match="selector retirement"):
        transaction.commit()
    monkeypatch.setattr(module.Path, "unlink", real_unlink)

    assert not transaction.pending_release.exists()
    assert transaction.pending.exists()
    transaction.recover()

    assert current_release.readlink() == Path(f"releases/{'b' * 40}")
    assert not transaction.pending.exists()


def test_committing_recovery_can_crash_after_selector_retirement_and_retry(
    monkeypatch, tmp_path
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "payload"
    source.write_bytes(b"new")
    current_release = root / "opt/aicc/current"
    current_release.parent.mkdir(parents=True)
    current_release.symlink_to(f"releases/{'b' * 40}")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/payload"),))
    transaction.apply()
    transaction.pending_release.write_text(
        f"releases/{'a' * 40}\n", encoding="ascii"
    )
    transaction.pending_release.chmod(0o600)
    real_unlink = module.Path.unlink

    def crash_commit_selector_unlink(self, *args, **kwargs):
        if self == transaction.pending_release:
            raise OSError("crash before selector retirement")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", crash_commit_selector_unlink)
    with pytest.raises(OSError, match="before selector retirement"):
        transaction.commit()
    monkeypatch.setattr(module.Path, "unlink", real_unlink)
    assert transaction.pending_release.exists()

    def crash_recovery_pending_unlink(self, *args, **kwargs):
        if self == transaction.pending:
            raise OSError("crash after selector retirement")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(module.Path, "unlink", crash_recovery_pending_unlink)
    with pytest.raises(OSError, match="after selector retirement"):
        transaction.recover()
    monkeypatch.setattr(module.Path, "unlink", real_unlink)
    assert not transaction.pending_release.exists()
    assert transaction.pending.exists()

    transaction.recover()
    assert current_release.readlink() == Path(f"releases/{'b' * 40}")
    assert not transaction.pending.exists()


def test_recover_restores_release_selector_before_any_service_snapshot(
    monkeypatch, tmp_path
):
    """A prior test of this name created no `pending.json` and never
    instrumented `restore_service_snapshot`, so `recover()`'s early
    no-pending-install branch (which never touches a service snapshot at
    all) satisfied it trivially -- an implementation that restored services
    *before* the release selector inside the real interrupted-install branch
    would still have passed (review finding on 5f2f1dd). Exercise that
    branch for real: a pending APPLIED generation plus a pending release
    selector plus an actual service snapshot, and record the call order of
    both restorations."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    release_dir = current.parent / "releases" / ("a" * 40)
    release_dir.mkdir(parents=True)
    (release_dir / "marker").write_text("release\n", encoding="utf-8")
    source = tmp_path / "source"
    source.write_bytes(b"installed")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()
    assert json.loads(transaction.pending.read_text())["phase"] == "APPLIED"

    # Recovery now proves a release before selecting it, so this interrupted
    # generation needs the root-owned manifest its staging step would have
    # written (independent review on cacfc257). Created after `apply()` so the
    # transaction owns the state directory's mode, exactly as the installer's
    # `install -d -m 0700` does in production.
    (state / "releases").mkdir(mode=0o700, parents=True, exist_ok=True)
    module.record_release_manifest(
        release_dir,
        transaction.release_manifest_path("a" * 40),
        "a" * 40,
        trusted_uid=os.geteuid(),
        trusted_gid=os.getegid(),
    )

    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    (state / "attempt-units.json").write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )

    order: list[str] = []
    original_selector_restore = module.FileTransaction._restore_release_selector

    def recording_quiesce(path, **_kw):
        assert path == state / "attempt-units.json"
        order.append("quiesce")

    def recording_selector_restore(self, **kwargs):
        order.append("selector")
        return original_selector_restore(self, **kwargs)

    def recording_closure(path):
        assert path == state / "attempt-units.json"
        order.append("closure")

    def recording_service_restore(path):
        assert path == state / "attempt-units.json"
        order.append("services")

    monkeypatch.setattr(
        module.FileTransaction,
        "_restore_release_selector",
        recording_selector_restore,
    )
    monkeypatch.setattr(module, "quiesce_service_snapshot", recording_quiesce)
    monkeypatch.setattr(
        module, "verify_service_snapshot_closure", recording_closure
    )
    monkeypatch.setattr(module, "restore_service_snapshot", recording_service_restore)

    transaction.recover()

    assert order == [
        "closure",
        "quiesce",
        "closure",
        "selector",
        "services",
        "closure",
    ], order
    assert current.readlink() == Path(f"releases/{'a' * 40}")
    assert not pending_release.exists()
    assert not (root / "etc/new").exists()
    assert not transaction.pending.exists()


def test_recover_refuses_a_selector_to_a_missing_release(tmp_path):
    """A stale pending-release must not point the live selector into a
    missing directory -- every worker ExecStart would dereference it
    (independent-review finding on 6e22b93)."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    state.mkdir()
    pending = state / "pending-release"
    pending.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending.chmod(0o600)
    transaction = module.FileTransaction(root, state)
    with pytest.raises(RuntimeError, match="without install journal"):
        transaction.recover()
    assert current.readlink() == Path(f"releases/{'b' * 40}")


def test_recovery_itself_refuses_an_unattested_pending_release(monkeypatch, tmp_path):
    """Independent review on cacfc257.

    Proving a release is only worth anything if the recovery path actually
    calls the proof. This drives `recover()` through its real interrupted-
    generation branch with a `pending-release` whose release directory exists
    and looks right but carries no root-owned manifest, and requires the
    selector NOT to be repointed at it.

    Deliberately written against `recover()` rather than against
    `verify_release_selection` directly: a test of the helper alone still
    passes when the call site is deleted, which is exactly the regression that
    matters here.
    """
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    current = root / "opt/aicc/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(f"releases/{'b' * 40}")
    release = current.parent / "releases" / ("a" * 40)
    release.mkdir(parents=True)
    (release / "marker").write_text("unattested\n", encoding="utf-8")
    source = tmp_path / "source"
    source.write_bytes(b"installed")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()

    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    (state / "attempt-units.json").write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        module, "verify_service_snapshot_closure", lambda path: None
    )
    monkeypatch.setattr(module, "restore_service_snapshot", lambda *a, **k: None)

    with pytest.raises(module.ReleaseRefused):
        transaction.recover()

    # The live selector still points where it did; an unproven release was
    # never selected, and the pending record survives for a later retry.
    assert os.readlink(current) == f"releases/{'b' * 40}"
    assert pending_release.exists()


def test_a_legacy_symlink_target_is_replaced_and_restored(monkeypatch, tmp_path):
    """Found on the first live worker install, not by any test here.

    Every production unit is still a symlink from /etc/systemd/system into the
    operator's home. Taking those units under repository ownership means
    replacing the link with the repository's own root-owned file -- but prepare
    refused any target that was not a regular file, so the install stopped at
    `existing target is not a regular file`.

    A symlink is now recorded (by its literal target, never followed) and
    replaced; rollback puts the LINK back, not a file, because leaving a
    regular file where a link belonged silently changes what the unit
    resolves to.
    """
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    elsewhere = tmp_path / "home" / "unit.service"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("legacy unit\n", encoding="utf-8")
    target_dir = root / "etc"
    target_dir.mkdir(parents=True)
    (target_dir / "unit.service").symlink_to(elsewhere)

    source = tmp_path / "source"
    source.write_bytes(b"repo-owned unit\n")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/unit.service"),))
    transaction.apply()

    installed = target_dir / "unit.service"
    assert not installed.is_symlink(), "the link must have been replaced by a file"
    assert installed.read_bytes() == b"repo-owned unit\n"
    # The link's own target is untouched -- we replaced the link, not what it
    # pointed at.
    assert elsewhere.read_text(encoding="utf-8") == "legacy unit\n"

    transaction.recover()

    assert installed.is_symlink(), "rollback must restore the link, not a file"
    assert os.readlink(installed) == str(elsewhere)


def test_a_target_that_is_neither_file_nor_symlink_is_still_refused(tmp_path):
    """Allowing symlinks must not have opened the door to anything else."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    target_dir = root / "etc"
    target_dir.mkdir(parents=True)
    (target_dir / "unit.service").mkdir()

    source = tmp_path / "source"
    source.write_bytes(b"repo-owned unit\n")

    transaction = module.FileTransaction(root, state)
    with pytest.raises(ValueError, match="not a regular file"):
        transaction.prepare((_spec(module, source, "/etc/unit.service"),))


def test_symlink_restore_is_idempotent(monkeypatch, tmp_path):
    """Independent review on 2b8826a.

    `restore` read the target with `_read_regular` before looking at the
    record, and that call opens with O_NOFOLLOW -- so on a target that is still
    (or already again) a symlink it raises, and recovery aborted with "target
    shape changed" on a generation that was simply not applied yet or already
    rolled back. Boot recovery runs unattended and can itself be interrupted,
    so it has to be safe to run twice.
    """
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    elsewhere = tmp_path / "home" / "unit.service"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("legacy unit\n", encoding="utf-8")
    (root / "etc").mkdir(parents=True)
    installed = root / "etc" / "unit.service"
    installed.symlink_to(elsewhere)

    source = tmp_path / "source"
    source.write_bytes(b"repo-owned unit\n")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/unit.service"),))
    transaction.apply()

    # `restore` is driven directly, twice, against the same journal: that is
    # what an interrupted boot recovery looks like. Going through `recover()`
    # would delete the journal on the first pass and the second call would
    # silently do nothing, which is why an earlier version of this test passed
    # against the very bug it was written for.
    manifest = next(state.rglob("manifest.json"))
    transaction.restore(manifest, clear_pending=False)
    assert installed.is_symlink()

    transaction.restore(manifest, clear_pending=False)
    assert installed.is_symlink()
    assert os.readlink(installed) == str(elsewhere)


def test_symlink_restore_refuses_a_vanished_target(tmp_path):
    """Independent review on 2b8826a: the symlink branch accepted a missing
    target and recreated the link, papering over whatever removed it. The
    regular-file branch refuses that, and so must this one."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    elsewhere = tmp_path / "home" / "unit.service"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("legacy\n", encoding="utf-8")
    (root / "etc").mkdir(parents=True)
    installed = root / "etc" / "unit.service"
    installed.symlink_to(elsewhere)

    source = tmp_path / "source"
    source.write_bytes(b"repo-owned\n")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/unit.service"),))
    transaction.apply()

    installed.unlink()  # a third party removes the installed file

    manifest = next(state.rglob("manifest.json"))
    with pytest.raises(RuntimeError, match="disappeared"):
        transaction.restore(manifest)


def test_recovery_does_not_demand_a_mainpid_from_a_socket_unit(tmp_path):
    """`MainPID` is a service property. A `.socket` has none, so systemd
    returns an empty value and a non-zero probe — and demanding it anyway made
    recovery unable to prove the state of `aicc-agent-launcher.socket`. A
    recovery that cannot finish leaves its WAL in place, and the retained WAL
    then refuses every install behind it (observed live on worker-01,
    2026-08-31).
    """
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": False,
                        "active": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def run(command, **kwargs):
        action = command[1]
        stdout = ""
        if action == "is-active":
            stdout = "inactive\n"
        elif action == "is-enabled":
            stdout = "disabled\n"
        elif action == "show":
            if "LoadState" in " ".join(command):
                stdout = "loaded\n"
            else:
                # What systemd actually answers for a socket: nothing, and a
                # non-zero status for the unknown property.
                return SimpleNamespace(returncode=1, stderr="", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    module.restore_service_snapshot(snapshot, run=run)


def test_a_service_that_exists_must_still_report_a_mainpid(tmp_path):
    """The relaxation is for units that have no such property, not for
    services that fail to answer — that remains unprovable state."""
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "voyn-aicc-worker@blue.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def run(command, **kwargs):
        action = command[1]
        stdout = ""
        if action == "is-active":
            stdout = "active\n"
        elif action == "is-enabled":
            stdout = "enabled\n"
        elif action == "show":
            if "LoadState" in " ".join(command):
                stdout = "loaded\n"
            else:
                return SimpleNamespace(returncode=1, stderr="", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    with pytest.raises(RuntimeError, match="cannot prove restored service state"):
        module.restore_service_snapshot(snapshot, run=run)


def _quiesce_snapshot(tmp_path, unit: str):
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {unit: {"exists": True, "enabled": True, "active": True}},
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def _not_found_runner():
    def run(command, **kwargs):
        if command[1] == "show" and "LoadState" in " ".join(command):
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    return run


def test_quiesce_proceeds_for_a_unit_this_rollback_will_restore(tmp_path):
    """The deadlock this closes: unit files are restored by `restore()`, which
    runs *after* quiesce. A rollback interrupted between removing a unit file
    and restoring it could never be resumed — every later attempt died here,
    on the unit the previous attempt was replacing. Four consecutive install
    attempts on worker-01 each stopped on the next such unit.
    """
    module = _module()
    unit = "voyn-aicc-worker.service"
    snapshot = _quiesce_snapshot(tmp_path, unit)

    module.quiesce_service_snapshot(
        snapshot, run=_not_found_runner(), restorable_units=frozenset({unit})
    )


def test_quiesce_still_refuses_a_unit_that_vanished_unexplained(tmp_path):
    """A missing unit nobody intends to restore is real divergence: mutating a
    host whose state cannot be accounted for is exactly what this refusal is
    for. Deliberately not a legacy unit — those are retired on purpose."""
    module = _module()
    snapshot = _quiesce_snapshot(tmp_path, "aicc-agent-launcher.socket")

    with pytest.raises(RuntimeError, match="expected unit disappeared before quiesce"):
        module.quiesce_service_snapshot(snapshot, run=_not_found_runner())


def test_restorable_units_are_read_from_the_journal_not_the_disk(tmp_path):
    module = _module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {"target": "/etc/systemd/system/voyn-aicc-worker.service"},
                    {"target": "/etc/systemd/system/sub/dir/not-a-unit.conf"},
                    {"target": "/etc/aicc/worker-lanes"},
                ]
            }
        ),
        encoding="utf-8",
    )

    units = module._units_restored_by(manifest)

    assert units == frozenset({"voyn-aicc-worker.service"})


def test_quiesce_tolerates_a_legacy_unit_the_rollout_retired(tmp_path):
    """Retiring the pre-template workers is part of installing, and disabling
    a unit whose fragment is a symlink removes that symlink. So after a
    rollout these units are legitimately gone while the snapshot taken before
    it still records them present. Without this, each attempt retired one more
    and the next died on it — four consecutive attempts on worker-01 failed
    this way, on `voyn-aicc-worker-2.service` and then
    `voyn-aicc-worker.service`, with nothing actually wrong.
    """
    module = _module()
    snapshot = _quiesce_snapshot(tmp_path, "voyn-aicc-worker-2.service")

    module.quiesce_service_snapshot(snapshot, run=_not_found_runner())


def test_the_retired_legacy_set_matches_the_rollout_and_the_installer():
    """Three copies of one list: this module (imported by the bootstrap before
    the rollout module exists on disk), the rollout, and the installer's
    `--include-unit` arguments. A unit added to one and forgotten in another
    reintroduces exactly the deadlock this closes."""
    module = _module()
    root = Path(__file__).parents[2]

    rollout_src = (root / "ops" / "aicc_staged_worker_rollout.py").read_text(
        encoding="utf-8"
    )
    block = rollout_src.split("LEGACY_WORKER_UNITS = (", 1)[1].split(")", 1)[0]
    rollout_units = {line.strip().strip('",') for line in block.splitlines() if line.strip()}

    installer = (
        root / "deploy" / "install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")
    included = {
        line.split("--include-unit", 1)[1].strip().rstrip("\\").strip()
        for line in installer.splitlines()
        if "--include-unit" in line
    }

    assert module.RETIRED_LEGACY_UNITS == rollout_units
    assert module.RETIRED_LEGACY_UNITS <= included


def test_a_boot_generated_dropin_is_not_required_to_match():
    """`aicc-principal-recovery` writes its drop-in into
    `/run/systemd/generator.early/`, which is tmpfs — it exists only while
    this boot's generator output is live. Requiring it to match a snapshot
    demands a value that by construction does not persist, and refused the
    restore on a host where nothing was wrong (worker-01, 2026-08-31).
    """
    module = _module()
    snapshotted = "/run/systemd/generator.early/voyn-aicc-worker.service.d/10-aicc-recovery.conf"

    assert module._properties_match("DropInPaths", "", snapshotted)


def test_an_administrator_dropin_must_still_match():
    """The check exists because a silently added drop-in can weaken the
    isolation the snapshot preserves. Only the generated ones are exempt."""
    module = _module()
    expected = "/etc/systemd/system/voyn-aicc-worker.service.d/20-principal-isolation.conf"

    assert not module._properties_match("DropInPaths", "", expected)
    assert module._properties_match("DropInPaths", expected, expected)


def test_dropin_comparison_ignores_order():
    module = _module()
    a = "/etc/systemd/system/x.d/10-a.conf /etc/systemd/system/x.d/20-b.conf"
    b = "/etc/systemd/system/x.d/20-b.conf /etc/systemd/system/x.d/10-a.conf"

    assert module._properties_match("DropInPaths", a, b)


def test_a_command_property_still_ignores_only_its_invocation_fields():
    """The other renderer whose text moves without the configuration moving.
    Routed through the same matcher, so both stay in one place."""
    module = _module()
    snapshot = "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m w ; pid=0 }"
    after_run = "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m w ; pid=76841 }"
    replaced = "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m attacker ; pid=0 }"

    assert module._properties_match("ExecStart", after_run, snapshot)
    assert not module._properties_match("ExecStart", replaced, snapshot)


def test_restore_does_not_revive_a_legacy_unit_the_rollout_retired(tmp_path):
    """Retiring the pre-template workers is what installing *does*, and
    `disable` removes the symlink that was their fragment. The snapshot still
    describes the configuration from before that, so restoring it would revive
    a unit the rollout just took out of service — and, the unit being gone,
    every property it recorded now reads empty and the comparison refuses.

    That is exactly what the live host produced, one property at a time:
    `DropInPaths`, then `EnvironmentFiles`, each on a unit that was correctly
    absent (worker-01, 2026-08-31).
    """
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 3,
                "units": {
                    "voyn-aicc-worker.service": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                        "properties": dict.fromkeys(_module().SNAPSHOT_PROPERTIES, ""),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    mutations: list[tuple[str, ...]] = []

    def run(command, **kwargs):
        action = command[1]
        if action in {"enable", "disable", "start", "stop", "daemon-reload"}:
            mutations.append(tuple(command[1:]))
        if action == "show" and "LoadState" in " ".join(command):
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        if action == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if action == "is-active":
            return SimpleNamespace(returncode=0, stderr="", stdout="inactive\n")
        if action == "is-enabled":
            return SimpleNamespace(returncode=0, stderr="", stdout="disabled\n")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    module.restore_service_snapshot(snapshot, run=run)

    assert not any(m[0] in {"enable", "start"} for m in mutations)


def test_restore_still_refuses_a_non_legacy_unit_that_lost_its_properties(tmp_path):
    """The exemption is for units the installer retires on purpose. Anything
    else whose recorded configuration no longer matches is still a refusal."""
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 3,
                "units": {
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                        "properties": {
                            **dict.fromkeys(_module().SNAPSHOT_PROPERTIES, ""),
                            "DropInPaths": "/etc/systemd/system/x.d/a.conf",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def run(command, **kwargs):
        if command[1] == "show" and "LoadState" in " ".join(command):
            return SimpleNamespace(returncode=0, stderr="", stdout="loaded\n")
        if command[1] == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with pytest.raises(RuntimeError, match="refusing unsafe snapshot restart"):
        module.restore_service_snapshot(snapshot, run=run)


# ---------------------------------------------------------------------------
# Removal specs: `removal_spec()` folds "this target must not exist" into the
# same generation as ordinary installs. Built for the control profile's
# worker-only purge (VOYN-W0-AICC-INSTALLER-HAS-NO-CONTROL-PROFILE): dropping
# a target from the spec list only stops a transaction from *writing* it, it
# does nothing about what an earlier worker install already left on disk, and
# a later "uninstall the worker profile, then install control" split leaves a
# host with neither installation if the second half fails. One generation,
# one prepare/apply/commit, one rollback boundary for both directions.
# ---------------------------------------------------------------------------


def test_removal_spec_purges_a_preexisting_target_atomically_with_an_install(
    tmp_path,
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "var/lib/aicc-agent/claude/.claude/.credentials.json"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"agent-secret")
    worker_only.chmod(0o600)
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            module.removal_spec("/var/lib/aicc-agent/claude/.claude/.credentials.json"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )

    assert not worker_only.exists()
    assert (root / "etc/control-file").read_bytes() == b"control-only"


def test_removal_spec_is_a_noop_when_the_target_never_existed(tmp_path):
    """A host that never ran the worker profile has nothing to purge -- the
    purge must not invent the target just to delete it."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            module.removal_spec("/etc/aicc/agent.env"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )

    assert not (root / "etc/aicc/agent.env").exists()
    assert (root / "etc/control-file").read_bytes() == b"control-only"

    # Idempotent: running it again against an already-purged host is still a
    # no-op, not a refusal.
    control_source.write_bytes(b"control-only-two")
    transaction.install(
        (
            module.removal_spec("/etc/aicc/agent.env"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )
    assert not (root / "etc/aicc/agent.env").exists()
    assert (root / "etc/control-file").read_bytes() == b"control-only-two"


def test_a_failure_after_the_purge_rolls_the_purge_back_too(monkeypatch, tmp_path):
    """The exact regression an earlier attempt shipped: a worker-to-control
    transition that purges worker artefacts and then fails installing the
    control specs must not leave a host with neither generation. The purge
    is a record in this same generation, so `apply()`'s existing failure path
    -- restore every record, including ones already applied -- undoes it
    like any other mutation, and the pre-transaction file is exactly back."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "var/lib/aicc-agent/codex/.codex/auth.json"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"codex-secret")
    worker_only.chmod(0o600)
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    transaction.prepare(
        (
            # Purge record first: apply() removes it before the injected
            # failure below, so the rollback has to undo a completed removal,
            # not merely skip one that never ran.
            module.removal_spec("/var/lib/aicc-agent/codex/.codex/auth.json"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )
    real_atomic = module._atomic_bytes

    def fail_the_control_install(path, *args, **kwargs):
        if path == root / "etc/control-file":
            raise OSError("injected post-purge failure")
        return real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_bytes", fail_the_control_install)

    with pytest.raises(OSError, match="injected post-purge failure"):
        transaction.apply()

    assert worker_only.read_bytes() == b"codex-secret"
    assert stat.S_IMODE(worker_only.stat().st_mode) == 0o600
    assert not (root / "etc/control-file").exists()
    assert not transaction.pending.exists()
    assert not list(state.glob("generation-*"))


def test_removal_record_recovers_from_a_crash_between_purge_and_install(tmp_path):
    """The write-ahead index makes every mutation recoverable one record at a
    time. A crash after the purge applied but before the next record starts
    must resume to the identical pre-transaction state as a synchronous
    failure at the same point."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "etc/aicc/worker-lanes"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"lane-registry")
    worker_only.chmod(0o644)
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare(
        (
            module.removal_spec("/etc/aicc/worker-lanes"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )

    # Simulate apply() having durably logged and completed only the first
    # (removal) record before the process died.
    transaction._write_journal(manifest, "APPLYING", 0)
    worker_only.unlink()

    module.FileTransaction(root, state).recover()

    assert worker_only.read_bytes() == b"lane-registry"
    assert not (root / "etc/control-file").exists()
    assert not (state / "pending.json").exists()
    assert not list(state.glob("generation-*"))


def test_uninstall_all_restores_a_purged_target_through_the_generation_chain(
    tmp_path,
):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "etc/aicc/agent.env"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"agent-env")
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            module.removal_spec("/etc/aicc/agent.env"),
            _spec(module, control_source, "/etc/control-file"),
        )
    )
    assert not worker_only.exists()

    transaction.uninstall_all()

    assert worker_only.read_bytes() == b"agent-env"
    assert not (root / "etc/control-file").exists()
    assert not transaction.current.exists()
    assert not transaction.pending.exists()
    assert not list(state.glob("generation-*"))


def test_removal_spec_restores_a_legacy_symlink_it_replaced(monkeypatch, tmp_path):
    """A worker-only target can itself be one of the still-live legacy
    symlinks (see `test_a_legacy_symlink_target_is_replaced_and_restored`).
    Purging it must record and restore the link, not treat it as a regular
    file that happens to be gone."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    elsewhere = tmp_path / "home" / "aicc-worker.service"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("legacy unit\n", encoding="utf-8")
    target_dir = root / "etc/systemd/system"
    target_dir.mkdir(parents=True)
    (target_dir / "aicc-worker.service").symlink_to(elsewhere)

    transaction = module.FileTransaction(root, state)
    transaction.prepare((module.removal_spec("/etc/systemd/system/aicc-worker.service"),))
    transaction.apply()

    installed = target_dir / "aicc-worker.service"
    assert not installed.exists() and not installed.is_symlink()
    # The link's own target is untouched -- removal drops the link, not what
    # it pointed at.
    assert elsewhere.read_text(encoding="utf-8") == "legacy unit\n"

    transaction.recover()

    assert installed.is_symlink(), "rollback must restore the link, not a file"
    assert os.readlink(installed) == str(elsewhere)


def test_quiesce_worker_only_units_stops_and_disables_what_is_loaded(tmp_path):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        if command[1] == "list-unit-files":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if command[1] == "list-units":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="voyn-aicc-worker@a.service loaded active running\n",
            )
        if command[1] == "show" and command[2] == "aicc-agent-launcher.socket":
            return SimpleNamespace(returncode=0, stderr="", stdout="loaded\n")
        if command[1] == "show" and command[2] == "voyn-aicc-worker@a.service":
            return SimpleNamespace(returncode=0, stderr="", stdout="loaded\n")
        if command[1] == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        if command[1] == "disable":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected systemctl call: {command}")

    module.quiesce_worker_only_units(run=run)

    stopped = {c[3] for c in calls if c[1] == "disable"}
    assert stopped == {"aicc-agent-launcher.socket", "voyn-aicc-worker@a.service"}


def test_quiesce_worker_only_units_tolerates_a_host_that_never_ran_the_worker_profile(
    tmp_path,
):
    module = _module()

    def run(command, **kwargs):
        if command[1] in {"list-unit-files", "list-units"}:
            return SimpleNamespace(returncode=1, stderr="", stdout="")
        if command[1] == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        raise AssertionError(f"unexpected systemctl call: {command}")

    module.quiesce_worker_only_units(run=run)


def test_quiesce_worker_only_units_refuses_when_stopping_a_loaded_unit_fails(
    tmp_path,
):
    module = _module()

    def run(command, **kwargs):
        if command[1] in {"list-unit-files", "list-units"}:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if command[1] == "show" and command[2] == "aicc-agent-launcher.socket":
            return SimpleNamespace(returncode=0, stderr="", stdout="loaded\n")
        if command[1] == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        if command[1] == "disable":
            return SimpleNamespace(returncode=1, stderr="denied", stdout="")
        raise AssertionError(f"unexpected systemctl call: {command}")

    with pytest.raises(RuntimeError, match="cannot stop worker-only unit"):
        module.quiesce_worker_only_units(run=run)


# ---------------------------------------------------------------------------
# Compare-and-remove. A removal is the one mutation this transaction cannot
# reconstruct from anything but its own snapshot, so apply() unlinks only a
# target that is still byte-for-byte (or link-for-link) what prepare() saw.
# Drift means some other writer owns that file now; deleting it would destroy
# state this generation never examined and its backup does not describe.
# ---------------------------------------------------------------------------


def _control_shaped_generation(module, tmp_path, *, payload=b"agent-secret"):
    """A prepared generation shaped like a worker→control transition: one
    purge of a pre-existing worker artefact, one ordinary control install."""
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "etc/aicc/agent.env"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(payload)
    worker_only.chmod(0o640)
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare(
        (
            _spec(module, control_source, "/etc/control-file"),
            module.removal_spec("/etc/aicc/agent.env"),
        )
    )
    return transaction, manifest, worker_only, root / "etc/control-file"


def _assert_nothing_was_mutated(transaction, installed):
    """Every refusal below must fail the generation with the host untouched:
    the purge is refused *and* the install that precedes it in spec order has
    not been written, because the check runs before the first mutation."""
    assert not installed.exists(), "a refused purge must not half-apply the generation"
    assert transaction.pending.exists(), "the pending WAL stays for recovery"


def test_apply_refuses_to_remove_a_target_whose_content_drifted(tmp_path):
    module = _module()
    transaction, _manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )

    worker_only.write_bytes(b"rewritten by something else")
    worker_only.chmod(0o640)

    with pytest.raises(RuntimeError, match="purge target changed before compare"):
        transaction.apply()

    assert worker_only.read_bytes() == b"rewritten by something else"
    _assert_nothing_was_mutated(transaction, installed)


def test_apply_refuses_to_remove_a_target_whose_mode_drifted(tmp_path):
    """Same bytes, wider permissions: the snapshot records mode, so a file
    that is no longer the one prepare() approved is not removed."""
    module = _module()
    transaction, _manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )

    worker_only.chmod(0o644)

    with pytest.raises(RuntimeError, match="purge target changed before compare"):
        transaction.apply()

    assert worker_only.exists()
    _assert_nothing_was_mutated(transaction, installed)


def test_apply_refuses_to_remove_a_target_whose_owner_drifted(tmp_path):
    """uid and gid drift cannot be produced without privilege, so the
    recorded expectation is moved instead -- indistinguishable to apply()
    from a chown between prepare() and apply()."""
    module = _module()
    transaction, manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )

    for field in ("original_uid", "original_gid"):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for record in payload["records"]:
            if record["remove"] and record["existed"]:
                record[field] = record[field] + 1
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(RuntimeError, match="purge target changed before compare"):
            transaction.apply()

        assert worker_only.exists()
        _assert_nothing_was_mutated(transaction, installed)


def test_apply_refuses_to_remove_a_symlink_that_now_points_elsewhere(tmp_path):
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    legacy = tmp_path / "home" / "aicc-worker.service"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy unit\n", encoding="utf-8")
    target_dir = root / "etc/systemd/system"
    target_dir.mkdir(parents=True)
    link = target_dir / "aicc-worker.service"
    link.symlink_to(legacy)
    transaction = module.FileTransaction(root, state)
    transaction.prepare(
        (module.removal_spec("/etc/systemd/system/aicc-worker.service"),)
    )

    elsewhere = tmp_path / "home" / "someone-elses.service"
    elsewhere.write_text("not ours\n", encoding="utf-8")
    link.unlink()
    link.symlink_to(elsewhere)

    with pytest.raises(RuntimeError, match="purge target symlink changed"):
        transaction.apply()

    assert os.readlink(link) == str(elsewhere), "the foreign link is left alone"


def test_apply_refuses_to_remove_a_symlink_that_became_a_regular_file(tmp_path):
    """The snapshot is a link's literal target; a regular file at the same
    path is a different object with content no backup here holds."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    legacy = tmp_path / "home" / "aicc-worker.service"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy unit\n", encoding="utf-8")
    target_dir = root / "etc/systemd/system"
    target_dir.mkdir(parents=True)
    link = target_dir / "aicc-worker.service"
    link.symlink_to(legacy)
    transaction = module.FileTransaction(root, state)
    transaction.prepare(
        (module.removal_spec("/etc/systemd/system/aicc-worker.service"),)
    )

    link.unlink()
    link.write_text("a real file now\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="purge target is no longer a symlink"):
        transaction.apply()

    assert link.read_text(encoding="utf-8") == "a real file now\n"


def test_apply_refuses_to_remove_a_target_that_appeared_after_prepare(tmp_path):
    """prepare() recorded absence, so this generation holds no backup for the
    file that appeared since. Removing it would be an unrecoverable delete of
    something the transaction never saw."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    transaction.prepare(
        (
            _spec(module, control_source, "/etc/control-file"),
            module.removal_spec("/etc/aicc/agent.env"),
        )
    )

    appeared = root / "etc/aicc/agent.env"
    appeared.parent.mkdir(parents=True)
    appeared.write_bytes(b"written after prepare")

    with pytest.raises(RuntimeError, match="purge target appeared after prepare"):
        transaction.apply()

    assert appeared.read_bytes() == b"written after prepare"
    _assert_nothing_was_mutated(transaction, root / "etc/control-file")


def test_apply_refuses_when_the_purge_target_vanished_after_prepare(tmp_path):
    """Absence is the intended end state, but reaching it by someone else's
    hand means the backup no longer describes what is (not) there. The
    generation fails closed rather than commit a purge it did not perform."""
    module = _module()
    transaction, _manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )

    worker_only.unlink()

    with pytest.raises(RuntimeError, match="purge target disappeared before removal"):
        transaction.apply()

    _assert_nothing_was_mutated(transaction, installed)


def test_a_refused_purge_leaves_a_generation_recover_puts_back(tmp_path):
    """The refusal is not the end of the story: the durable WAL is retained,
    and once the drift is resolved recovery returns the host to its exact
    pre-transaction state."""
    module = _module()
    transaction, _manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )
    original = worker_only.read_bytes()
    worker_only.write_bytes(b"drifted")
    worker_only.chmod(0o640)

    with pytest.raises(RuntimeError, match="purge target changed before compare"):
        transaction.apply()

    # The operator restores what drifted; recovery then unwinds the untouched
    # generation cleanly.
    worker_only.write_bytes(original)
    worker_only.chmod(0o640)
    module.FileTransaction(transaction.root, transaction.state_dir).recover()

    assert worker_only.read_bytes() == original
    assert not installed.exists()
    assert not transaction.pending.exists()
    assert not list(transaction.state_dir.glob("generation-*"))


def test_a_quiesce_failure_rolls_the_prepared_generation_back_untouched(tmp_path):
    """The installer's order for a control host is prepare -> stop the
    worker-only units -> apply. A failure at the middle step must leave a
    generation that has mutated nothing and recovers completely: the worker
    artefacts are still on disk, still running for the operator to retry, and
    no half-installed control plane exists."""
    module = _module()
    transaction, _manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )

    def run(command, **kwargs):
        if command[1] in {"list-unit-files", "list-units"}:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if command[1] == "show" and command[2] == "aicc-agent-launcher.socket":
            return SimpleNamespace(returncode=0, stderr="", stdout="loaded\n")
        if command[1] == "show":
            return SimpleNamespace(returncode=0, stderr="", stdout="not-found\n")
        if command[1] == "disable":
            return SimpleNamespace(returncode=1, stderr="job failed", stdout="")
        raise AssertionError(f"unexpected systemctl call: {command}")

    with pytest.raises(RuntimeError, match="cannot stop worker-only unit"):
        module.quiesce_worker_only_units(run=run)

    # What the installer's trap does next.
    module.FileTransaction(transaction.root, transaction.state_dir).recover()

    assert worker_only.read_bytes() == b"agent-secret"
    assert stat.S_IMODE(worker_only.stat().st_mode) == 0o640
    assert not installed.exists()
    assert not transaction.pending.exists()
    assert not list(transaction.state_dir.glob("generation-*"))


def test_a_second_control_install_purges_nothing_and_still_succeeds(tmp_path):
    """Idempotence across runs: the first install removes the worker
    artefact, the second finds it already absent and must neither refuse nor
    resurrect it."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    worker_only = root / "etc/aicc/agent.env"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"agent-secret")
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    specs = (
        _spec(module, control_source, "/etc/control-file"),
        module.removal_spec("/etc/aicc/agent.env"),
    )

    transaction.install(specs)
    assert not worker_only.exists()

    transaction.install(specs)

    assert not worker_only.exists()
    assert (root / "etc/control-file").read_bytes() == b"control-only"
    assert not transaction.pending.exists()


def test_quiesce_worker_only_is_refused_outside_the_control_profile(tmp_path):
    """Under any other profile it would stop the very units that profile is
    installing."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir()
    args = SimpleNamespace(
        action="quiesce-worker-only",
        state_dir=state,
        repo_root=tmp_path,
        root=tmp_path / "root",
        profile="worker",
    )

    with pytest.raises(RuntimeError, match="requires the control profile"):
        module._dispatch(args, argparse.ArgumentParser())


def test_the_control_purge_covers_every_retired_worker_unit():
    """A worker unit named in one list and forgotten in the other keeps
    running on a control host whose unit file the same generation removes."""
    module = _module()

    assert module.RETIRED_LEGACY_UNITS < module.CONTROL_PURGE_UNITS
    assert "aicc-agent-launcher.socket" in module.CONTROL_PURGE_UNITS
    # The boot recovery capsule is the one agent-adjacent unit that must keep
    # running: it is what retries an interrupted rollback.
    assert "aicc-principal-recovery.service" not in module.CONTROL_PURGE_UNITS


def test_the_whole_control_generation_validates_as_one_spec_set(tmp_path):
    """End to end on the real spec list: the control profile's installs and
    its purges are one set that `validate_sources` accepts. Absent agent
    credentials -- the file whose absence stopped control-01 -- are no longer
    read as sources, and no purge collides with a target the same generation
    installs (which `validate_sources` would refuse as a duplicate)."""
    module = _module()
    repo = Path(__file__).parents[2]
    authority = tmp_path / "authority.env"
    authority.write_text("AICC_WORKSPACE_ROOTS=/srv/aicc-workspaces\n", encoding="utf-8")
    specs = module.default_specs(
        repo,
        authority_env=authority,
        claude_auth=tmp_path / "absent-claude.json",
        codex_auth=tmp_path / "absent-codex.json",
        resolve_identities=False,
        profile="control",
    )

    validated = module.FileTransaction.validate_sources(specs)

    assert len(validated) == len(specs)
    assert {
        spec.target for spec in specs if spec.remove and not spec.directory
    } == module.WORKER_ONLY_TARGETS
    assert [
        spec.target for spec in specs if spec.remove and spec.directory
    ] == list(module.WORKER_ONLY_DIRECTORIES)
    assert not (tmp_path / "absent-claude.json").exists()


# ---------------------------------------------------------------------------
# Compare-and-destroy without a second name resolution.
#
# Comparing a path and then unlinking the same path resolves that name twice,
# and an untrusted writer of the directory only has to win the gap between the
# two to have a root-run installer destroy a file it never examined. The
# object is therefore renamed into an unpredictable quarantine entry under a
# pinned parent descriptor FIRST; the comparison that authorises destruction
# and the unlink that performs it both address that entry, and nothing that
# happens to the pathname afterwards reaches it.
# ---------------------------------------------------------------------------


def _purge_record(module, manifest, target: str):
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return next(
        record
        for record in module._generation_records(payload)
        if record.target == target
    )


def test_a_swap_after_the_final_compare_cannot_redirect_the_destruction(
    monkeypatch, tmp_path
):
    """The exact interposition the pathname version could not survive.

    The comparison passes, and *then* -- before the destruction -- a decoy
    lands at the pathname. Under compare-then-unlink-the-path, root deletes
    the decoy: a file this generation never read, never backed up and cannot
    put back. Here the comparison already ran against the quarantined inode,
    so the unlink takes that inode and the decoy is untouched.
    """
    module = _module()
    transaction, manifest, worker_only, installed = _control_shaped_generation(
        module, tmp_path
    )
    record = _purge_record(module, manifest, "/etc/aicc/agent.env")
    real_assert = module.FileTransaction._assert_removal_at
    interposed = {"done": False}

    def swap_after_the_authorising_compare(this, held, parent_fd, name, target):
        real_assert(this, held, parent_fd, name, target)
        if name == target.name:
            # The cheap pathname pre-check. The compare that authorises the
            # unlink is the one below, against the quarantine entry.
            return
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"someone-elses-file")
        decoy.chmod(0o600)
        os.replace(decoy, target)
        interposed["done"] = True

    monkeypatch.setattr(
        module.FileTransaction, "_assert_removal_at", swap_after_the_authorising_compare
    )

    transaction._apply_removal(record)

    assert interposed["done"], "the interposition never fired"
    assert worker_only.read_bytes() == b"someone-elses-file"
    assert stat.S_IMODE(worker_only.stat().st_mode) == 0o600
    assert not list(worker_only.parent.glob(".agent.env.aicc-purge-*")), (
        "the quarantined snapshot was not destroyed"
    )
    assert installed.exists() is False


def test_a_swap_before_the_quarantine_compare_is_put_back_without_data_loss(
    monkeypatch, tmp_path
):
    """The other side of the same window: the swap lands before the rename,
    so the decoy -- not the snapshotted file -- is what gets quarantined. The
    comparison refuses it, and the decoy goes back under its own name intact.
    A refused removal must never cost anyone a file."""
    module = _module()
    transaction, manifest, worker_only, _installed = _control_shaped_generation(
        module, tmp_path
    )
    record = _purge_record(module, manifest, "/etc/aicc/agent.env")
    real_assert = module.FileTransaction._assert_removal_at

    def swap_between_the_pre_check_and_the_rename(this, held, parent_fd, name, target):
        real_assert(this, held, parent_fd, name, target)
        if name != target.name:
            return
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"someone-elses-file")
        decoy.chmod(0o600)
        os.replace(decoy, target)

    monkeypatch.setattr(
        module.FileTransaction,
        "_assert_removal_at",
        swap_between_the_pre_check_and_the_rename,
    )

    with pytest.raises(RuntimeError, match="changed before compare-and-remove"):
        transaction._apply_removal(record)

    assert worker_only.read_bytes() == b"someone-elses-file"
    assert stat.S_IMODE(worker_only.stat().st_mode) == 0o600
    assert not list(worker_only.parent.glob(".agent.env.aicc-purge-*")), (
        "a refused removal left the object stranded under its quarantine name"
    )


def test_the_whole_generation_fails_closed_on_a_late_purge_swap(monkeypatch, tmp_path):
    """Through the real apply(), not one record: the refusal above must fail
    the generation, keep the durable WAL, and leave the interposed file
    exactly where its author put it."""
    module = _module()
    transaction, _manifest, worker_only, _installed = _control_shaped_generation(
        module, tmp_path
    )
    real_assert = module.FileTransaction._assert_removal_at
    pathname_compares = {"count": 0}

    def swap_on_the_second_pathname_compare(this, held, parent_fd, name, target):
        real_assert(this, held, parent_fd, name, target)
        if name != target.name:
            return
        pathname_compares["count"] += 1
        # 1 = apply()'s pre-mutation sweep, 2 = _apply_removal's pre-check.
        if pathname_compares["count"] != 2:
            return
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"someone-elses-file")
        decoy.chmod(0o600)
        os.replace(decoy, target)

    monkeypatch.setattr(
        module.FileTransaction,
        "_assert_removal_at",
        swap_on_the_second_pathname_compare,
    )

    with pytest.raises(RuntimeError):
        transaction.apply()

    assert pathname_compares["count"] == 2
    assert worker_only.read_bytes() == b"someone-elses-file"
    assert transaction.pending.exists(), "a refused generation keeps its durable WAL"


def test_a_quarantine_that_cannot_be_put_back_reports_where_it_is(
    monkeypatch, tmp_path
):
    """`link` and not `rename`, so a name someone else has claimed since is
    not silently overwritten. Both objects survive and the error names the
    quarantine entry the operator has to deal with."""
    module = _module()
    transaction, manifest, worker_only, _installed = _control_shaped_generation(
        module, tmp_path
    )
    record = _purge_record(module, manifest, "/etc/aicc/agent.env")
    real_assert = module.FileTransaction._assert_removal_at

    def refuse_the_quarantine_and_reclaim_the_name(this, held, parent_fd, name, target):
        if name == target.name:
            return real_assert(this, held, parent_fd, name, target)
        target.write_bytes(b"claimed-in-the-window")
        raise RuntimeError("purge target changed before compare-and-remove")

    monkeypatch.setattr(
        module.FileTransaction,
        "_assert_removal_at",
        refuse_the_quarantine_and_reclaim_the_name,
    )

    with pytest.raises(RuntimeError, match="could not be put back and is retained at"):
        transaction._apply_removal(record)

    assert worker_only.read_bytes() == b"claimed-in-the-window"
    quarantined = list(worker_only.parent.glob(".agent.env.aicc-purge-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"agent-secret", (
        "the snapshotted file must still exist under some name"
    )


# ---------------------------------------------------------------------------
# Sensitive-removal retirement.
#
# A removal is reversible only because prepare() copied the target into the
# generation's backups first. For the two model credentials that copy is the
# same secret in a second place, on the host whose entire premise is that it
# does not hold them -- so a committed control generation destroys it, and
# every older copy of the same target in the state directory, in an explicit
# finalisation phase driven by its own durable journal.
# ---------------------------------------------------------------------------

CLAUDE_CREDENTIAL = "/var/lib/aicc-agent/claude/.claude/.credentials.json"
CODEX_CREDENTIAL = "/var/lib/aicc-agent/codex/.codex/auth.json"
CLAUDE_BYTES = b"CLAUDE-OAUTH-REFRESH-TOKEN-0001"
CODEX_BYTES = b"CODEX-OAUTH-REFRESH-TOKEN-0002"


def _byte_search(root: Path, needle: bytes) -> list[Path]:
    """Every regular file under `root` whose bytes contain `needle`."""
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if needle in path.read_bytes():
            hits.append(path)
    return hits


def _worker_host_with_credentials(module, tmp_path):
    """A host that ran the worker profile: both credentials installed by a
    committed generation, so the state directory holds a staged copy and the
    targets hold the live ones."""
    root = tmp_path / "root"
    state = tmp_path / "state"
    claude_source = tmp_path / "claude.json"
    claude_source.write_bytes(CLAUDE_BYTES)
    codex_source = tmp_path / "codex.json"
    codex_source.write_bytes(CODEX_BYTES)
    transaction = module.FileTransaction(root, state)
    transaction.install(
        (
            _spec(module, claude_source, CLAUDE_CREDENTIAL, 0o600),
            _spec(module, codex_source, CODEX_CREDENTIAL, 0o600),
        )
    )
    assert _byte_search(state, CLAUDE_BYTES), "the worker generation staged the secret"
    return transaction, root, state


def _control_transition_specs(module, tmp_path):
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    return (
        _spec(module, control_source, "/etc/control-file"),
        module.removal_spec(CLAUDE_CREDENTIAL, sensitive=True),
        module.removal_spec(CODEX_CREDENTIAL, sensitive=True),
    )


def test_a_committed_control_generation_retains_no_credential_byte(tmp_path):
    """The whole claim, asserted by searching every byte the host still has:
    not in the targets, not in this generation's backups, and not in the
    previous worker generation that is still reachable through the chain."""
    module = _module()
    transaction, root, state = _worker_host_with_credentials(module, tmp_path)

    transaction.install(_control_transition_specs(module, tmp_path))

    assert not (root / CLAUDE_CREDENTIAL.lstrip("/")).exists()
    assert not (root / CODEX_CREDENTIAL.lstrip("/")).exists()
    for secret in (CLAUDE_BYTES, CODEX_BYTES):
        assert _byte_search(state, secret) == [], "a credential survived in state"
        assert _byte_search(root, secret) == [], "a credential survived on the host"
    assert not (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()
    assert (root / "etc/control-file").read_bytes() == b"control-only"
    # More than one generation is still on disk: the retirement redacts, it
    # does not blow the chain away.
    assert len(list(state.glob("generation-*"))) == 2


def test_a_failure_before_commit_still_restores_both_credentials(
    monkeypatch, tmp_path
):
    """The retirement is armed inside commit(), so everything before commit
    keeps the ordinary rollback guarantee: a control transition that purges
    the credentials and then fails puts them back byte-for-byte."""
    module = _module()
    transaction, root, state = _worker_host_with_credentials(module, tmp_path)
    specs = _control_transition_specs(module, tmp_path)
    transaction.prepare(
        (specs[1], specs[2], specs[0])  # purge first, then the install that fails
    )
    real_atomic = module._atomic_bytes

    def fail_the_control_install(path, *args, **kwargs):
        if path == root / "etc/control-file":
            raise OSError("injected post-purge failure")
        return real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_bytes", fail_the_control_install)

    with pytest.raises(OSError, match="injected post-purge failure"):
        transaction.apply()

    for target, payload in (
        (CLAUDE_CREDENTIAL, CLAUDE_BYTES),
        (CODEX_CREDENTIAL, CODEX_BYTES),
    ):
        restored = root / target.lstrip("/")
        assert restored.read_bytes() == payload
        assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert not (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()


def test_a_crash_between_commit_and_retirement_is_finished_by_recover(
    monkeypatch, tmp_path
):
    """The journal is armed while the generation is still governed by a
    COMMITTING WAL, so a crash anywhere after it leaves an outstanding
    destruction that recover() completes -- not a committed control host with
    the credentials still in its backups."""
    module = _module()
    transaction, root, state = _worker_host_with_credentials(module, tmp_path)
    crashed = {"active": True}
    real_retirement = module.FileTransaction._run_sensitive_retirement

    def crash_before_retiring(this):
        if crashed["active"]:
            return
        return real_retirement(this)

    monkeypatch.setattr(
        module.FileTransaction, "_run_sensitive_retirement", crash_before_retiring
    )

    transaction.install(_control_transition_specs(module, tmp_path))

    assert (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()
    assert _byte_search(state, CLAUDE_BYTES), "the crash is not being simulated"

    crashed["active"] = False
    module.FileTransaction(root, state).recover()

    assert not (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()
    for secret in (CLAUDE_BYTES, CODEX_BYTES):
        assert _byte_search(state, secret) == []


def test_a_retired_generation_refuses_rollback_by_name(tmp_path):
    """Not silently impossible: the record survives saying a secret was here
    and was destroyed on purpose, and restore() refuses it by target rather
    than skipping it or failing with a shape error nobody can act on."""
    module = _module()
    transaction, root, state = _worker_host_with_credentials(module, tmp_path)
    transaction.install(_control_transition_specs(module, tmp_path))
    manifest = Path(
        json.loads(transaction.current.read_text(encoding="utf-8"))["manifest"]
    )
    records = module._generation_records(
        json.loads(manifest.read_text(encoding="utf-8"))
    )
    retired = [record for record in records if record.sensitive_retired]

    assert {record.target for record in retired} == {
        CLAUDE_CREDENTIAL,
        CODEX_CREDENTIAL,
    }
    assert all(record.backup is None and record.staged == "" for record in retired)
    with pytest.raises(RuntimeError, match="retired at commit"):
        transaction.restore(manifest)


def test_uninstalling_a_retired_control_host_completes_without_resurrection(tmp_path):
    """The one caller that accepts it. Unwinding an installation means leaving
    the host without AICC state, and a credential the control profile removed
    on purpose is exactly that -- so uninstall finishes, and does not put the
    secret back."""
    module = _module()
    transaction, root, state = _worker_host_with_credentials(module, tmp_path)
    transaction.install(_control_transition_specs(module, tmp_path))

    transaction.uninstall_all()

    assert not (root / CLAUDE_CREDENTIAL.lstrip("/")).exists()
    assert not (root / CODEX_CREDENTIAL.lstrip("/")).exists()
    assert not (root / "etc/control-file").exists()
    assert not transaction.current.exists()
    for secret in (CLAUDE_BYTES, CODEX_BYTES):
        assert _byte_search(root, secret) == []
        assert _byte_search(state, secret) == []


def test_retiring_a_target_this_generation_never_held_arms_nothing(tmp_path):
    """A control host that never ran the worker profile has no credential to
    purge, so there is no finalisation phase and no journal to consume."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    control_source = tmp_path / "control-file"
    control_source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            _spec(module, control_source, "/etc/control-file"),
            module.removal_spec(CLAUDE_CREDENTIAL, sensitive=True),
        )
    )

    assert not (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()
    manifest = Path(
        json.loads(transaction.current.read_text(encoding="utf-8"))["manifest"]
    )
    records = module._generation_records(
        json.loads(manifest.read_text(encoding="utf-8"))
    )
    assert not any(record.sensitive_retired for record in records)
    # And it is still rollbackable, unlike a generation that really did
    # destroy a backup.
    transaction.restore(manifest)


# ---------------------------------------------------------------------------
# Publisher-group membership.
#
# /etc/aicc/workspace-authority.env is 0640 root:aicc-publisher on every
# profile, and deploy/sysusers.d/aicc-agent.conf puts `aicc-worker` and
# `voynadmin` in that group. sysusers never removes a membership, so a host
# converted from worker to control kept two worker-era principals able to
# read the control plane's authority key. The group IS the boundary, so the
# membership is what has to go -- dropping the file's group bits instead
# would move the boundary rather than enforce it.
# ---------------------------------------------------------------------------


def _publisher_group(module, tmp_path, members):
    """A state directory plus an injectable publisher group and gpasswd."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    live = set(members)
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        assert command[0] == module.GPASSWD
        assert command[3] == module.AUTHORITY_GROUP
        if command[1] == "-d":
            live.discard(command[2])
        else:
            live.add(command[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def getgrnam(name):
        assert name == module.AUTHORITY_GROUP
        return SimpleNamespace(gr_gid=4242, gr_mem=sorted(live))

    return state, live, calls, run, getgrnam


def test_the_control_transition_revokes_the_legacy_publisher_membership(tmp_path):
    module = _module()
    state, live, calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin", "aicc-control-plane"}
    )

    revoked = module.revoke_legacy_authority_membership(
        state, run=run, getgrnam=getgrnam
    )

    assert set(revoked) == {"aicc-worker", "voynadmin"}
    assert live == {"aicc-control-plane"}, "a member nobody named was disturbed"
    assert [call[1] for call in calls] == ["-d", "-d"]
    journal = json.loads(
        (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).read_text(encoding="utf-8")
    )
    assert journal["members_before"] == [
        "aicc-control-plane",
        "aicc-worker",
        "voynadmin",
    ]


def test_the_revocation_is_undone_exactly_by_the_rollback(tmp_path):
    module = _module()
    state, live, _calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin", "aicc-control-plane"}
    )
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    module.restore_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    assert live == {"aicc-worker", "voynadmin", "aicc-control-plane"}
    assert not (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()


def test_a_revocation_that_cannot_be_proven_fails_closed_with_its_journal(tmp_path):
    """Root-mediated and fail-closed in both directions: an unproven removal
    raises rather than letting the install continue believing the boundary
    holds, and the durable journal stays so the rollback can still undo the
    members that did come out."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    live = {"aicc-worker", "voynadmin"}

    def run(command, **kwargs):
        if command[2] == "voynadmin":
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        live.discard(command[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def getgrnam(name):
        return SimpleNamespace(gr_gid=4242, gr_mem=sorted(live))

    with pytest.raises(RuntimeError, match="cannot revoke legacy authority membership"):
        module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    assert live == {"voynadmin"}
    assert (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()


def test_a_restore_that_cannot_be_proven_keeps_the_journal_for_the_next_attempt(
    tmp_path,
):
    module = _module()
    state, live, _calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin"}
    )
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    def refuse(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="denied")

    with pytest.raises(
        RuntimeError, match="cannot restore legacy authority membership"
    ):
        module.restore_legacy_authority_membership(
            state, run=refuse, getgrnam=getgrnam
        )

    assert (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()


def test_a_revocation_is_idempotent_across_a_retried_install(tmp_path):
    module = _module()
    state, live, calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin"}
    )
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)

    assert live == set()
    journal = json.loads(
        (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).read_text(encoding="utf-8")
    )
    assert journal["revoked"] == ["aicc-worker", "voynadmin"], (
        "a retry must not overwrite the pre-state with the already-revoked one"
    )


def _restoring_recover(module, monkeypatch, run, getgrnam):
    real_restore = module.restore_legacy_authority_membership
    monkeypatch.setattr(
        module,
        "restore_legacy_authority_membership",
        lambda state_dir: real_restore(state_dir, run=run, getgrnam=getgrnam),
    )


def test_recover_restores_the_membership_a_rolled_back_generation_revoked(
    monkeypatch, tmp_path
):
    """One rollback boundary: the operator runs the same `recover` the
    installer's trap runs, and the files and the authority come back
    together."""
    module = _module()
    root = tmp_path / "root"
    state, live, _calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin"}
    )
    worker_only = root / "etc/aicc/agent.env"
    worker_only.parent.mkdir(parents=True)
    worker_only.write_bytes(b"agent-secret")
    worker_only.chmod(0o640)
    transaction = module.FileTransaction(root, state)
    transaction.prepare((module.removal_spec("/etc/aicc/agent.env"),))
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)
    assert live == set()
    _restoring_recover(module, monkeypatch, run, getgrnam)

    transaction.recover()

    assert worker_only.read_bytes() == b"agent-secret"
    assert live == {"aicc-worker", "voynadmin"}
    assert not (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()


def test_commit_makes_the_revocation_terminal(monkeypatch, tmp_path):
    """A committed control host must not have its worker-era memberships put
    back by the next recover(): the generation is live, so the revocation it
    made is part of what is live."""
    module = _module()
    root = tmp_path / "root"
    state, live, _calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin"}
    )
    source = tmp_path / "control-file"
    source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/control-file"),))
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)
    transaction.apply()

    transaction.commit()

    assert not (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()
    _restoring_recover(module, monkeypatch, run, getgrnam)
    module.FileTransaction(root, state).recover()
    assert live == set(), "a committed revocation was undone by a later recover"


def test_an_interrupted_commit_finalises_the_membership_it_did_not_reach(
    monkeypatch, tmp_path
):
    """recover() resolves the auxiliary journal in the same direction as the
    generation it belongs to: a COMMITTING journal is finished, so the
    membership is finalised rather than restored."""
    module = _module()
    root = tmp_path / "root"
    state, live, _calls, run, getgrnam = _publisher_group(
        module, tmp_path, {"aicc-worker", "voynadmin"}
    )
    source = tmp_path / "control-file"
    source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare((_spec(module, source, "/etc/control-file"),))
    module.revoke_legacy_authority_membership(state, run=run, getgrnam=getgrnam)
    transaction.apply()
    transaction._write_journal(manifest, "COMMITTING", 1)
    _restoring_recover(module, monkeypatch, run, getgrnam)

    module.FileTransaction(root, state).recover()

    assert (root / "etc/control-file").read_bytes() == b"control-only"
    assert live == set()
    assert not (state / module.AUTHORITY_MEMBERSHIP_JOURNAL).exists()


def test_revoke_worker_authority_is_refused_outside_the_control_profile(tmp_path):
    """Under any other profile it would strip the publisher group of the very
    members that profile installs against."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    args = SimpleNamespace(
        action="revoke-worker-authority",
        state_dir=state,
        repo_root=tmp_path,
        root=tmp_path / "root",
        profile="worker",
    )

    with pytest.raises(RuntimeError, match="requires the control profile"):
        module._dispatch(args, argparse.ArgumentParser())


def test_the_authority_journal_refuses_a_member_outside_the_legacy_set(tmp_path):
    """A journal claiming to have revoked something else would make the
    rollback add a membership no install ever took away."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    journal = state / module.AUTHORITY_MEMBERSHIP_JOURNAL
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "group": "aicc-publisher",
                "members_before": ["root"],
                "revoked": ["root"],
            }
        ),
        encoding="utf-8",
    )
    journal.chmod(0o600)

    with pytest.raises(RuntimeError, match="authority membership journal is invalid"):
        module.restore_legacy_authority_membership(state)


# ---------------------------------------------------------------------------
# Template instances: worker lanes AND broker sessions.
#
# Neither `voyn-aicc-worker@<lane>` nor `aicc-agent-launcher@<connection>` has
# a unit file of its own. Both run off a template the control generation
# removes, so both have to be discovered from systemd, snapshotted, stopped
# and recognised as restorable by the template the journal actually names.
# ---------------------------------------------------------------------------


def _listing_runner(*, workers=(), launchers=(), loaded=()):
    def run(command, **kwargs):
        if command[1] == "list-unit-files":
            return SimpleNamespace(returncode=1, stderr="", stdout="")
        if command[1] == "list-units":
            names = (
                launchers
                if command[2] == "aicc-agent-launcher@*.service"
                else workers
            )
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="".join(f"{name} loaded active running\n" for name in names),
            )
        if command[1] == "show":
            state = "loaded" if command[2] in loaded else "not-found"
            return SimpleNamespace(returncode=0, stderr="", stdout=f"{state}\n")
        if command[1] == "disable":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected systemctl call: {command}")

    return run


def test_the_control_purge_stops_every_live_broker_instance(tmp_path):
    """Naming only `aicc-agent-launcher.socket` left every accepted session
    running as its own unit, off a fragment apply() was about to delete."""
    module = _module()
    calls = []
    listing = _listing_runner(
        workers=("voyn-aicc-worker@1.service",),
        launchers=("● aicc-agent-launcher@7.service", "aicc-agent-launcher@9.service"),
        loaded={
            "aicc-agent-launcher.socket",
            "aicc-agent-launcher@7.service",
            "aicc-agent-launcher@9.service",
            "voyn-aicc-worker@1.service",
        },
    )

    def run(command, **kwargs):
        calls.append(tuple(command))
        return listing(command, **kwargs)

    module.quiesce_worker_only_units(run=run)

    disabled = [call[3] for call in calls if call[1] == "disable"]
    assert set(disabled) == {
        "aicc-agent-launcher.socket",
        "aicc-agent-launcher@7.service",
        "aicc-agent-launcher@9.service",
        "voyn-aicc-worker@1.service",
    }
    assert disabled[0] == "aicc-agent-launcher.socket", (
        "stopping an instance while its socket still listens only frees the name"
    )


def test_a_broker_instance_outside_the_snapshot_fails_closure(tmp_path):
    """The snapshot is what a rollback restores. A running unit absent from
    it is a unit no rollback can put back."""
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-agent-launcher.socket": {
                        "exists": True,
                        "enabled": True,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="outside service snapshot"):
        module.verify_service_snapshot_closure(
            snapshot,
            run=_listing_runner(launchers=("aicc-agent-launcher@7.service",)),
        )


def test_a_snapshot_that_covers_the_broker_instances_passes_closure(tmp_path):
    module = _module()
    snapshot = tmp_path / "attempt-units.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 2,
                "units": {
                    "aicc-agent-launcher@7.service": {
                        "exists": True,
                        "enabled": False,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    module.verify_service_snapshot_closure(
        snapshot, run=_listing_runner(launchers=("aicc-agent-launcher@7.service",))
    )


def test_a_concrete_instance_is_restorable_by_the_template_the_journal_names(
    tmp_path,
):
    """`voyn-aicc-worker@1.service` has no unit file, so no manifest can name
    it: the journal names `voyn-aicc-worker@.service`. Matching restorable
    units by exact name therefore never recognised a single running lane, and
    a rollback interrupted between removing the template and restoring it
    deadlocked on the first instance."""
    module = _module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 3,
                "records": [
                    {"target": "/etc/systemd/system/voyn-aicc-worker@.service"},
                    {"target": "/etc/systemd/system/aicc-agent-launcher@.service"},
                ],
            }
        ),
        encoding="utf-8",
    )

    restorable = module._units_restored_by(manifest)

    assert module._is_restorable_unit("voyn-aicc-worker@1.service", restorable)
    assert module._is_restorable_unit("aicc-agent-launcher@7.service", restorable)
    assert not module._is_restorable_unit("voyn-aicc-worker.service", restorable)
    assert not module._is_restorable_unit("aicc-agent-launcher.socket", restorable)


@pytest.mark.parametrize(
    "unit, template",
    [
        ("voyn-aicc-worker@1.service", "voyn-aicc-worker@.service"),
        ("aicc-agent-launcher@7.service", "aicc-agent-launcher@.service"),
    ],
)
def test_quiesce_proceeds_for_an_instance_of_a_template_this_rollback_restores(
    tmp_path, unit, template
):
    module = _module()
    snapshot = _quiesce_snapshot(tmp_path, unit)

    module.quiesce_service_snapshot(
        snapshot,
        run=_not_found_runner(),
        restorable_units=frozenset({template}),
    )


def test_quiesce_still_refuses_an_instance_of_a_template_nobody_restores(tmp_path):
    module = _module()
    snapshot = _quiesce_snapshot(tmp_path, "voyn-aicc-worker@1.service")

    with pytest.raises(RuntimeError, match="expected unit disappeared before quiesce"):
        module.quiesce_service_snapshot(
            snapshot,
            run=_not_found_runner(),
            restorable_units=frozenset({"aicc-agent-launcher@.service"}),
        )


def test_every_restorable_unit_shape_is_accepted_by_both_modules():
    """The rollout writes the snapshot and the transaction restores it; a
    name one accepts and the other rejects is a snapshot that can be taken
    and never applied."""
    module = _module()
    sys.path.insert(0, str(Path(__file__).parents[2] / "ops"))
    import importlib

    rollout = importlib.import_module("aicc_staged_worker_rollout")

    assert module.RESTORABLE_UNIT_RE.pattern == rollout.RESTORABLE_UNIT_RE.pattern
    for unit in (
        "voyn-aicc-worker@1.service",
        "aicc-agent-launcher@7.service",
        "aicc-agent-launcher.socket",
        "aicc-principal-recovery.service",
    ):
        assert module.RESTORABLE_UNIT_RE.fullmatch(unit)


# ---------------------------------------------------------------------------
# The caller's umask is not part of the installer's security posture.
# ---------------------------------------------------------------------------


def test_generation_directories_are_private_under_a_permissive_umask(tmp_path):
    """`Path.mkdir(parents=True, mode=0o700)` gives the mode to the LAST
    component only -- pathlib creates the intermediate ones with the default
    0o777 masked by the caller's umask. The generation directory is an
    intermediate one, and it holds the backups, so under a 0o002 umask the
    credential copies sat in a group-writable directory and under 0o000 in a
    world-writable one. Every directory this transaction owns is therefore
    created and chmod'ed explicitly."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    previous = os.umask(0o000)
    try:
        transaction = module.FileTransaction(root, state)
        transaction.prepare((_spec(module, source, "/etc/installed"),))
    finally:
        os.umask(previous)

    generation = next(state.glob("generation-*"))
    for directory in (state, generation, generation / "backups", generation / "staged"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


# ---------------------------------------------------------------------------
# Generation-manifest schema compatibility.
#
# New code reads old journals; an older exact-SHA reader refuses a record it
# predates deterministically, before any mutation, and keeps working on the
# generations that do not concern it.
# ---------------------------------------------------------------------------


VERSION_TWO_RECORD_FIELDS = (
    "target",
    "existed",
    "backup",
    "original_mode",
    "original_uid",
    "original_gid",
    "original_sha256",
    "staged",
    "install_sha256",
    "install_mode",
    "install_uid",
    "install_gid",
    "original_symlink",
    "remove",
)
VERSION_ONE_RECORD_FIELDS = VERSION_TWO_RECORD_FIELDS[:-2]


def _older_reader(fields):
    """What an already-deployed reader does with a manifest.

    It builds `BackupRecord(**value)` from each record dict, so a key its
    dataclass predates is a `TypeError` from `__init__` -- reproduced here
    rather than imported, because the whole point is the behaviour of a build
    that is on the host and cannot be changed.
    """

    def read(records):
        loaded = []
        for value in records:
            unexpected = sorted(set(value) - set(fields))
            if unexpected:
                raise TypeError(
                    "__init__() got an unexpected keyword argument "
                    f"{unexpected[0]!r}"
                )
            loaded.append({name: value[name] for name in fields if name in value})
        return loaded

    return read


def _one_generation(module, tmp_path, specs):
    transaction = module.FileTransaction(tmp_path / "root", tmp_path / "state")
    manifest = transaction.prepare(specs)
    return transaction, manifest


def test_new_code_restores_a_version_one_journal(tmp_path):
    """A generation written before `original_symlink` and `remove` existed
    still loads: every field added since carries the default that reproduces
    the older semantics exactly."""
    module = _module()
    root = tmp_path / "root"
    existing = root / "etc/existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"before")
    existing.chmod(0o600)
    source = tmp_path / "source"
    source.write_bytes(b"after")
    transaction, manifest = _one_generation(
        module, tmp_path, (_spec(module, source, "/etc/existing", 0o600),)
    )
    transaction.apply()
    transaction.commit()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = 1
    payload["records"] = [
        {name: record[name] for name in VERSION_ONE_RECORD_FIELDS}
        for record in payload["records"]
    ]
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    manifest.chmod(0o600)

    transaction.restore(manifest)

    assert existing.read_bytes() == b"before"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600


def test_an_unsupported_manifest_version_is_refused_before_any_mutation(tmp_path):
    """The direction this build CAN fail safely in: a manifest from a format
    it does not know is refused outright rather than read with the wrong
    field meanings."""
    module = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction, manifest = _one_generation(
        module, tmp_path, (_spec(module, source, "/etc/installed"),)
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = module.MANIFEST_VERSION + 1
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(RuntimeError, match="unsupported generation manifest version"):
        transaction.apply()

    assert not (root / "etc/installed").exists()


def test_a_record_field_this_build_does_not_know_is_refused(tmp_path):
    module = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    transaction, manifest = _one_generation(
        module, tmp_path, (_spec(module, source, "/etc/installed"),)
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["records"][0]["invented_later"] = True
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(RuntimeError, match="unsupported fields"):
        transaction.apply()

    assert not (root / "etc/installed").exists()


def test_a_generation_with_nothing_version_three_still_loads_in_an_older_reader(
    tmp_path,
):
    """The version-3 fields are emitted only on the records that use them, so
    an ordinary install generation is still exactly what an already-deployed
    exact-SHA reader expects."""
    module = _module()
    source = tmp_path / "source"
    source.write_bytes(b"installed")
    _transaction, manifest = _one_generation(
        module, tmp_path, (_spec(module, source, "/etc/installed"),)
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert payload["version"] == module.MANIFEST_VERSION
    _older_reader(VERSION_TWO_RECORD_FIELDS)(payload["records"])


def test_an_older_reader_refuses_a_version_three_record_deterministically(tmp_path):
    """The refusal an already-deployed reader gives, and the only one it can
    give: it builds `BackupRecord(**value)` from each dict while assembling
    the record list, so a field it predates raises TypeError strictly before
    the first mutation. Not a graceful message, but total, deterministic and
    fail-closed -- and it cannot be improved in code that is already on the
    host. What this build controls is that it only happens on generations
    that really do carry the new semantics."""
    module = _module()
    root = tmp_path / "root"
    home = root / "var/lib/aicc-agent/claude/.claude"
    home.mkdir(parents=True)
    credential = home / ".credentials.json"
    credential.write_bytes(CLAUDE_BYTES)
    credential.chmod(0o600)
    _transaction, manifest = _one_generation(
        module,
        tmp_path,
        (
            module.removal_spec(CLAUDE_CREDENTIAL, sensitive=True),
            module.directory_removal_spec("/var/lib/aicc-agent/claude/.claude"),
        ),
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        _older_reader(VERSION_TWO_RECORD_FIELDS)(payload["records"])

    assert credential.read_bytes() == CLAUDE_BYTES, "prepare() mutates no target"


# ---------------------------------------------------------------------------
# Worker-only directories.
#
# Removing the files and leaving the tree is a residual artefact tree: an
# empty /var/lib/aicc-agent/claude/.claude still names the secret that used to
# be in it, and /run/aicc-agent-homes is where the launcher materialised
# ephemeral copies of both credentials. The directories go in the same
# generation -- but only if everything in them is something this generation is
# itself removing.
# ---------------------------------------------------------------------------


def _agent_credential_tree(tmp_path):
    root = tmp_path / "root"
    home = root / "var/lib/aicc-agent/claude/.claude"
    home.mkdir(parents=True)
    credential = home / ".credentials.json"
    credential.write_bytes(CLAUDE_BYTES)
    credential.chmod(0o600)
    for directory in (
        root / "var/lib/aicc-agent",
        root / "var/lib/aicc-agent/claude",
        home,
    ):
        os.chmod(directory, 0o700)
    return root, credential


def _credential_tree_specs(module):
    return (
        module.removal_spec(CLAUDE_CREDENTIAL, sensitive=True),
        module.directory_removal_spec("/var/lib/aicc-agent/claude/.claude"),
        module.directory_removal_spec("/var/lib/aicc-agent/claude"),
        module.directory_removal_spec("/var/lib/aicc-agent"),
    )


def test_worker_only_directories_are_removed_after_the_files_they_held(tmp_path):
    module = _module()
    root, _credential = _agent_credential_tree(tmp_path)
    transaction = module.FileTransaction(root, tmp_path / "state")

    transaction.install(_credential_tree_specs(module))

    assert not (root / "var/lib/aicc-agent").exists()
    assert (root / "var/lib").is_dir(), "only the declared tree is removed"


def test_unexpected_content_under_a_worker_only_directory_fails_closed(tmp_path):
    """A directory removal is reversible only as "recreate an empty directory
    with this mode and owner", so the only content it may find is content
    this same generation removes. Anything else -- an operator's file, a
    session the quiesce step failed to stop, a provider cache nobody declared
    -- fails the generation before prepare() has touched a target."""
    module = _module()
    root, credential = _agent_credential_tree(tmp_path)
    intruder = credential.parent / "settings.json"
    intruder.write_bytes(b"nobody-declared-this")
    transaction = module.FileTransaction(root, tmp_path / "state")

    with pytest.raises(RuntimeError, match="unexpected content under worker-only"):
        transaction.prepare(_credential_tree_specs(module))

    assert credential.read_bytes() == CLAUDE_BYTES
    assert intruder.read_bytes() == b"nobody-declared-this"
    assert not transaction.pending.exists()
    assert not list((tmp_path / "state").glob("generation-*"))


def test_a_worker_only_directory_that_refilled_after_prepare_is_not_removed(tmp_path):
    """rmdir is itself the compare: it removes a directory only while that
    directory is empty. Something that appeared since prepare() proved the
    tree accounted-for stops the removal atomically, in the kernel."""
    module = _module()
    root, credential = _agent_credential_tree(tmp_path)
    transaction = module.FileTransaction(root, tmp_path / "state")
    transaction.prepare(_credential_tree_specs(module))
    intruder = credential.parent / "arrived-late.json"
    intruder.write_bytes(b"arrived-after-prepare")

    with pytest.raises(RuntimeError, match="not empty at removal"):
        transaction.apply()

    assert intruder.read_bytes() == b"arrived-after-prepare"
    assert credential.read_bytes() == CLAUDE_BYTES, "the purge was rolled back"
    assert not transaction.pending.exists()


def test_a_rolled_back_generation_puts_every_directory_back_exactly(
    monkeypatch, tmp_path
):
    """Reversed record order, so the directories come back before the files
    that belonged in them -- with the mode and owner the snapshot recorded,
    not whatever the caller's umask would have produced."""
    module = _module()
    root, credential = _agent_credential_tree(tmp_path)
    source = tmp_path / "control-file"
    source.write_bytes(b"control-only")
    transaction = module.FileTransaction(root, tmp_path / "state")
    transaction.prepare(
        (*_credential_tree_specs(module), _spec(module, source, "/etc/control-file"))
    )
    real_atomic = module._atomic_bytes

    def fail_the_control_install(path, *args, **kwargs):
        if path == root / "etc/control-file":
            raise OSError("injected post-purge failure")
        return real_atomic(path, *args, **kwargs)

    monkeypatch.setattr(module, "_atomic_bytes", fail_the_control_install)
    previous = os.umask(0o077)
    try:
        with pytest.raises(OSError, match="injected post-purge failure"):
            transaction.apply()
    finally:
        os.umask(previous)

    assert credential.read_bytes() == CLAUDE_BYTES
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    for directory in (
        root / "var/lib/aicc-agent",
        root / "var/lib/aicc-agent/claude",
        root / "var/lib/aicc-agent/claude/.claude",
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_a_directory_purge_is_idempotent_on_an_already_converted_host(tmp_path):
    module = _module()
    root, _credential = _agent_credential_tree(tmp_path)
    transaction = module.FileTransaction(root, tmp_path / "state")
    specs = _credential_tree_specs(module)
    transaction.install(specs)

    transaction.install(specs)

    assert not (root / "var/lib/aicc-agent").exists()


def test_a_worker_only_directory_target_that_is_a_symlink_is_refused(tmp_path):
    """rmdir on a symlink would remove the link and leave whatever it pointed
    at, and recreating a directory in its place would be a different object
    entirely."""
    module = _module()
    root = tmp_path / "root"
    (root / "var/lib").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "var/lib/aicc-agent").symlink_to(elsewhere)
    transaction = module.FileTransaction(root, tmp_path / "state")

    with pytest.raises(RuntimeError, match="is not a directory"):
        transaction.prepare((module.directory_removal_spec("/var/lib/aicc-agent"),))

    assert (root / "var/lib/aicc-agent").is_symlink()


# ---------------------------------------------------------------------------
# The whole control profile, end to end, on the real spec list.
# ---------------------------------------------------------------------------


def test_a_real_control_install_leaves_no_worker_artefact_or_secret_tree(tmp_path):
    """A host that ran the worker profile, converted by the real
    `default_specs(profile="control")`: every worker-only file gone, every
    worker-only directory gone, and no credential byte anywhere on the host
    or in the transaction state that recorded the conversion."""
    import dataclasses

    module = _module()
    repo = Path(__file__).parents[2]
    root = tmp_path / "root"
    state = tmp_path / "state"
    authority = tmp_path / "authority.env"
    authority.write_text(
        "AICC_WORKSPACE_ROOTS=/srv/aicc-workspaces\n", encoding="utf-8"
    )
    for target, payload in (
        (CLAUDE_CREDENTIAL, CLAUDE_BYTES),
        (CODEX_CREDENTIAL, CODEX_BYTES),
        ("/etc/aicc/agent.env", b"AICC_AGENT_ENV=1"),
        ("/usr/libexec/aicc-agent-launcher", b"#!/usr/bin/python3\n"),
        ("/etc/systemd/system/aicc-agent-launcher.socket", b"[Socket]\n"),
        (
            "/etc/systemd/system/voyn-aicc-worker@.service.d/"
            "20-principal-isolation.conf",
            b"[Service]\n",
        ),
    ):
        path = root / target.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o600)
    for volatile in ("/run/aicc-agent-homes", "/run/aicc-agent-launcher/active"):
        (root / volatile.lstrip("/")).mkdir(parents=True)
    # Unprivileged: the real specs install as root:root, which this process
    # cannot chown to. Only the identities are rewritten -- the target set,
    # the removals and the directory purges are exactly what a host gets.
    specs = tuple(
        dataclasses.replace(spec, uid=os.geteuid(), gid=os.getegid())
        for spec in module.default_specs(
            repo,
            authority_env=authority,
            claude_auth=tmp_path / "absent-claude.json",
            codex_auth=tmp_path / "absent-codex.json",
            resolve_identities=False,
            profile="control",
        )
    )

    module.FileTransaction(root, state).install(specs)

    for gone in (
        *module.WORKER_ONLY_TARGETS,
        *module.WORKER_ONLY_DIRECTORIES,
    ):
        assert not (root / gone.lstrip("/")).exists(), gone
    assert (root / "etc/aicc/workspace-authority.env").exists()
    assert (root / "usr/libexec/aicc-install-transaction").exists()
    for secret in (CLAUDE_BYTES, CODEX_BYTES):
        assert _byte_search(root, secret) == []
        assert _byte_search(state, secret) == []
    assert not (state / module.SENSITIVE_RETIREMENT_JOURNAL).exists()


def test_a_quarantined_symlink_is_put_back_as_a_symlink(monkeypatch, tmp_path):
    """The legacy shape every unit on these hosts still has. Quarantining a
    link and putting it back must restore the LINK, not a copy of what it
    resolved to -- `linkat` without AT_SYMLINK_FOLLOW, so the entry that
    reappears is the same symlink inode with the same literal target."""
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    elsewhere = tmp_path / "operator-home/unit.service"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"legacy")
    link = root / "etc/systemd/system/legacy.service"
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    transaction = module.FileTransaction(root, state)
    manifest = transaction.prepare(
        (module.removal_spec("/etc/systemd/system/legacy.service"),)
    )
    record = _purge_record(module, manifest, "/etc/systemd/system/legacy.service")
    real_assert = module.FileTransaction._assert_removal_at

    def retarget_between_the_pre_check_and_the_rename(
        this, held, parent_fd, name, target
    ):
        real_assert(this, held, parent_fd, name, target)
        if name != target.name:
            return
        decoy = tmp_path / "decoy-link"
        decoy.symlink_to(tmp_path / "somewhere-else")
        os.replace(decoy, target)

    monkeypatch.setattr(
        module.FileTransaction,
        "_assert_removal_at",
        retarget_between_the_pre_check_and_the_rename,
    )

    with pytest.raises(RuntimeError, match="symlink changed before removal"):
        transaction._apply_removal(record)

    assert link.is_symlink()
    assert os.readlink(link) == str(tmp_path / "somewhere-else")
    assert not list(link.parent.glob(".legacy.service.aicc-purge-*"))
    assert elsewhere.read_bytes() == b"legacy", "nothing followed the link"
