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


def test_uninstall_removes_a_target_a_narrower_later_generation_stopped_managing(
    tmp_path,
):
    """A `control`-profile install runs as a narrower generation on top of an
    existing `worker` generation: it must not resurrect or touch a target it
    excludes, but a full uninstall must still reach back through the
    generation chain and remove it -- that reach-back is what a worker→control
    purge relies on to make the transition transactional rather than a side
    effect of the narrower install itself (independent review on 090afcf).
    """
    module = _module()
    root = tmp_path / "root"
    state = tmp_path / "state"
    credential_source = tmp_path / "credential"
    shared_source = tmp_path / "shared"
    credential_source.write_bytes(b"agent-credential")
    shared_source.write_bytes(b"shared")
    transaction = module.FileTransaction(root, state)

    transaction.install(
        (
            _spec(module, credential_source, "/var/agent-credential"),
            _spec(module, shared_source, "/etc/shared"),
        )
    )
    assert (root / "var/agent-credential").read_bytes() == b"agent-credential"

    shared_source.write_bytes(b"shared-two")
    transaction.install((_spec(module, shared_source, "/etc/shared"),))

    # The narrower generation neither removed nor re-managed the excluded
    # target -- it is simply untouched, exactly the gap review flagged.
    assert (root / "var/agent-credential").read_bytes() == b"agent-credential"

    transaction.uninstall_all()

    assert not (root / "var/agent-credential").exists()
    assert not (root / "etc/shared").exists()
    assert not transaction.current.exists()
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
