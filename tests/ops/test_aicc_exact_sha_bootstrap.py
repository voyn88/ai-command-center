from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_exact_sha_bootstrap.py"
    spec = importlib.util.spec_from_file_location("aicc_exact_sha_bootstrap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "HOME": str(repo.parent),
            "LANG": "C",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    return result.stdout.strip()


def _trusted_repo(module, tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    for relative in module.REQUIRED_ENTRYPOINTS:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"trusted:{relative}\n", encoding="utf-8")
        target.chmod(0o755 if relative.endswith((".sh", ".py")) else 0o644)
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=AICC Bootstrap Test",
        "-c",
        "user.email=bootstrap@example.invalid",
        "commit",
        "-m",
        "trusted tree",
    )
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return repo, sha, module._safe_environment(tmp_path)


def _verify(module, repo: Path, env: dict[str, str], sha: str):
    return module._verify_checkout(
        repo,
        env,
        sha,
        "attempt-test",
        trusted_uid=os.getuid(),
        trusted_gid=os.getgid(),
    )


def test_exact_clean_tree_is_attested(tmp_path):
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)

    attestation = _verify(module, repo, env, sha)

    assert attestation.expected_sha == sha
    assert attestation.remote_main_sha == sha
    assert attestation.file_count == len(module.REQUIRED_ENTRYPOINTS)
    assert len(attestation.tree_manifest_sha256) == 64


def test_modified_checkout_is_refused(tmp_path):
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    (repo / module.REQUIRED_ENTRYPOINTS[0]).write_text("mutated\n")

    with pytest.raises(module.BootstrapRefused, match="not clean"):
        _verify(module, repo, env, sha)


def test_remote_main_must_equal_expected_sha(tmp_path):
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    (repo / "later").write_text("later\n")
    _git(repo, "add", "later")
    _git(
        repo,
        "-c",
        "user.name=AICC Bootstrap Test",
        "-c",
        "user.email=bootstrap@example.invalid",
        "commit",
        "-m",
        "later main",
    )
    later = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", later, sha)
    _git(repo, "reset", "--hard", sha)

    with pytest.raises(module.BootstrapRefused, match="exact trusted remote main"):
        _verify(module, repo, env, sha)


def test_symlink_payload_is_refused_even_when_committed(tmp_path):
    module = _module()
    repo, _sha, env = _trusted_repo(module, tmp_path)
    link = repo / "hostile-link"
    link.symlink_to("/etc/shadow")
    _git(repo, "add", "hostile-link")
    _git(
        repo,
        "-c",
        "user.name=AICC Bootstrap Test",
        "-c",
        "user.email=bootstrap@example.invalid",
        "commit",
        "-m",
        "hostile symlink",
    )
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)

    with pytest.raises(module.BootstrapRefused, match="untrusted checkout path"):
        _verify(module, repo, env, sha)


