from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import agent_runner
from command_center.worker import handlers as worker_handlers

# Every test here drives the privileged installer, so every test builds its
# fixture paths the way a host has them and runs under the permissive 0o002
# umask an operator may well have. See tests/ops/conftest.py.
pytestmark = pytest.mark.usefixtures("host_shaped_fixture_roots")


def _launcher_module():
    path = Path(__file__).parents[2] / "ops" / "aicc_agent_launcher.py"
    spec = importlib.util.spec_from_file_location("aicc_agent_launcher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher():
    return _launcher_module()


def _manifest(tmp_path: Path, **updates):
    value = {
        "version": 1,
        "run_id": "a" * 32,
        "workspace": str(tmp_path),
        "executor": "codex",
        "profile": "trusted_development",
        "prompt": "make one local commit",
        "model": None,
        "timeout_seconds": 900,
    }
    value.update(updates)
    return value


def test_manifest_has_closed_schema_and_no_command_or_environment(launcher, tmp_path):
    valid = _manifest(tmp_path)
    assert launcher._load_manifest((json.dumps(valid) + "\n").encode()) == valid
    for forbidden in ("environment", "argv", "binary", "publisher_token"):
        poisoned = {**valid, forbidden: "attacker-controlled"}
        with pytest.raises(launcher.LaunchRefused, match="schema"):
            launcher._load_manifest((json.dumps(poisoned) + "\n").encode())

    oversized = {**valid, "prompt": "x" * (launcher.MAX_PROMPT_BYTES + 1)}
    with pytest.raises(launcher.LaunchRefused, match="prompt is too large"):
        launcher._load_manifest((json.dumps(oversized) + "\n").encode())


@pytest.mark.parametrize(
    "key",
    [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "VOYN_LEASE_DSN",
        "AICC_PG_PASSWORD",
        "AICC_REVIEW_DSN",
        "PGPASSFILE",
        "AICC_PUBLISH_DEPLOY_KEY",
        "AICC_WORKSPACE_AUTHORITY_KEY",
    ],
)
def test_agent_environment_refuses_every_publisher_authority(
    launcher, monkeypatch, tmp_path, key
):
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"{key}=secret\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_private_agent_environment", lambda *a, **k: True)
    with pytest.raises(launcher.LaunchRefused, match="not allowlisted"):
        launcher._validate_environment_file(env_file, "codex")


def test_model_auth_allowlist_is_provider_specific(launcher, monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("OPENAI_API_KEY=model-only\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_private_agent_environment", lambda *a, **k: True)
    assert launcher._validate_environment_file(env_file, "codex")
    with pytest.raises(launcher.LaunchRefused):
        launcher._validate_environment_file(env_file, "claude")


def test_codex_keeps_inner_workspace_write_sandbox(launcher, tmp_path):
    command = launcher._provider_command(_manifest(tmp_path))
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "danger-full-access" not in command
    assert command[-2] == "--"
    assert command[-1] == "make one local commit"


def test_copilot_is_fail_closed_until_auth_is_model_only(launcher, tmp_path):
    for profile in ("read_only", "trusted_development"):
        poisoned = _manifest(tmp_path, executor="copilot", profile=profile)
        with pytest.raises(launcher.LaunchRefused, match="allowlisted"):
            launcher._load_manifest((json.dumps(poisoned) + "\n").encode())


@pytest.mark.parametrize(
    ("executor", "task_type"),
    [
        ("claude", "review"),
        ("claude", "implementation"),
        ("codex", "review"),
        ("codex", "implementation"),
    ],
)
def test_root_launcher_provider_argv_cannot_drift_from_worker_policy(
    launcher, tmp_path, executor, task_type
):
    profile = agent_runner.profile_for_task_type(task_type)
    manifest = _manifest(
        tmp_path,
        executor=executor,
        profile=profile,
        model="test-model",
    )
    worker_command = getattr(
        agent_runner,
        agent_runner.COMMAND_BUILDERS[executor],
    )(
        manifest["prompt"],
        task_type=task_type,
        model=manifest["model"],
    )
    expected = [launcher.EXECUTOR_BINARIES[executor], *worker_command[1:]]
    assert launcher._provider_command(manifest) == expected


def test_only_publisher_group_or_root_can_call_broker(launcher, monkeypatch):
    monkeypatch.setattr(
        launcher.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name=f"user-{uid}", pw_gid=100),
    )
    monkeypatch.setattr(
        launcher.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=200),
    )
    monkeypatch.setattr(
        launcher.os,
        "getgrouplist",
        lambda name, gid: [gid, 200] if name == "user-123" else [gid],
    )
    assert launcher._authorised_peer(0)
    assert launcher._authorised_peer(123)
    assert not launcher._authorised_peer(124)


def test_outer_unit_is_exact_workspace_and_cgroup_sealed(
    launcher, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        launcher, "_validate_environment_file", lambda *args, **kwargs: False
    )
    command = launcher._systemd_command(
        _manifest(tmp_path),
        Path("/run/aicc-agent-homes/test"),
        "aicc-agent-test.service",
        "aicc-agent-launcher@test.service",
        tmp_path.parent,
        tmp_path,
    )
    joined = "\n".join(command)
    assert "--property=DynamicUser=yes" in command
    assert "--uid=aicc-agent" not in command
    assert not any("User=aicc-agent" in value for value in command)
    assert "--property=NoNewPrivileges=yes" in command
    assert "--property=CapabilityBoundingSet=" in command
    assert "--property=AmbientCapabilities=" in command
    assert "--property=ProtectHome=tmpfs" in command
    assert "--property=ProtectProc=invisible" in command
    assert "--property=ProtectControlGroups=yes" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=Delegate=no" in command
    assert "--property=BindsTo=aicc-agent-launcher@test.service" in command
    assert f"--property=BindPaths={tmp_path}:/workspace" in command
    assert "--property=ReadWritePaths=/workspace /agent-home" in command
    assert "--property=BindPaths=/run/aicc-agent-homes/test:/agent-home" in command
    # `in command` only proves the expected property is *present*; a second,
    # broader `--property=ReadWritePaths=` or `--property=BindPaths=` entry
    # (systemd-run unions repeated list properties rather than overriding
    # them) would still satisfy that membership check while granting the
    # agent substantially wider host writes. Collect every occurrence of each
    # property and require the exact expected set, nothing more (review
    # finding on 5f2f1dd).
    read_write_paths = [
        value for value in command if value.startswith("--property=ReadWritePaths=")
    ]
    assert read_write_paths == ["--property=ReadWritePaths=/workspace /agent-home"]
    bind_paths = {
        value for value in command if value.startswith("--property=BindPaths=")
    }
    assert bind_paths == {
        f"--property=BindPaths={tmp_path}:/workspace",
        "--property=BindPaths=/run/aicc-agent-homes/test:/agent-home",
    }
    # Check membership against the EXACT InaccessiblePaths value, not a
    # substring of the whole argv: /run/aicc-agent-homes also appears in the
    # BindPaths line above, so `in joined` reported it masked even if the
    # mask were dropped (review on 27c06df).
    inaccessible_prop = next(
        value[len("--property=InaccessiblePaths=") :]
        for value in command
        if value.startswith("--property=InaccessiblePaths=")
    )
    # Optional trees carry a leading '-' (tolerate-absent); strip it for the
    # membership check. The two mandatory roots (workspace, ephemeral home)
    # have no prefix.
    masked = {entry.lstrip("-") for entry in inaccessible_prop.split()}
    for inaccessible in (
        "/etc/aicc",
        "/etc/voyn",
        "/home",
        "/root",
        "/var/lib/aicc-worker",
        "/var/lib/aicc-agent",
        "/var/lib/voyn-aicc-credential-rotation",
        "/run/aicc-agent-launcher",
        "/run/aicc-agent-workspace-binds",
        "/run/credentials",
        "/run/voyn-aicc-worker",
        "/run/aicc-agent-homes",
        "/srv/aicc-quarantine",
        str(tmp_path.parent),
    ):
        assert inaccessible in masked
    raw_entries = inaccessible_prop.split()
    assert "-/etc/aicc" in raw_entries, "optional trees must tolerate absence"
    assert str(tmp_path.parent) in raw_entries or f"{tmp_path.parent}" in raw_entries
    assert "AICC_WORKSPACE_AUTHORITY_KEY" not in joined
    assert "VOYN_LEASE_DSN" not in joined
    assert "AICC_PG_PASSWORD" not in joined
    assert "PGPASSFILE" not in joined
    assert "GH_TOKEN" not in joined


