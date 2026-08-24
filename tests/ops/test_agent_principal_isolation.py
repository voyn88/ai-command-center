from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import agent_runner
from command_center.worker import handlers as worker_handlers


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
        "AICC_PUBLISH_DEPLOY_KEY",
        "AICC_WORKSPACE_AUTHORITY_KEY",
    ],
)
def test_agent_environment_refuses_every_publisher_authority(
    launcher, monkeypatch, tmp_path, key
):
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"{key}=secret\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_root_owned_regular", lambda *a, **k: True)
    with pytest.raises(launcher.LaunchRefused, match="not allowlisted"):
        launcher._validate_environment_file(env_file, "codex")


def test_model_auth_allowlist_is_provider_specific(launcher, monkeypatch, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("OPENAI_API_KEY=model-only\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_root_owned_regular", lambda *a, **k: True)
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
        tmp_path,
        Path("/run/aicc-agent-homes/test"),
        "aicc-agent-test.service",
        "aicc-agent-launcher@test.service",
        tmp_path.parent,
    )
    joined = "\n".join(command)
    assert "--uid=aicc-agent" in command
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
    assert (
        "--property=BindPaths=/run/aicc-agent-homes/test:/agent-home" in command
    )
    for inaccessible in (
        "/etc/aicc",
        "/var/lib/aicc-worker",
        "/var/lib/aicc-agent",
        "/run/aicc-agent-launcher",
        str(tmp_path.parent),
    ):
        assert inaccessible in joined
    assert "AICC_WORKSPACE_AUTHORITY_KEY" not in joined
    assert "VOYN_LEASE_DSN" not in joined
    assert "GH_TOKEN" not in joined


def test_transient_agent_requires_socket_broker_cgroup(launcher, tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/system.slice/system-aicc\\x2dagent\\x2dlauncher.slice/"
        "aicc-agent-launcher@9.service\n",
        encoding="utf-8",
    )
    assert (
        launcher._current_broker_unit(cgroup)
        == "aicc-agent-launcher@9.service"
    )
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
    assert str(agent_runner.PRINCIPAL_WORKSPACE_ROOTS_FILE) == str(
        launcher.ROOTS_FILE
    )

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

    launcher._prepare_workspace_permissions(workspace)

    assert stat.S_IMODE(workspace.stat().st_mode) == 0o2770
    assert stat.S_IMODE(nested.stat().st_mode) == 0o2770
    assert stat.S_IMODE(payload.stat().st_mode) == 0o660


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


def test_sigterm_ignoring_unit_escalates_and_is_proven_inactive(
    launcher, monkeypatch
):
    sealed_states = iter((False, False, True))
    monkeypatch.setattr(
        launcher, "_unit_is_sealed", lambda unit: next(sealed_states)
    )
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
            return subprocess.CompletedProcess(args, 0, stdout=b"inactive\n", stderr=b"")
        assert any("ControlGroup" in value for value in args)
        return control_group_result

    monkeypatch.setattr(launcher, "_systemctl", systemctl)
    assert not launcher._unit_is_sealed(unit)


def test_worker_runtime_sends_secrets_neither_in_argv_nor_env(
    monkeypatch, tmp_path
):
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


def test_deployment_definitions_pin_separate_non_login_identity():
    root = Path(__file__).parents[2]
    sysusers = (root / "deploy/sysusers.d/aicc-agent.conf").read_text()
    worker = (root / "deploy/systemd/aicc-worker.service").read_text()
    worker_template = (
        root / "deploy/systemd/voyn-aicc-worker@.service"
    ).read_text()
    socket_unit = (root / "deploy/systemd/aicc-agent-launcher.socket").read_text()
    launcher_unit = (
        root / "deploy/systemd/aicc-agent-launcher@.service"
    ).read_text()
    workspace_roots = (root / "deploy/aicc/agent-workspace-roots").read_text()
    assert "/usr/sbin/nologin" in sysusers
    assert "u aicc-worker " in sysusers
    assert "u aicc-agent " in sysusers
    assert "m aicc-agent aicc-workspace" in sysusers
    assert "m aicc-worker aicc-workspace" in sysusers
    assert "m aicc-worker aicc-publisher" in sysusers
    assert "m voynadmin aicc-publisher" in sysusers
    assert "User=aicc-worker" in worker
    assert "AICC_AGENT_PRINCIPAL_ISOLATION=required" in worker
    assert "NoNewPrivileges=true" in worker
    assert "ExecStart=/opt/aicc/.venv/bin/python -m command_center.worker" in worker_template
    assert "EnvironmentFile=/var/lib/voyn-aicc-credential-rotation/worker.env" in worker_template
    assert "TimeoutStopSec=3660s" in worker_template
    assert "TimeoutStartSec=180s" in worker_template
    assert "SocketUser=root" in socket_unit
    assert "SocketGroup=aicc-publisher" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "User=root" in launcher_unit
    assert "ExecStart=/usr/libexec/aicc-agent-launcher --serve-socket" in launcher_unit
    assert "/srv/aicc-workspaces" in workspace_roots
    assert "/home/" not in workspace_roots


def test_versioned_os_boundary_acceptance_is_fail_closed():
    root = Path(__file__).parents[2]
    verifier = (root / "ops/verify-agent-principal-boundary.sh").read_text()
    installer = (root / "deploy/install-agent-principal-isolation.sh").read_text()
    transaction = (root / "ops/aicc_install_transaction.py").read_text()
    assert "agent_uid" in verifier and "publisher_uid" in verifier
    assert "aicc-agent can read" not in verifier
    assert "runuser -u aicc-agent -- test -r" in verifier
    assert "aicc-agent-launcher.socket" in verifier
    assert "ProtectControlGroups" in verifier
    assert "AICC_AGENT_PRINCIPAL_BOUNDARY_OK" in verifier
    assert "load_workspace_authority_environment" in installer
    assert "/etc/aicc/workspace-authority.env" in installer
    assert "voyn-aicc-worker@.service.d/20-principal-isolation.conf" in transaction
    assert "_atomic_bytes" in transaction
    assert "self.restore(manifest)" in transaction
    assert '"--uninstall"' in installer
    assert "run_transaction rollback" in installer
    assert "voyn-aicc-worker@1.service" in verifier
    assert "voyn-aicc-worker@2.service" in verifier
    assert "voyn-aicc-worker-2.service" not in verifier
    assert "source " not in installer