def test_safe_environment_drops_git_and_dynamic_loader_injection(monkeypatch, tmp_path):
    module = _module()
    poisoned = {
        "GIT_DIR": "/attacker/git",
        "GIT_OBJECT_DIRECTORY": "/attacker/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/attacker/alternates",
        "GIT_CONFIG_GLOBAL": "/attacker/config",
        "PYTHONPATH": "/attacker/python",
        "LD_PRELOAD": "/attacker/library.so",
        "SSH_AUTH_SOCK": "/attacker/agent.sock",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)

    safe = module._safe_environment(tmp_path)

    assert set(safe) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
    }
    assert safe["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_installer_requires_attestation_before_transaction_code():
    root = Path(__file__).parents[2]
    script = (root / "deploy/install-agent-principal-isolation.sh").read_text()
    attestation = script.index("AICC_BOOTSTRAP_ATTESTATION")
    transaction = script.index("run_transaction recover")

    assert attestation < transaction
    assert "AICC_EXPECTED_RELEASE_SHA" in script
    assert "--verify-attestation" in script


def test_transaction_installs_future_root_owned_bootstrap():
    root = Path(__file__).parents[2]
    transaction = (root / "ops/aicc_install_transaction.py").read_text()
    verifier = (root / "ops/verify-agent-principal-boundary.sh").read_text()

    assert '"/usr/local/sbin/voyn-aicc-bootstrap"' in transaction
    assert "installed exact-SHA bootstrap SHA drifted" in verifier


def test_bootstrap_uses_absolute_system_python():
    root = Path(__file__).parents[2]
    bootstrap = (root / "ops/aicc_exact_sha_bootstrap.py").read_text()
    runbook = (
        root / "docs/operations/AGENT_PRINCIPAL_ISOLATION_ROLLOUT.md"
    ).read_text()

    assert bootstrap.startswith("#!/usr/bin/python3\n")
    assert "/usr/bin/python3 /var/lib/aicc-stage0/voyn-aicc-bootstrap" in runbook
    assert "cat-file blob" in runbook


def test_authority_file_rejects_untrusted_group():
    module = _module()
    safe = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o640,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
    )

    assert module._authority_file_is_safe(safe, {0})
    hostile = SimpleNamespace(**{**vars(safe), "st_gid": 1000})
    assert not module._authority_file_is_safe(hostile, {0})