def test_broker_systemd_client_environment_is_closed_allowlist(launcher):
    assert launcher.SYSTEMD_RUN_ENVIRONMENT == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def test_transient_agent_requires_socket_broker_cgroup(launcher, tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/system.slice/system-aicc\\x2dagent\\x2dlauncher.slice/"
        "aicc-agent-launcher@9.service\n",
        encoding="utf-8",
    )
    assert launcher._current_broker_unit(cgroup) == "aicc-agent-launcher@9.service"
    cgroup.write_text("0::/system.slice/ssh.service\n", encoding="utf-8")
    with pytest.raises(launcher.LaunchRefused, match="not inside"):
        launcher._current_broker_unit(cgroup)


def test_workspace_allowlist_rejects_symlink_and_sibling(launcher, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    workspace = root / "task"
    workspace.mkdir()
    assert launcher._validated_workspace(str(workspace), (root,)) == workspace

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    with pytest.raises(launcher.LaunchRefused, match="outside"):
        launcher._validated_workspace(str(sibling), (root,))

    alias = root / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(launcher.LaunchRefused, match="symlink"):
        launcher._validated_workspace(str(alias), (root,))

    unsafe = root / "task:injected"
    unsafe.mkdir()
    with pytest.raises(launcher.LaunchRefused, match="unsafe"):
        launcher._validated_workspace(str(unsafe), (root,))


def test_workspace_bind_mounts_run_in_pid1_mount_namespace(
    launcher, monkeypatch, tmp_path
):
    # The broker's sandbox (ProtectSystem= etc.) puts it in a slave mount
    # namespace; a bind created there is invisible to PID 1, which resolves
    # the BindPaths source to the empty staging directory. Every mount and
    # umount must therefore enter PID 1's namespace.
    assert launcher._host_mount_namespace_command(["cmd", "arg"]) == [
        launcher.NSENTER,
        "--mount=/proc/1/ns/mnt",
        "--",
        "cmd",
        "arg",
    ]
    monkeypatch.setattr(launcher, "WORKSPACE_BIND_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_workspace_bind_root_ready", lambda: None)
    monkeypatch.setattr(launcher, "_recover_workspace_bind_journals", lambda: None)
    monkeypatch.setattr(
        launcher, "_workspace_bind_journal", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        launcher, "_validate_workspace_bind", lambda *args, **kwargs: None
    )
    commands = []

    def _record(command, **kwargs):
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(launcher.subprocess, "run", _record)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        binding = launcher._prepare_workspace_bind(descriptor, "run-nsenter")
        launcher._cleanup_workspace_bind(binding)
    finally:
        os.close(descriptor)
    assert len(commands) == 2
    for command in commands:
        assert command[:3] == [launcher.NSENTER, "--mount=/proc/1/ns/mnt", "--"]
    assert commands[0][3] == launcher.MOUNT
    assert commands[1][3] == launcher.UMOUNT


def test_lane_registry_parser_survives_set_u_and_detects_duplicates():
    root = Path(__file__).parents[2]
    verifier = (root / "ops/verify-agent-principal-boundary.sh").read_text()
    # Duplicate detection must use the subshell-local newline accumulator:
    # reading the unset outer variable aborts under `set -u`, and a
    # space-joined accumulator never matches `grep -Fqx`.
    assert "seen=''" in verifier
    assert 'grep -Fqx "$family_unit"' in verifier
    assert '"$lane_family_units" | grep -Fqx' not in verifier


def test_workspace_with_renamable_parent_is_refused(launcher, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(launcher.LaunchRefused, match="renamable"):
        launcher._open_pinned_workspace(workspace)


def test_workspace_bind_source_stays_on_pinned_inode_after_path_replacement(
    launcher, monkeypatch, tmp_path
):
    monkeypatch.setattr(launcher, "_parent_is_rename_proof", lambda workspace: True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "identity").write_text("original", encoding="utf-8")
    descriptor = launcher._open_pinned_workspace(workspace)
    try:
        original = os.fstat(descriptor)
        displaced = tmp_path / "displaced"
        workspace.rename(displaced)
        workspace.mkdir()
        (workspace / "identity").write_text("replacement", encoding="utf-8")
        replacement = workspace.stat()
        assert (original.st_dev, original.st_ino) != (
            replacement.st_dev,
            replacement.st_ino,
        )
        child = os.open("identity", os.O_RDONLY, dir_fd=descriptor)
        try:
            assert os.read(child, 32) == b"original"
        finally:
            os.close(child)
    finally:
        os.close(descriptor)


def test_systemd_bind_uses_explicit_pinned_workspace_source(
    launcher, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        launcher, "_validate_environment_file", lambda *args, **kwargs: False
    )
    pinned = Path(f"/proc/{os.getpid()}/fd/123")
    command = launcher._systemd_command(
        _manifest(tmp_path),
        Path("/run/aicc-agent-homes/test"),
        "aicc-agent-test.service",
        "aicc-agent-launcher@test.service",
        tmp_path.parent,
        pinned,
    )
    assert f"--property=BindPaths={pinned}:/workspace" in command
    assert f"--property=BindPaths={tmp_path}:/workspace" not in command


def test_workspace_bind_refuses_deterministic_symlink_replacement(launcher, tmp_path):
    bind_root = tmp_path / "binds"
    bind_root.mkdir(mode=0o700)
    launcher.WORKSPACE_BIND_ROOT = bind_root
    staging = bind_root / "run-a"
    staging.mkdir(mode=0o700)
    descriptor = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(descriptor)
        binding = launcher.WorkspaceBind(
            staging,
            staging.with_suffix(".json"),
            (info.st_dev, info.st_ino),
        )
        displaced = bind_root / "displaced"
        staging.rename(displaced)
        staging.symlink_to(displaced, target_is_directory=True)
        with pytest.raises(launcher.LaunchRefused, match="no longer names"):
            launcher._validate_workspace_bind(binding, descriptor)
    finally:
        os.close(descriptor)


def test_bind_owner_liveness_is_pid_reuse_proof(launcher, monkeypatch):
    # Host-independent: drive the identity helpers directly so the /proc-backed
    # branches are exercised on Linux and macOS alike.
    monkeypatch.setattr(launcher, "_boot_id", lambda: "boot-A")
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: None)  # PID is live
    monkeypatch.setattr(launcher, "_proc_starttime", lambda pid: 555)

    # Same live PID, matching recorded start-time -> still the original owner.
    assert launcher._bind_owner_alive(4242, 555, "boot-A")
    # Live PID but the recorded start-time differs: the PID was reused by a
    # different process, so the original bind owner is gone and the stale mount
    # must be reclaimed rather than skipped forever.
    assert not launcher._bind_owner_alive(4242, 999, "boot-A")
    # A journal written under a previous boot cannot name a current owner.
    assert not launcher._bind_owner_alive(4242, 555, "boot-B")
    # An empty recorded boot id means the read failed at journal time -- it
    # proves nothing about a reboot, so the live owner must be kept.
    assert launcher._bind_owner_alive(4242, 555, "")
    # Legacy journals (no recorded start-time/boot id) fall back to bare
    # liveness so an in-flight rolling deploy keeps working.
    assert launcher._bind_owner_alive(4242, None, None)

    def _dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(launcher.os, "kill", _dead)
    assert not launcher._bind_owner_alive(4242, None, None)


def test_worker_derived_workspace_is_accepted_by_same_canonical_root(
    launcher, monkeypatch, tmp_path
):
    canonical_root = tmp_path / "aicc-workspaces"
    canonical_root.mkdir()
    monkeypatch.setenv("AICC_AGENT_PRINCIPAL_ISOLATION", "required")
    monkeypatch.setattr(
        agent_runner, "principal_workspace_root", lambda: canonical_root
    )
    repository = tmp_path / "publisher" / "ai-command-center"
    repository.mkdir(parents=True)
    derived = worker_handlers._isolated_workspace_path(
        repository, "backlog/VOYN-W0-TEST"
    )
    derived.mkdir(parents=True)
    assert derived.is_relative_to(canonical_root)
    assert launcher._validated_workspace(str(derived), (canonical_root,)) == derived
    assert str(agent_runner.PRINCIPAL_WORKSPACE_ROOTS_FILE) == str(launcher.ROOTS_FILE)

    same_name_repository = tmp_path / "other-tenant" / "ai-command-center"
    same_name_repository.mkdir(parents=True)
    other = worker_handlers._isolated_workspace_path(
        same_name_repository, "backlog/VOYN-W0-TEST"
    )
    assert other != derived
    assert other.parent != derived.parent


def test_workspace_permission_normalization_clears_inherited_public_bits(
    launcher, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    payload = nested / "payload"
    payload.write_text("test", encoding="utf-8")
    workspace.chmod(0o777)
    nested.chmod(0o755)
    payload.chmod(0o777)
    monkeypatch.setattr(
        launcher.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid())
    )
    monkeypatch.setattr(launcher.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "_tracked_executables", lambda path: frozenset())

    launcher._prepare_workspace_permissions(workspace)

    assert stat.S_IMODE(workspace.stat().st_mode) == 0o2770
    assert stat.S_IMODE(nested.stat().st_mode) == 0o2770
    assert stat.S_IMODE(payload.stat().st_mode) == 0o660


def test_workspace_permission_normalization_preserves_tracked_executable_bit(
    launcher, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    script = workspace / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    data = workspace / "data.txt"
    data.write_text("payload\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "run.sh", "data.txt"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=AICC Test",
            "-c",
            "user.email=aicc@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    before = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert before == ""
    monkeypatch.setattr(
        launcher.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid())
    )
    monkeypatch.setattr(launcher.os, "chown", lambda *args, **kwargs: None)

    launcher._prepare_workspace_permissions(workspace)

    assert stat.S_IMODE(script.stat().st_mode) == 0o770
    assert stat.S_IMODE(data.stat().st_mode) == 0o660
    after = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert after == before


def test_workspace_index_parser_never_executes_malicious_git_fsmonitor(
    launcher, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    script = workspace / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    subprocess.run(["git", "-C", str(workspace), "add", "run.sh"], check=True)
    marker = tmp_path / "root-code-executed"
    malicious = tmp_path / "malicious-fsmonitor"
    malicious.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    malicious.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "core.fsmonitor", str(malicious)],
        check=True,
    )
    monkeypatch.setattr(
        launcher.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getgid())
    )
    monkeypatch.setattr(launcher.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("root launcher must never invoke Git")
        ),
    )

    launcher._prepare_workspace_permissions(workspace)

    assert stat.S_IMODE(script.stat().st_mode) == 0o770
    assert not marker.exists()


