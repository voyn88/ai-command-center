from __future__ import annotations

import importlib.util
import json
import os
import stat
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


def test_first_install_boot_generator_uses_self_contained_recovery(tmp_path):
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

    assert generator.generate(destination, state, expected_uid=os.getuid())

    unit = (destination / "aicc-principal-recovery.service").read_text()
    assert f"ExecStart=/usr/bin/python3 {recovery} recover" in unit
    alias = generation / "alias.py"
    alias.symlink_to(recovery)
    (state / "pending.json").write_text(
        json.dumps({"recovery": str(alias)}), encoding="utf-8"
    )
    assert not generator.generate(destination, state, expected_uid=os.getuid())


def test_recovery_generator_is_the_first_destination_of_clean_install(tmp_path):
    module = _module()
    repo = Path(__file__).parents[2]
    specs = module.default_specs(
        repo,
        authority_env=tmp_path / "authority.env",
        claude_auth=tmp_path / "claude.json",
        codex_auth=tmp_path / "codex.json",
        resolve_identities=False,
    )
    assert (
        specs[0].target == "/usr/lib/systemd/system-generators/aicc-principal-recovery"
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
        module,
        "restore_service_snapshot",
        lambda path: (_ for _ in ()).throw(RuntimeError("systemd unavailable")),
    )

    with pytest.raises(RuntimeError, match="systemd unavailable"):
        transaction.recover()

    assert transaction.pending.exists()
    monkeypatch.setattr(module, "restore_service_snapshot", lambda path: None)
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
    (current.parent / "releases" / ("a" * 40)).mkdir(parents=True)
    source = tmp_path / "source"
    source.write_bytes(b"installed")

    transaction = module.FileTransaction(root, state)
    transaction.prepare((_spec(module, source, "/etc/new"),))
    transaction.apply()
    assert json.loads(transaction.pending.read_text())["phase"] == "APPLIED"

    pending_release = state / "pending-release"
    pending_release.write_text(f"releases/{'a' * 40}\n", encoding="ascii")
    pending_release.chmod(0o600)
    (state / "attempt-units.json").write_text(
        json.dumps({"version": 2, "units": {}}), encoding="utf-8"
    )

    order: list[str] = []
    original_selector_restore = module.FileTransaction._restore_release_selector

    def recording_selector_restore(self):
        order.append("selector")
        return original_selector_restore(self)

    def recording_service_restore(path):
        assert path == state / "attempt-units.json"
        order.append("services")

    monkeypatch.setattr(
        module.FileTransaction,
        "_restore_release_selector",
        recording_selector_restore,
    )
    monkeypatch.setattr(module, "restore_service_snapshot", recording_service_restore)

    transaction.recover()

    assert order == ["selector", "services"], order
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
    with pytest.raises(RuntimeError, match="missing release"):
        transaction.recover()
    assert current.readlink() == Path(f"releases/{'b' * 40}")
