from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_install_transaction.py"
    spec = importlib.util.spec_from_file_location("aicc_install_transaction", path)
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
    real_atomic = module._atomic_bytes
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staged-install failure")
        return real_atomic(*args, **kwargs)

    monkeypatch.setattr(module, "_atomic_bytes", fail_once)
    transaction = module.FileTransaction(root, state)
    with pytest.raises(OSError, match="injected"):
        transaction.install(
            (
                _spec(module, source_one, "/etc/existing"),
                _spec(module, source_two, "/etc/new"),
            )
        )

    assert existing.read_bytes() == b"before"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert not (root / "etc/new").exists()


def test_uninstall_restores_replaced_file_and_removes_new_file(tmp_path):
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

    transaction.restore()

    assert existing.read_bytes() == b"before"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert not (root / "etc/new").exists()


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