def _write_index(path: Path, *, version: int, mode: int, name: bytes) -> None:
    entry = bytearray(62)
    struct.pack_into("!L", entry, 24, mode)
    struct.pack_into("!H", entry, 60, min(len(name), 0xFFF))
    content = bytearray(struct.pack("!4sLL", b"DIRC", version, 1))
    entry_payload = entry + name + b"\0"
    if version in {2, 3}:
        entry_payload.extend(b"\0" * (-len(entry_payload) % 8))
    content.extend(entry_payload)
    digest = hashlib.sha1(content, usedforsecurity=False).digest()
    path.write_bytes(content + digest)


def test_workspace_index_parser_rejects_malformed_untrusted_inputs(
    launcher, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    index = git_dir / "index"

    index.write_bytes(b"DIRC\0\0")
    with pytest.raises(launcher.LaunchRefused):
        launcher._tracked_executables(workspace)

    _write_index(index, version=5, mode=0o100755, name=b"run.sh")
    with pytest.raises(launcher.LaunchRefused, match="unsupported"):
        launcher._tracked_executables(workspace)

    _write_index(index, version=2, mode=0o100755, name=b"../escape")
    with pytest.raises(launcher.LaunchRefused, match="escaped"):
        launcher._tracked_executables(workspace)

    _write_index(index, version=2, mode=0o100600, name=b"unsupported")
    with pytest.raises(launcher.LaunchRefused, match="mode"):
        launcher._tracked_executables(workspace)

    _write_index(index, version=2, mode=0o100755, name=b"run.sh")
    payload = index.read_bytes()
    content = payload[:-20] + b"link" + struct.pack("!L", 0)
    index.write_bytes(content + hashlib.sha1(content, usedforsecurity=False).digest())
    with pytest.raises(launcher.LaunchRefused, match="extension"):
        launcher._tracked_executables(workspace)

    monkeypatch.setattr(launcher, "MAX_GIT_INDEX_BYTES", 16)
    with pytest.raises(launcher.LaunchRefused, match="shape"):
        launcher._tracked_executables(workspace)


def test_workspace_index_fifo_is_refused_without_blocking(launcher, tmp_path):
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    os.mkfifo(git_dir / "index")
    import signal as _signal

    def _hang(_s, _f):
        raise AssertionError("FIFO open blocked -- non-blocking guarantee lost")

    prev = _signal.signal(_signal.SIGALRM, _hang)
    _signal.alarm(5)
    try:
        with pytest.raises(launcher.LaunchRefused, match="shape"):
            launcher._tracked_executables(workspace)
    finally:
        _signal.alarm(0)
        _signal.signal(_signal.SIGALRM, prev)


def test_workspace_index_open_is_fd_relative(launcher, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    _write_index(git_dir / "index", version=2, mode=0o100755, name=b"run.sh")
    real_open = launcher.os.open
    observed: list[tuple[object, object]] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        observed.append((path, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(launcher.os, "open", recording_open)
    launcher._tracked_executables(workspace)
    assert any(path == ".git" and dir_fd is not None for path, dir_fd in observed)
    assert any(path == "index" and dir_fd is not None for path, dir_fd in observed)


def test_prior_agent_cgroup_is_sealed_before_permission_normalization(
    launcher, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    active = tmp_path / "active"
    monkeypatch.setattr(launcher, "ACTIVE_UNIT_ROOT", active)
    active.mkdir(mode=0o700)
    unit = f"aicc-agent-{'a' * 32}-123.service"
    launcher._write_active_workspace_unit(workspace, unit)
    calls: list[str] = []
    monkeypatch.setattr(
        launcher,
        "_seal_unit",
        lambda name: calls.append(f"seal:{name}") or True,
    )
    monkeypatch.setattr(
        launcher,
        "_prepare_workspace_permissions",
        lambda path: calls.append(f"prepare:{path}"),
    )

    launcher._prepare_reusable_workspace(workspace)

    assert calls == [f"seal:{unit}", f"prepare:{workspace}"]
    assert not launcher._active_workspace_record(workspace).exists()


def test_provider_environment_policy_rejects_public_mode_and_symlink(
    launcher, tmp_path
):
    secret = tmp_path / "provider.env"
    secret.write_text("OPENAI_API_KEY=model-only\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(launcher.LaunchRefused, match="drifted"):
        launcher._regular_file_policy(
            secret,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            exact_mode=0o640,
        )
    secret.chmod(0o640)
    assert launcher._regular_file_policy(
        secret,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        exact_mode=0o640,
    )
    alias = tmp_path / "provider-link.env"
    alias.symlink_to(secret)
    with pytest.raises(launcher.LaunchRefused, match="drifted"):
        launcher._regular_file_policy(
            alias,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            exact_mode=0o640,
        )


def test_model_auth_reader_is_nofollow_and_exact_mode(launcher, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"model-only"}\n', encoding="utf-8")
    auth.chmod(0o644)
    with pytest.raises(launcher.LaunchRefused, match="drifted"):
        launcher._read_exact_protected_file(
            auth,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            exact_mode=0o600,
        )
    auth.chmod(0o600)
    assert launcher._read_exact_protected_file(
        auth,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        exact_mode=0o600,
    ).startswith(b'{"token"')
    alias = tmp_path / "auth-link.json"
    alias.symlink_to(auth)
    with pytest.raises(launcher.LaunchRefused, match="safely"):
        launcher._read_exact_protected_file(
            alias,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            exact_mode=0o600,
        )


def test_output_limit_is_incremental_and_triggers_seal(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "MAX_OUTPUT_BYTES", 1024)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'x' * 4096)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sealed: list[bool] = []
    try:
        with pytest.raises(launcher.LaunchRefused, match="bounded transport"):
            launcher._bounded_collect(
                proc,
                lambda: (sealed.append(True), proc.kill()),
            )
    finally:
        proc.kill()
        proc.wait()
    assert sealed == [True]


def test_sigterm_ignoring_unit_escalates_and_is_proven_inactive(launcher, monkeypatch):
    sealed_states = iter((False, False, True))
    monkeypatch.setattr(launcher, "_unit_is_sealed", lambda unit: next(sealed_states))
    monotonic = iter((0.0, 0.0, 11.0, 11.0, 12.0))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        launcher,
        "_systemctl",
        lambda args, **kwargs: calls.append(args),
    )
    assert launcher._seal_unit("aicc-agent-test.service")
    assert [
        "kill",
        "--kill-whom=all",
        "--signal=KILL",
        "aicc-agent-test.service",
    ] in calls


def test_systemctl_transport_error_is_unsealed_and_selects_quarantine(
    launcher, monkeypatch, tmp_path
):
    unit = "aicc-agent-transport-error.service"
    cgroup = tmp_path / "system.slice" / unit
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(launcher, "CGROUP_ROOT", tmp_path / "system.slice")
    monkeypatch.setattr(
        launcher,
        "_systemctl",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout=b"", stderr=b"transport failure"
        ),
    )
    assert not launcher._unit_is_sealed(unit)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selected: list[Path] = []
    monkeypatch.setattr(launcher, "_seal_unit", lambda name: False)
    monkeypatch.setattr(
        launcher,
        "_quarantine_workspace",
        lambda path, run_id: selected.append(path) or path,
    )
    assert launcher._seal_or_quarantine(unit, workspace, "a" * 32) == workspace
    assert selected == [workspace]


@pytest.mark.parametrize(
    "control_group_result",
    [
        subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"transport"),
        subprocess.CompletedProcess([], 0, stdout=b"\n", stderr=b""),
    ],
)
def test_control_group_error_or_empty_is_not_proof_when_expected_cgroup_exists(
    launcher, monkeypatch, tmp_path, control_group_result
):
    unit = "aicc-agent-still-present.service"
    expected = tmp_path / "system.slice" / unit
    expected.mkdir(parents=True)
    (expected / "cgroup.procs").write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(launcher, "CGROUP_ROOT", tmp_path / "system.slice")

    def systemctl(args, **kwargs):
        if any("LoadState" in value for value in args):
            return subprocess.CompletedProcess(args, 0, stdout=b"loaded\n", stderr=b"")
        if any("ActiveState" in value for value in args):
            return subprocess.CompletedProcess(
                args, 0, stdout=b"inactive\n", stderr=b""
            )
        assert any("ControlGroup" in value for value in args)
        return control_group_result

    monkeypatch.setattr(launcher, "_systemctl", systemctl)
    assert not launcher._unit_is_sealed(unit)


def test_worker_runtime_sends_secrets_neither_in_argv_nor_env(monkeypatch, tmp_path):
    capture = tmp_path / "capture.json"
    fake_launcher = tmp_path / "aicc-agent-launcher"
    fake_launcher.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        f"p={str(capture)!r}\n"
        "m=json.loads(sys.stdin.readline())\n"
        "open(p,'w').write(json.dumps({'argv':sys.argv,'env':dict(os.environ),'manifest':m}))\n"
        "print('agent completed')\n",
        encoding="utf-8",
    )
    fake_launcher.chmod(0o755)
    monkeypatch.setattr(
        agent_runner, "PRINCIPAL_ISOLATION_LAUNCHER", str(fake_launcher)
    )
    monkeypatch.setenv("AICC_AGENT_PRINCIPAL_ISOLATION", "required")
    monkeypatch.setenv("GH_TOKEN", "publisher-secret")
    monkeypatch.setenv("VOYN_LEASE_DSN", "lease-secret")
    monkeypatch.setenv("AICC_WORKSPACE_AUTHORITY_KEY", "hmac-secret")

    result = agent_runner.run_claude_code(
        repository_path=tmp_path,
        prompt="do local work",
        task_type="implementation",
        timeout_seconds=30,
        executor="codex",
    )
    assert result.status == "completed"
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed["argv"] == [str(fake_launcher), "--client"]
    assert observed["manifest"]["workspace"] == str(tmp_path)
    assert observed["manifest"]["profile"] == "trusted_development"
    serialized = json.dumps(observed)
    for secret in ("publisher-secret", "lease-secret", "hmac-secret"):
        assert secret not in serialized
    # macOS' platform Python wrapper may add SDK/CF variables after exec; the
    # caller-provided authority surface must still be absent.
    for key in observed["env"]:
        assert not key.startswith(
            (
                "AICC_PUBLISH_",
                "AICC_WORKSPACE_AUTHORITY_",
                "VOYN_LEASE_",
                "GH_",
                "GITHUB_",
                "GIT_",
                "SSH_",
            )
        )
    assert "HOME" not in observed["env"]


def test_deployment_definitions_pin_separate_non_login_identity(monkeypatch):
    root = Path(__file__).parents[2]
    sysusers = (root / "deploy/sysusers.d/aicc-agent.conf").read_text()
    worker = (root / "deploy/systemd/aicc-worker.service").read_text()
    worker_template = (root / "deploy/systemd/voyn-aicc-worker@.service").read_text()
    socket_unit = (root / "deploy/systemd/aicc-agent-launcher.socket").read_text()
    launcher_unit = (root / "deploy/systemd/aicc-agent-launcher@.service").read_text()
    workspace_roots = (root / "deploy/aicc/agent-workspace-roots").read_text()
    assert "/usr/sbin/nologin" in sysusers
    assert "u aicc-worker " in sysusers
    assert "u aicc-agent " in sysusers
    assert "m aicc-agent aicc-workspace" in sysusers
    assert "m aicc-worker aicc-workspace" in sysusers
    assert "m aicc-worker aicc-publisher" in sysusers
    assert "m voynadmin aicc-publisher" in sysusers
    assert "User=aicc-worker" in worker
    # The rollout runbook forbids shipping the fail-closed flag inside the
    # base unit: it arrives only via the final-canary-step drop-in
    # (independent-review REJECT on b6ea174, chunk 4/9). The base unit must
    # NOT carry it; the drop-in must.
    assert "AICC_AGENT_PRINCIPAL_ISOLATION=required" not in worker
    isolation_dropin = (
        root / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf"
    ).read_text()
    assert "AICC_AGENT_PRINCIPAL_ISOLATION=required" in isolation_dropin
    # The flag is only real if the transaction DELIVERS the drop-in to both
    # unit families -- asserting file contents alone proved nothing about
    # aicc-worker.service (independent-review finding on c00fc46).
    monkeypatch.syspath_prepend(str(root / "ops"))
    import aicc_install_transaction as _tx

    destinations = {
        str(spec.target)
        for spec in _tx.default_specs(
            root,
            authority_env=root / "x-authority.env",
            claude_auth=root / "x-claude.json",
            codex_auth=root / "x-codex.json",
            resolve_identities=False,
        )
        if str(spec.source).endswith("voyn-aicc-worker-principal-isolation.conf")
    }
    assert destinations == {
        "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf",
        "/etc/systemd/system/aicc-worker.service.d/20-principal-isolation.conf",
    }
    assert "NoNewPrivileges=true" in worker
    assert (
        "ExecStart=/opt/aicc/current/.venv/bin/python -m command_center.worker"
        in worker_template
    )
    assert "AICC_AGENT_PRINCIPAL_ISOLATION" not in worker_template
    assert (
        "EnvironmentFile=/var/lib/voyn-aicc-credential-rotation/worker.env"
        in worker_template
    )
    assert "TimeoutStopSec=3660s" in worker_template
    # 195s adopted from main (PR #382) at the merge of the two templates.
    assert "TimeoutStartSec=195s" in worker_template
    assert "RuntimeDirectory=voyn-aicc-worker/%i" in worker_template
    assert "PGPASSFILE=/run/voyn-aicc-worker/%i/pgpass" in worker_template
    assert "SocketUser=root" in socket_unit
    assert "SocketGroup=aicc-publisher" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "User=root" in launcher_unit
    assert "ExecStart=/usr/libexec/aicc-agent-launcher --serve-socket" in launcher_unit
    # The rendered-command assertion above (--property=DynamicUser=yes in the
    # built argv) is the real control; a raw-source substring is satisfied by
    # a comment (review note on c00fc46).
    assert "/srv/aicc-workspaces" in workspace_roots
    assert "/home/" not in workspace_roots


def test_versioned_os_boundary_acceptance_is_fail_closed():
    root = Path(__file__).parents[2]
    verifier = (root / "ops/verify-agent-principal-boundary.sh").read_text()
    installer = (root / "deploy/install-agent-principal-isolation.sh").read_text()
    transaction = (root / "ops/aicc_install_transaction.py").read_text()
    rollout = (root / "ops/aicc_staged_worker_rollout.py").read_text()
    assert "agent_uid" in verifier and "publisher_uid" in verifier
    assert "aicc-agent can read" not in verifier
    assert "runuser -u aicc-agent -- test -r" in verifier
    assert "aicc-agent-launcher.socket" in verifier
    assert "ProtectControlGroups" in rollout
    assert "AICC_AGENT_PRINCIPAL_BOUNDARY_OK" in verifier
    assert "load_workspace_authority_environment" in installer
    assert "/etc/aicc/workspace-authority.env" in installer
    assert "voyn-aicc-worker@.service.d/20-principal-isolation.conf" in transaction
    assert "_atomic_bytes" in transaction
    assert "self.restore(manifest)" in transaction
    assert '"PREPARED"' in transaction
    assert '"APPLIED"' in transaction
    assert "run_transaction commit" in installer
    assert '"--uninstall"' in installer
    assert "run_transaction recover" in installer
    assert "aicc_staged_worker_rollout.py" in verifier
    assert "/var/lib/aicc-agent/claude/.claude/.credentials.json" in verifier
    assert "/var/lib/aicc-agent/codex/.codex/auth.json" in verifier
    assert "discover_units" in rollout
    assert "for unit in units" in rollout
    assert "voyn-aicc-worker-2.service" not in verifier
    assert "lane_registry=/etc/aicc/worker-lanes" in verifier
    assert "/etc/voyn/aicc-worker-lanes.conf" not in verifier
    assert (
        'run_rollout snapshot --lanes "$repo_root/deploy/aicc/worker-lanes"'
        in installer
    )
    uninstall = installer[installer.index('if [ "${1:-}" = "--uninstall" ]') :]
    uninstall = uninstall.split("# Validate the stable authority")[0]
    assert "run_rollout snapshot --lanes /etc/aicc/worker-lanes" in uninstall
    assert "run_transaction uninstall-begin" in uninstall
    assert "run_transaction uninstall-arm" in uninstall
    assert "run_transaction uninstall-complete" in uninstall
    assert 'run_transaction quiesce --service-snapshot "$uninstall_units"' in uninstall
    assert uninstall.count("run_rollout verify-snapshot-closure") == 3
    assert uninstall.index("run_transaction recover") < uninstall.index(
        "baseline_release_value="
    )
    assert uninstall.index("release-verify") < uninstall.index(
        "run_rollout snapshot --lanes /etc/aicc/worker-lanes"
    )
    assert uninstall.index(
        "run_rollout snapshot --lanes /etc/aicc/worker-lanes"
    ) < uninstall.index("run_transaction quiesce")
    assert uninstall.rindex("run_rollout verify-snapshot-closure") < uninstall.rindex(
        "run_transaction uninstall-complete"
    )
    assert ": \"${AICC_INSTALL_LOCK_FD:?" in installer
    assert '--lock-fd "$AICC_INSTALL_LOCK_FD"' in installer
    assert "run_rollout rollout --lanes /etc/aicc/worker-lanes" in installer
    assert "repo_lanes=" not in installer
    assert "source " not in installer
    assert "O_NOFOLLOW" in verifier
    assert "st_uid != 0" in verifier
    assert "st_gid != 0" in verifier
    assert "st_ino" in verifier
    assert "changed while being read" in verifier


# ---------------------------------------------------------------------------
# Installation profiles. Before these existed there was one profile for every
# host, and it demanded the agent's Claude and Codex credentials
# unconditionally -- so installing the control plane meant either putting
# agent secrets on a host that must never hold them, or not installing it at
# all. The live attempt on control-01 took the second branch and stopped at
# `source is not a safe regular file: /home/voynadmin/.claude/.credentials.json`,
# on a file whose absence was correct.
# ---------------------------------------------------------------------------


def _profile_specs(profile, tmp_path):
    import importlib

    root = Path(__file__).parents[2]
    sys.path.insert(0, str(root / "ops"))
    tx = importlib.import_module("aicc_install_transaction")
    return tx, tx.default_specs(
        root,
        authority_env=tmp_path / "authority.env",
        claude_auth=tmp_path / "claude.json",
        codex_auth=tmp_path / "codex.json",
        resolve_identities=False,
        profile=profile,
    )


def _specs(profile, tmp_path):
    """The targets a profile INSTALLS.

    A removal spec also names a target, but asserts the opposite thing about
    it -- that the generation leaves it absent -- so it must never be counted
    as installed. `_purged` returns those.
    """
    tx, specs = _profile_specs(profile, tmp_path)
    return tx, {spec.target for spec in specs if not spec.remove}


def _purged(profile, tmp_path):
    """The FILE targets a profile removes in the same generation it installs."""
    _tx, specs = _profile_specs(profile, tmp_path)
    return {spec.target for spec in specs if spec.remove and not spec.directory}


def _purged_directories(profile, tmp_path):
    """The DIRECTORY targets a profile removes, in removal order."""
    _tx, specs = _profile_specs(profile, tmp_path)
    return [spec.target for spec in specs if spec.remove and spec.directory]


def test_worker_profile_is_unchanged_and_is_the_default(tmp_path):
    """An existing caller that knows nothing about profiles must install
    exactly what it always installed."""
    tx, explicit = _specs("worker", tmp_path)
    default = {
        spec.target
        for spec in tx.default_specs(
            Path(__file__).parents[2],
            authority_env=tmp_path / "authority.env",
            claude_auth=tmp_path / "claude.json",
            codex_auth=tmp_path / "codex.json",
            resolve_identities=False,
        )
    }
    assert explicit == default
    assert tx.WORKER_ONLY_TARGETS <= explicit


def test_control_profile_installs_no_agent_credentials(tmp_path):
    """The specific failure that stopped control-01: the profile must not ask
    for credentials a control-plane host is right not to have -- and, on a
    host that already ran the worker profile, must take the ones already
    there away rather than install "around" them."""
    _tx, targets = _specs("control", tmp_path)
    purged = _purged("control", tmp_path)

    for credential in (
        "/var/lib/aicc-agent/claude/.claude/.credentials.json",
        "/var/lib/aicc-agent/codex/.codex/auth.json",
    ):
        assert credential not in targets
        assert credential in purged


def test_control_profile_drops_every_worker_only_target_and_nothing_else(tmp_path):
    tx, control = _specs("control", tmp_path)
    _tx, worker = _specs("worker", tmp_path)

    assert worker - control == tx.WORKER_ONLY_TARGETS


def test_control_profile_purges_exactly_what_it_drops(tmp_path):
    """Every dropped target is paired with a removal in the same generation,
    and nothing else is removed. A drop alone only stops this transaction
    from writing the file; the file an earlier worker install left on disk
    stays live without the paired removal."""
    tx, _control = _specs("control", tmp_path)

    assert _purged("control", tmp_path) == tx.WORKER_ONLY_TARGETS
    assert _purged("worker", tmp_path) == set()
    assert _purged_directories("worker", tmp_path) == []


def test_control_purges_the_worker_only_directories_child_before_parent(tmp_path):
    """Removing the files and leaving the tree is a residual artefact tree:
    an empty /var/lib/aicc-agent/claude/.claude still names the secret that
    used to be in it, and /run/aicc-agent-homes is where the launcher
    materialised ephemeral copies of both credentials. The directories are
    removed in the same generation, after the files that empty them, and
    every directory strictly before its own parent -- rmdir cannot do it in
    any other order."""
    tx, _control = _specs("control", tmp_path)
    _tx, specs = _profile_specs("control", tmp_path)
    directories = _purged_directories("control", tmp_path)

    assert directories == list(tx.WORKER_ONLY_DIRECTORIES)
    for index, directory in enumerate(directories):
        for other in directories[index + 1 :]:
            assert not other.startswith(directory + "/"), (
                f"{other} is removed after its own parent {directory}"
            )
    ordered = [spec.target for spec in specs if spec.remove]
    assert ordered[: len(ordered) - len(directories)] == sorted(
        tx.WORKER_ONLY_TARGETS
    ), "a directory is removed before the files this generation takes out of it"

    # Operator data created by the agent layer is not an artefact of it.
    for kept in ("/srv/aicc-workspaces", "/srv/aicc-quarantine"):
        assert kept not in directories


def test_control_profile_needs_no_agent_identity_to_resolve_its_specs(tmp_path):
    """The agent group is created by the worker-only sysusers config, which a
    control host never runs. Resolving it unconditionally would fail a
    profile that installs nothing against it, on a host that never carried
    the agent layer and so does not have the group at all."""
    import importlib

    root = Path(__file__).parents[2]
    sys.path.insert(0, str(root / "ops"))
    tx = importlib.import_module("aicc_install_transaction")

    def only_publisher(name):
        if name != "aicc-publisher":
            raise KeyError(f"getgrnam(): name not found: {name}")
        return SimpleNamespace(gr_gid=4242)

    original = tx.grp.getgrnam
    tx.grp.getgrnam = only_publisher
    try:
        specs = tx.default_specs(
            root,
            authority_env=tmp_path / "authority.env",
            claude_auth=tmp_path / "claude.json",
            codex_auth=tmp_path / "codex.json",
            profile="control",
        )
        authority = next(
            spec
            for spec in specs
            if spec.target == "/etc/aicc/workspace-authority.env"
        )
        assert authority.gid == 4242, "the publisher group is required on every host"
        with pytest.raises(KeyError, match="aicc-agent"):
            tx.default_specs(
                root,
                authority_env=tmp_path / "authority.env",
                claude_auth=tmp_path / "claude.json",
                codex_auth=tmp_path / "codex.json",
                profile="worker",
            )
    finally:
        tx.grp.getgrnam = original


def test_control_profile_keeps_the_recovery_anchor_and_the_transaction_tool(tmp_path):
    """Dropping the agent layer must not drop the machinery that installs and
    recovers anything at all."""
    _tx, targets = _specs("control", tmp_path)

    assert "/usr/local/sbin/voyn-aicc-bootstrap" in targets
    assert "/usr/libexec/aicc-install-transaction" in targets
    assert "/etc/aicc/workspace-authority.env" in targets


def test_an_unknown_profile_is_refused_rather_than_treated_as_worker(tmp_path):
    tx, _targets = _specs("worker", tmp_path)
    root = Path(__file__).parents[2]

    with pytest.raises(ValueError, match="unknown installation profile"):
        tx.default_specs(
            root,
            authority_env=tmp_path / "authority.env",
            claude_auth=tmp_path / "claude.json",
            codex_auth=tmp_path / "codex.json",
            resolve_identities=False,
            profile="controlplane",
        )


def _installer_text() -> str:
    return (
        Path(__file__).parents[2] / "deploy" / "install-agent-principal-isolation.sh"
    ).read_text(encoding="utf-8")


def test_worker_to_control_is_one_generation_with_one_rollback_boundary():
    """Excluding a target from the transaction does not delete what is already
    on disk: a worker→control install that only skipped those specs left agent
    credentials and the launcher socket live on the control plane -- the
    precise boundary this installer exists to create (independent review of
    `090afcf`).

    Refusing the install and telling the operator to uninstall the worker
    profile first was the earlier answer, and it is worse than the disease:
    two transactions, each committing on its own, so a failure of the second
    leaves a host with the worker layer already gone and no control plane
    installed, and nothing to roll back to. The purge is therefore part of
    the control generation itself -- one prepare, one apply, one commit, one
    rollback boundary -- so no uninstall may commit ahead of it.
    """
    text = _installer_text()
    # Everything after the `--uninstall` branch exits: the install path
    # proper, where a control host's worker artefacts are now dealt with.
    install_path = text.split('if [ "${1:-}" = "--uninstall" ]; then', 1)[1].split(
        "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED\"\n  exit 0\nfi\n", 1
    )[1]

    assert "control profile refuses: worker artefacts present:" not in text
    assert "uninstall the worker profile first" not in text
    for action in (
        "run_transaction uninstall",
        "run_transaction uninstall-begin",
        "run_transaction uninstall-arm",
        "run_transaction uninstall-complete",
    ):
        assert action not in install_path, f"{action} runs before the control install"
    commands = [line.strip() for line in install_path.splitlines()]
    prepare = commands.index("run_transaction prepare")
    quiesce = commands.index("run_transaction quiesce-worker-only")
    apply_index = commands.index("run_transaction apply")
    commit = commands.index("run_transaction commit")

    # Exactly one of each: a second prepare/apply/commit anywhere on the
    # install path would be a second generation, and a second boundary.
    for action in ("prepare", "apply", "commit"):
        assert commands.count(f"run_transaction {action}") == 1
    # The worker units are stopped after their removal is staged and before
    # apply() takes the unit files away underneath them, so a failure at any
    # point is still a recoverable pending generation.
    assert prepare < quiesce < apply_index < commit
    guard = 'if [ "$install_profile" = "control" ]; then'
    assert guard in install_path[: install_path.index("run_transaction quiesce-worker-only")]


def test_the_agent_sysusers_and_tmpfiles_side_effects_are_worker_only():
    """Both configs build the agent layer outside the transaction:
    sysusers.d/aicc-agent.conf creates the `aicc-agent` principal, and
    tmpfiles.d/aicc-agent.conf creates /var/lib/aicc-agent -- including the
    two credential homes the control generation is purging. Running them on a
    control host would put back, as an untracked side effect, part of exactly
    what this profile removes."""
    text = _installer_text()
    guard = 'if [ "$install_profile" = "worker" ]; then'
    root = Path(__file__).parents[2]

    for line in (
        'systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-agent.conf"',
        'systemd-tmpfiles --create "$repo_root/deploy/tmpfiles.d/aicc-agent.conf"',
    ):
        assert line in text
        before = text[: text.rindex(line)]
        assert guard in before, f"{line} is not behind the worker-profile guard"
        assert "\nfi\n" not in before[before.rindex(guard):]

    # The control host still provisions the one identity its own specs
    # install against, and nothing else: no agent user, no workspace group,
    # and no tmpfiles directories at all.
    assert 'systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-control.conf"' in text
    control = (root / "deploy/sysusers.d/aicc-control.conf").read_text(encoding="utf-8")
    declarations = [
        line for line in control.splitlines() if line and not line.startswith("#")
    ]
    assert declarations == ["g aicc-publisher -"]
    assert "systemd-tmpfiles" not in text.split(
        'systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-control.conf"', 1
    )[1]


def test_the_agent_layer_is_only_enabled_for_the_worker_profile():
    """The launcher socket brokers agent principals, the rollout drives worker
    lanes, and the boundary verifier asserts a separation a control host has
    no parties for. None may run unconditionally."""
    text = _installer_text()
    guard = 'if [ "$install_profile" = "worker" ]; then'

    for line in (
        "systemctl enable --now aicc-agent-launcher.socket",
        "run_rollout rollout --lanes /etc/aicc/worker-lanes",
        '"$repo_root/ops/verify-agent-principal-boundary.sh"',
    ):
        assert line in text
        # The last occurrence: the boundary verifier is also named earlier,
        # where it is only being checked for existence.
        before = text[: text.rindex(line)]
        assert guard in before, f"{line} is not behind the worker-profile guard"
        # The guard must still be open where the line sits: no `fi` may close
        # it between the two, or the line runs unconditionally after all.
        assert "\nfi\n" not in before[before.rindex(guard):]


# ---------------------------------------------------------------------------
# The two comparisons that blocked installation on both hosts, 2026-08-31.
# ---------------------------------------------------------------------------


def _tx_module():
    import importlib

    sys.path.insert(0, str(Path(__file__).parents[2] / "ops"))
    return importlib.import_module("aicc_install_transaction")


def test_a_restored_command_property_ignores_the_last_invocation_fields():
    """systemd renders `ExecStart` with the *last run's* pid, exit code and
    timestamps appended. Those change every time the unit starts, so comparing
    the whole string makes a successful restore report failure — which is what
    kept worker-01's recovery from ever completing, leaving the WAL in place
    and refusing every later install.
    """
    tx = _tx_module()
    snapshot = (
        "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m command_center.worker ; "
        "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
        "code=(null) ; status=0/0 }"
    )
    after_a_run = (
        "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m command_center.worker ; "
        "ignore_errors=no ; start_time=[Sun 2026-08-31 01:00:00 UTC] ; "
        "stop_time=[n/a] ; pid=76841 ; code=(null) ; status=0/0 }"
    )

    assert tx._normalise_property(snapshot) == tx._normalise_property(after_a_run)


def test_a_changed_command_is_still_a_failed_restore():
    """The normalisation must not swallow the thing it exists to detect."""
    tx = _tx_module()
    original = (
        "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m command_center.worker ; "
        "ignore_errors=no ; pid=0 }"
    )
    replaced = (
        "{ path=/usr/bin/python ; argv[]=/usr/bin/python -m attacker ; "
        "ignore_errors=no ; pid=0 }"
    )

    assert tx._normalise_property(original) != tx._normalise_property(replaced)


def test_a_plain_property_value_is_compared_unchanged():
    tx = _tx_module()

    assert tx._normalise_property("/etc/systemd/system/x.service") == (
        "/etc/systemd/system/x.service"
    )
    assert tx._normalise_property("") == ""


def test_the_control_transition_revokes_the_authority_before_it_mutates_files():
    """The publisher group is the boundary on
    /etc/aicc/workspace-authority.env, and a converted host still had
    `aicc-worker` and `voynadmin` in it -- sysusers adds a membership and
    never takes one away. The revocation runs inside the same prepared
    generation, between prepare() and apply(), so its journal exists while
    the generation is still fully rollbackable and the same `recover` the
    trap runs undoes both together."""
    text = _installer_text()
    install_path = text.split('if [ "${1:-}" = "--uninstall" ]; then', 1)[1].split(
        "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED\"\n  exit 0\nfi\n", 1
    )[1]
    commands = [line.strip() for line in install_path.splitlines()]
    prepare = commands.index("run_transaction prepare")
    quiesce = commands.index("run_transaction quiesce-worker-only")
    revoke = commands.index("run_transaction revoke-worker-authority")
    apply_index = commands.index("run_transaction apply")

    assert commands.count("run_transaction revoke-worker-authority") == 1
    assert prepare < quiesce < revoke < apply_index
    guard = 'if [ "$install_profile" = "control" ]; then'
    before = install_path[: install_path.index("run_transaction revoke-worker-authority")]
    assert guard in before
    assert "\nfi\n" not in before[before.rindex(guard) :]
    # No chmod workaround: the file keeps its group ownership and mode, and
    # the membership is what changes.
    assert "chmod" not in install_path.split(
        "run_transaction revoke-worker-authority", 1
    )[0].rsplit(guard, 1)[1]


def test_the_authority_file_keeps_its_publisher_group_on_the_control_profile(tmp_path):
    """The boundary is not moved, it is enforced: the control profile still
    installs the authority key 0640 root:aicc-publisher, and the group is
    empty because deploy/sysusers.d/aicc-control.conf declares no members and
    the transition revokes the legacy ones."""
    tx, _targets = _specs("control", tmp_path)
    _tx, specs = _profile_specs("control", tmp_path)
    authority = next(
        spec for spec in specs if spec.target == "/etc/aicc/workspace-authority.env"
    )

    assert authority.mode == 0o640
    assert tx.AUTHORITY_GROUP == "aicc-publisher"
    assert set(tx.LEGACY_AUTHORITY_MEMBERS) == {"aicc-worker", "voynadmin"}
    agent_conf = (
        Path(__file__).parents[2] / "deploy/sysusers.d/aicc-agent.conf"
    ).read_text(encoding="utf-8")
    declared = {
        line.split()[1]
        for line in agent_conf.splitlines()
        if line.startswith("m ") and line.split()[2] == "aicc-publisher"
    }
    assert declared == set(tx.LEGACY_AUTHORITY_MEMBERS), (
        "a membership the worker profile grants and the transition forgets to "
        "revoke leaves the authority key readable by a worker-era principal"
    )


def test_the_control_profile_does_not_claim_to_remove_the_unix_principals(tmp_path):
    """sysusers has no removal verb and deleting a system account whose uid
    may still own inodes elsewhere is not an installer's call. What the
    profile actually takes away is files, directories and authority; the
    accounts are left inert and the docstring says so rather than claiming
    the agent principal is absent."""
    import inspect

    tx, _targets = _specs("control", tmp_path)
    documentation = tx.default_specs.__doc__
    body = inspect.getsource(tx.default_specs).replace(documentation, "")

    assert "NOT removed" in documentation
    assert "userdel" in documentation
    assert "inert" in documentation
    # The claim the docstring now disclaims must not survive anywhere else in
    # the function as an unqualified statement of fact.
    assert "whose whole point is that the agent" not in body
    assert "premise is that the agent principal does not exist" not in body


def test_the_rollback_and_the_uninstall_stop_the_broker_sessions_too():
    """`disable --now` on the socket stops it listening. The sessions it has
    already accepted are separate units running off the same template, and
    they outlive it -- so both paths that take the launcher out stop them as
    well, before the snapshot-closure check that refuses to mutate while any
    unit is live outside the snapshot."""
    text = _installer_text()
    stop_instances = "systemctl stop 'aicc-agent-launcher@*.service'"
    disable_socket = "systemctl disable --now aicc-agent-launcher.socket"

    assert text.count(stop_instances) == 2
    for segment in text.split(stop_instances)[:-1]:
        assert disable_socket in segment
        commands = [
            line.strip()
            for line in segment.splitlines()
            if line.strip().startswith("systemctl ")
        ]
        assert commands[-1].startswith(disable_socket), (
            "the socket must stop listening before its sessions are stopped"
        )
    rollback = text.split("rollback() {", 1)[1].split("\ntrap rollback", 1)[0]
    assert stop_instances in rollback
    assert rollback.index(stop_instances) < rollback.index("run_transaction recover")