def test_uninstall_is_attested_and_skips_authority_mutation(monkeypatch, tmp_path):
    module = _module()
    sha = "a" * 40
    repo = tmp_path / "attempt" / "repo"
    repo.mkdir(parents=True)
    attestation = module.TreeAttestation(
        expected_sha=sha,
        remote_main_sha=sha,
        tree_manifest_sha256="b" * 64,
        file_count=1,
        repository=module.TRUSTED_REMOTE,
        attempt_id="attempt-test",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(module, "_require_private_root_directory", lambda *a, **k: None)
    monkeypatch.setattr(module, "_fetch_exact_checkout", lambda *a, **k: repo)
    monkeypatch.setattr(module, "_verify_checkout", lambda *a, **k: attestation)
    monkeypatch.setattr(module, "_atomic_write", lambda *a, **k: None)
    monkeypatch.setattr(
        module,
        "_prepare_authority_file",
        lambda *a, **k: pytest.fail("uninstall must not mutate authority state"),
    )

    def fake_run(argv, *, cwd, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(module, "_run", fake_run)

    assert (
        module.main(["uninstall", "--expected-sha", sha, "--state-root", str(tmp_path)])
        == 0
    )
    assert calls == [
        [str(repo / "deploy/install-agent-principal-isolation.sh"), "--uninstall"]
    ]


def test_poisoned_repository_config_cannot_run_code_during_verification(tmp_path):
    """The attestation reads the tree of a checkout the attacker may control.

    `core.fsmonitor` and `core.hooksPath` are the two repository-config knobs
    that turn a plain `git ls-tree` into arbitrary root code execution. The
    verifier pins `core.fsmonitor=false` and runs config-free; this proves the
    poisoned config is inert rather than merely unused today.
    """
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    canary = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "poison.sh"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _git(repo, "config", "core.hooksPath", str(tmp_path))

    _verify(module, repo, env, sha)

    assert not canary.exists()


def test_environment_git_config_injection_is_ignored(tmp_path, monkeypatch):
    """An inherited GIT_CONFIG_* / GIT_DIR environment must not steer the
    privileged verification at all -- the bootstrap passes its own scrubbed
    environment rather than the caller's."""
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    attacker = tmp_path / "attacker.gitconfig"
    attacker.write_text("[core]\n\tfsmonitor = /bin/false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(attacker))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(attacker))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))

    assert _verify(module, repo, env, sha).expected_sha == sha


def test_checkout_replaced_after_attestation_is_detected(tmp_path):
    """Attestation-then-use is a TOCTOU window: the installer re-verifies the
    checkout against the recorded attestation instead of trusting the file."""
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    attestation = _verify(module, repo, env, sha)

    implanted = repo / module.REQUIRED_ENTRYPOINTS[0]
    implanted.chmod(0o755)
    implanted.write_text("trusted:implanted\n", encoding="utf-8")

    with pytest.raises(module.BootstrapRefused):
        _verify(module, repo, env, sha)
    assert attestation.expected_sha == sha


def test_hardlinked_payload_file_is_refused(tmp_path):
    """A hardlink leaves the attacker a writable alias to a file the verifier
    has already proven, so the payload must contain single-link files only."""
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    target = repo / module.REQUIRED_ENTRYPOINTS[0]
    os.link(target, tmp_path / "alias")

    with pytest.raises(module.BootstrapRefused, match="file shape is unsafe"):
        _verify(module, repo, env, sha)


def test_group_writable_payload_directory_is_refused(tmp_path):
    module = _module()
    repo, sha, env = _trusted_repo(module, tmp_path)
    (repo / "ops").chmod(0o775)

    with pytest.raises(module.BootstrapRefused, match="untrusted checkout"):
        _verify(module, repo, env, sha)


def test_attestation_cannot_be_replayed_for_another_sha(tmp_path, monkeypatch):
    """A recorded attestation authorises exactly one SHA; replaying it under a
    different `--expected-sha` must not admit the installer."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    attestation = state / "attestation.json"
    attestation.write_text(
        '{"expected_sha": "' + "a" * 40 + '", "remote_main_sha": "' + "a" * 40 + '",'
        ' "tree_manifest_sha256": "0", "file_count": 1,'
        ' "repository": "' + module.TRUSTED_REMOTE + '", "attempt_id": "x"}',
        encoding="utf-8",
    )
    attestation.chmod(0o600)
    monkeypatch.setattr(module, "_require_private_root_directory", lambda *a, **k: None)

    with pytest.raises(module.BootstrapRefused, match="identity mismatch"):
        module._verify_existing_attestation(
            attestation,
            tmp_path,
            "b" * 40,
            trusted_uid=os.getuid(),
            trusted_gid=os.getgid(),
        )


def test_attestation_outside_a_private_root_directory_is_refused(tmp_path):
    """The attestation only means anything while no unprivileged principal can
    rewrite it; a world-writable state root must be refused, not trusted."""
    module = _module()
    state = tmp_path / "state"
    state.mkdir(mode=0o777)

    with pytest.raises(module.BootstrapRefused):
        module._require_private_root_directory(state, create=False)


def test_installer_failure_leaves_no_completed_marker(tmp_path, monkeypatch):
    """A failed privileged installer must not record a completed generation:
    the durable evidence is what a later run and boot recovery believe."""
    module = _module()
    state = tmp_path / "state"
    (state / "attempts").mkdir(mode=0o700, parents=True)
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd, env):
        calls.append(argv)
        if argv[0].endswith("install-agent-principal-isolation.sh"):
            raise module.BootstrapRefused("installer failed")
        return SimpleNamespace(stdout=b"", stderr=b"")

    monkeypatch.setattr(module, "_require_private_root_directory", lambda *a, **k: None)
    monkeypatch.setattr(module, "_fetch_exact_checkout", lambda a, e, s: a / "repo")
    monkeypatch.setattr(
        module,
        "_verify_checkout",
        lambda repo, env, sha, attempt_id, **kw: module.TreeAttestation(
            expected_sha=sha,
            remote_main_sha=sha,
            tree_manifest_sha256="0" * 64,
            file_count=1,
            repository=module.TRUSTED_REMOTE,
            attempt_id=attempt_id,
        ),
    )
    monkeypatch.setattr(module, "_prepare_authority_file", lambda path: None)
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *a: None)
    # `_atomic_write` pins every durable record to root; the test asserts which
    # records exist, not that an unprivileged runner can create root-owned ones.
    monkeypatch.setattr(module.os, "fchown", lambda *a: None)

    with pytest.raises(module.BootstrapRefused, match="installer failed"):
        module.main(["--expected-sha", "a" * 40, "--state-root", str(state)])

    assert not list(state.rglob("completed.json"))
    assert list(state.rglob("attestation.json"))
