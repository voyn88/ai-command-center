#!/usr/bin/python3
"""Root-owned broker for running untrusted coding agents as ``aicc-agent``.

The worker has publisher, lease and checkpoint authority and deliberately
keeps ``NoNewPrivileges=yes``.  It therefore cannot and must not use sudo to
change identity.  This broker is socket-activated by systemd, validates one
small manifest, and asks PID 1 to create a separate transient service/cgroup
under the non-login ``aicc-agent`` UID.

There is no shell expansion and no caller-supplied environment or executable.
Provider credentials come only from root-owned, key-allowlisted EnvironmentFile
inputs.  The exact task workspace is bind-mounted at ``/workspace``; the real
home tree and every publisher authority path remain inaccessible.
"""

from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pwd
import re
import select
import selectors
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

FAILURE = "AICC_AGENT_LAUNCH_INFRA_FAILURE"
SOCKET_PATH = "/run/aicc-agent-launcher/control.sock"
ROOTS_FILE = Path("/etc/aicc/agent-workspace-roots")
COMMON_ENV_FILE = Path("/etc/aicc/agent.env")
PROVIDER_ENV_FILES = {
    "claude": Path("/etc/aicc/agent-claude.env"),
    "codex": Path("/etc/aicc/agent-codex.env"),
}
MODEL_AUTH_SOURCES = {
    "claude": Path("/var/lib/aicc-agent/claude/.claude/.credentials.json"),
    "codex": Path("/var/lib/aicc-agent/codex/.codex/auth.json"),
}
MODEL_AUTH_TARGETS = {
    "claude": Path(".claude/.credentials.json"),
    "codex": Path(".codex/auth.json"),
}
EPHEMERAL_HOME_ROOT = Path("/run/aicc-agent-homes")
EXECUTOR_BINARIES = {
    "claude": "/usr/local/bin/claude",
    "codex": "/usr/local/bin/codex",
}
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
QUARANTINE_ROOT = Path("/srv/aicc-quarantine")
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 128 * 1024
# Both streams are embedded as base64 in one response frame. Keep their
# combined encoded size below MAX_MANIFEST_BYTES, which is also the client's
# bounded line-reader limit; oversized output becomes retryable infrastructure
# while its potentially modified task workspace is preserved.
MAX_OUTPUT_BYTES = 512 * 1024
PROFILES = frozenset({"read_only", "trusted_development"})
MODEL_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
BROKER_UNIT_RE = re.compile(r"aicc-agent-launcher@[^/]{1,200}\.service")
MANIFEST_KEYS = frozenset(
    {
        "version",
        "run_id",
        "workspace",
        "executor",
        "profile",
        "prompt",
        "model",
        "timeout_seconds",
    }
)

# Model authentication only.  Anything that can publish, acquire a writer
# lease, sign task checkpoints, change Git transport, or select an executable
# is intentionally absent.
COMMON_AGENT_ENV_KEYS = frozenset(
    {
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
PROVIDER_AGENT_ENV_KEYS = {
    "claude": frozenset(
        {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_MODEL"}
    ),
    "codex": frozenset({"OPENAI_API_KEY"}),
}
FORBIDDEN_ENV_PREFIXES = (
    "AICC_PUBLISH_",
    "AICC_WORKSPACE_AUTHORITY_",
    "VOYN_LEASE_",
    "GH_",
    "GITHUB_",
    "GIT_",
    "SSH_",
)

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
CLAUDE_GIT_DENIES = [
    "Bash(git apply:*)",
    "Bash(git checkout:*)",
    "Bash(git restore:*)",
    "Bash(git switch:*)",
    "Bash(git stash:*)",
    "Bash(git push:*)",
    "Bash(git merge:*)",
    "Bash(git reset:*)",
    "Bash(git rebase:*)",
    "Bash(git clean:*)",
    "Bash(git branch -d:*)",
    "Bash(git branch -D:*)",
    "Bash(gh:*)",
]


class LaunchRefused(RuntimeError):
    pass


def _readline_limited(stream) -> bytes:
    value = stream.readline(MAX_MANIFEST_BYTES + 1)
    if not value or len(value) > MAX_MANIFEST_BYTES or not value.endswith(b"\n"):
        raise LaunchRefused("manifest is missing, unterminated, or too large")
    return value


def _load_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchRefused("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        raise LaunchRefused("manifest schema mismatch")
    if value["version"] != 1:
        raise LaunchRefused("unsupported manifest version")
    if not isinstance(value["run_id"], str) or not RUN_ID_RE.fullmatch(value["run_id"]):
        raise LaunchRefused("invalid run_id")
    if value["executor"] not in EXECUTOR_BINARIES:
        raise LaunchRefused("executor is not allowlisted")
    if value["profile"] not in PROFILES:
        raise LaunchRefused("profile is not allowlisted")
    prompt = value["prompt"]
    if not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise LaunchRefused("prompt must be a non-empty NUL-free string")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise LaunchRefused("prompt is too large")
    model = value["model"]
    if model is not None and (
        not isinstance(model, str) or not MODEL_RE.fullmatch(model)
    ):
        raise LaunchRefused("model is invalid")
    timeout = value["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 30 <= timeout <= 3600
    ):
        raise LaunchRefused("timeout_seconds is outside 30..3600")
    if not isinstance(value["workspace"], str):
        raise LaunchRefused("workspace must be a string")
    return value


def _regular_file_policy(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int | None,
    exact_mode: int | None,
    optional: bool = False,
) -> bool:
    """Apply one reusable, symlink-safe ownership and mode policy."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise LaunchRefused(f"required protected file is missing: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != expected_uid
        or (expected_gid is not None and info.st_gid != expected_gid)
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise LaunchRefused(f"protected file ownership or mode drifted: {path}")
    return True


def _root_owned_regular(path: Path, *, optional: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise LaunchRefused(f"required root-owned file is missing: {path}")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise LaunchRefused(f"file is not root-owned and non-writable: {path}")
    return True


def _private_agent_environment(path: Path, *, optional: bool = False) -> bool:
    try:
        agent_gid = grp.getgrnam("aicc-agent").gr_gid
    except KeyError as exc:
        raise LaunchRefused("aicc-agent group does not exist") from exc
    return _regular_file_policy(
        path,
        expected_uid=0,
        expected_gid=agent_gid,
        exact_mode=0o640,
        optional=optional,
    )


def _workspace_roots(path: Path = ROOTS_FILE) -> tuple[Path, ...]:
    _root_owned_regular(path)
    roots: list[Path] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise LaunchRefused("workspace root must be absolute")
        resolved = candidate.resolve(strict=True)
        if resolved != candidate or not resolved.is_dir():
            raise LaunchRefused("workspace root must be a real directory")
        roots.append(resolved)
    if len(roots) != 1:
        raise LaunchRefused("exactly one canonical workspace root is required")
    return tuple(roots)


def _validated_workspace(value: str, roots: tuple[Path, ...]) -> Path:
    raw = Path(value)
    if not raw.is_absolute():
        raise LaunchRefused("workspace is not absolute")
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_dir():
        raise LaunchRefused("workspace contains a symlink or is not a directory")
    if any(character.isspace() or character in ":\\" for character in str(resolved)):
        raise LaunchRefused("workspace path is unsafe for a systemd bind property")
    if not any(resolved.is_relative_to(root) for root in roots):
        raise LaunchRefused("workspace is outside the root allowlist")
    return resolved


def _validate_environment_file(path: Path, executor: str) -> bool:
    if not _private_agent_environment(path, optional=True):
        return False
    allowed = COMMON_AGENT_ENV_KEYS | PROVIDER_AGENT_ENV_KEYS[executor]
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise LaunchRefused(f"invalid environment line {path}:{line_no}")
        key, _value = stripped.split("=", 1)
        if key not in allowed or any(
            key.startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES
        ):
            raise LaunchRefused(f"environment key is not allowlisted: {key}")
    return True


def _validate_binary(path: str) -> None:
    candidate = Path(path)
    resolved = candidate.resolve(strict=True)
    for chain in (candidate.parents, resolved.parents):
        for parent in chain:
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise LaunchRefused(
                    f"executor path component is not immutable root-owned: {parent}"
                )
    link_info = candidate.lstat()
    if link_info.st_uid != 0 or link_info.st_mode & 0o022:
        raise LaunchRefused(f"executor link is not immutable root-owned: {path}")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise LaunchRefused(f"executor is not an immutable root-owned file: {path}")
    if not os.access(resolved, os.X_OK):
        raise LaunchRefused(f"executor is not executable: {path}")


def _prepare_agent_home(executor: str, run_id: str) -> Path:
    """Create one non-persistent provider home with a private auth copy.

    The model process necessarily reads its provider credential, but it must
    not be able to persist plugins, instructions, config or a modified token
    into a later task. The root broker copies only the one allowlisted auth
    file into a per-run tmpfs-backed path and removes it after cgroup exit.
    """
    try:
        account = pwd.getpwnam("aicc-agent")
    except KeyError as exc:
        raise LaunchRefused("aicc-agent user does not exist") from exc
    source = MODEL_AUTH_SOURCES[executor]
    _root_owned_regular(source)
    source_info = source.stat()
    if source_info.st_mode & 0o077:
        raise LaunchRefused(f"model auth source is not root-private: {source}")

    home = EPHEMERAL_HOME_ROOT / run_id
    try:
        home.mkdir(mode=0o700)
        target = home / MODEL_AUTH_TARGETS[executor]
        target.parent.mkdir(mode=0o700, parents=True)
        shutil.copyfile(source, target)
        for path in (home, target.parent, target):
            os.chown(path, account.pw_uid, account.pw_gid)
        target.chmod(0o600)
    except (FileExistsError, OSError) as exc:
        shutil.rmtree(home, ignore_errors=True)
        raise LaunchRefused("cannot prepare ephemeral model home") from exc
    return home


def _tracked_executables(workspace: Path) -> frozenset[Path]:
    """Return executable paths from the trusted index without changing Git state."""
    command = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={workspace}",
        "-C",
        str(workspace),
        "ls-files",
        "--stage",
        "-z",
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    result = subprocess.run(command, env=environment, capture_output=True, check=False)
    if result.returncode != 0:
        raise LaunchRefused("cannot read the task-local Git executable metadata")
    executable: set[Path] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, path_bytes = raw.split(b"\t", 1)
            mode, _object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise LaunchRefused("malformed task-local Git index metadata") from exc
        if stage != b"0":
            continue
        relative = Path(os.fsdecode(path_bytes))
        if relative.is_absolute() or ".." in relative.parts:
            raise LaunchRefused("Git index path escaped the workspace")
        candidate = workspace / relative
        if mode == b"100755":
            executable.add(candidate)
    return frozenset(executable)


def _prepare_workspace_permissions(workspace: Path) -> None:
    """Grant only the shared workspace group, never a publisher credential group.

    Each agent unit sees only its exact bind mount, so membership in
    ``aicc-workspace`` does not make sibling task directories reachable.
    Setgid directories plus UMask=0007 keep files created by the agent readable
    by the guarded publisher after the unit exits.
    """
    try:
        workspace_gid = grp.getgrnam("aicc-workspace").gr_gid
    except KeyError as exc:
        raise LaunchRefused("aicc-workspace group does not exist") from exc
    owner_uid = workspace.lstat().st_uid
    executable = _tracked_executables(workspace)
    for root, dirs, files in os.walk(workspace, followlinks=False):
        paths = [Path(root), *(Path(root) / name for name in dirs + files)]
        for path in paths:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise LaunchRefused(f"hard-linked workspace file refused: {path}")
            os.chown(path, owner_uid, workspace_gid, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(path, 0o2770, follow_symlinks=False)
            elif stat.S_ISREG(info.st_mode):
                mode = 0o770 if path in executable else 0o660
                os.chmod(path, mode, follow_symlinks=False)


def _provider_command(manifest: dict[str, Any]) -> list[str]:
    executor = manifest["executor"]
    profile = manifest["profile"]
    prompt = manifest["prompt"]
    model = manifest["model"]
    binary = EXECUTOR_BINARIES[executor]
    if executor == "claude":
        command = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits" if profile == "read_only" else "bypassPermissions",
        ]
        if profile == "read_only":
            command += ["--tools", ",".join(READ_ONLY_TOOLS)]
        else:
            command += ["--disallowedTools", ",".join(CLAUDE_GIT_DENIES)]
    elif executor == "codex":
        command = [
            binary,
            "exec",
            "--sandbox",
            "read-only" if profile == "read_only" else "workspace-write",
            "--color",
            "never",
        ]
    else:
        command = [
            binary,
            "-p",
            prompt,
            "--no-color",
            "--silent",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-ask-user",
        ]
        if profile == "read_only":
            command += ["--allow-tool", "read"]
        else:
            command += [
                "--allow-tool",
                "read",
                "--allow-tool",
                "write",
                "--allow-tool",
                "shell",
                "--deny-tool",
                "shell(git push)",
                "--deny-tool",
                "shell(git remote)",
            ]
    if model:
        command += ["--model", model]
    if executor == "codex":
        # Prompt is an untrusted positional value. Explicitly terminate Codex
        # option parsing so a leading flag cannot override the sandbox.
        command += ["--", prompt]
    return command


def _current_broker_unit(cgroup_file: Path = Path("/proc/self/cgroup")) -> str:
    """Resolve the socket-activated parent unit for lifecycle binding.

    The transient agent is BindsTo this connection service, so PID 1 tears the
    whole agent cgroup down even if the root broker is OOM-killed or crashes.
    """
    try:
        lines = cgroup_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LaunchRefused("cannot resolve broker cgroup") from exc
    for line in lines:
        cgroup_path = line.rsplit(":", 1)[-1]
        for component in reversed(cgroup_path.split("/")):
            if BROKER_UNIT_RE.fullmatch(component):
                return component
    raise LaunchRefused("process is not inside an AICC launcher service")


def _systemd_command(
    manifest: dict[str, Any],
    workspace: Path,
    agent_home: Path,
    unit: str,
    broker_unit: str,
    workspace_root: Path,
) -> list[str]:
    executor = manifest["executor"]
    timeout = int(manifest["timeout_seconds"])
    command = [
        SYSTEMD_RUN,
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--service-type=exec",
        f"--unit={unit}",
        f"--property=BindsTo={broker_unit}",
        f"--property=After={broker_unit}",
        "--uid=aicc-agent",
        "--gid=aicc-agent",
        "--working-directory=/workspace",
        "--setenv=HOME=/agent-home",
        "--setenv=XDG_CONFIG_HOME=/agent-home/config",
        "--setenv=XDG_CACHE_HOME=/agent-home/cache",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "--setenv=GIT_CONFIG_NOSYSTEM=1",
        "--setenv=GIT_CONFIG_GLOBAL=/dev/null",
        "--setenv=GIT_TERMINAL_PROMPT=0",
        "--setenv=GCM_INTERACTIVE=never",
        "--property=SupplementaryGroups=aicc-workspace",
        "--property=UMask=0007",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=",
        "--property=AmbientCapabilities=",
        "--property=PrivateTmp=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateMounts=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        "--property=ProtectControlGroups=yes",
        "--property=ProtectKernelTunables=yes",
        "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes",
        "--property=ProtectClock=yes",
        "--property=ProtectHostname=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes",
        "--property=KeyringMode=private",
        "--property=RemoveIPC=yes",
        "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "--property=KillMode=control-group",
        "--property=Delegate=no",
        "--property=MemoryMax=6G",
        "--property=MemoryHigh=5G",
        "--property=CPUQuota=300%",
        "--property=TasksMax=512",
        f"--property=RuntimeMaxSec={timeout + 30}",
        "--property=TimeoutStopSec=20",
        f"--property=InaccessiblePaths=/etc/aicc /var/lib/aicc-worker /var/lib/aicc-agent /run/aicc-agent-launcher {workspace_root}",
        f"--property=BindPaths={workspace}:/workspace",
        f"--property=BindPaths={agent_home}:/agent-home",
        "--property=ReadWritePaths=/workspace /agent-home",
    ]
    for env_file in (COMMON_ENV_FILE, PROVIDER_ENV_FILES[executor]):
        if _validate_environment_file(env_file, executor):
            command.append(f"--property=EnvironmentFile={env_file}")
    command += ["--", *_provider_command(manifest)]
    return command


def _peer_uid(sock: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise LaunchRefused("SO_PEERCRED is unavailable")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _authorised_peer(uid: int) -> bool:
    if uid == 0:
        return True
    try:
        account = pwd.getpwuid(uid)
        publisher_gid = grp.getgrnam("aicc-publisher").gr_gid
    except KeyError:
        return False
    return publisher_gid in os.getgrouplist(account.pw_name, account.pw_gid)


def _bounded_collect(
    proc: subprocess.Popen[bytes], on_limit: Any
) -> tuple[bytes, bytes]:
    """Incrementally drain both pipes without letting root memory grow unbounded."""
    if proc.stdout is None or proc.stderr is None:
        raise LaunchRefused("agent output pipes are unavailable")
    collected = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    selector.register(proc.stderr, selectors.EVENT_READ)
    try:
        while selector.get_map():
            for key, _events in selector.select(timeout=0.25):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = collected[key.fd]
                if len(buffer) + len(chunk) > MAX_OUTPUT_BYTES:
                    on_limit()
                    raise LaunchRefused("agent output exceeded the bounded transport")
                buffer.extend(chunk)
    finally:
        selector.close()
    proc.wait(timeout=25)
    return bytes(collected[proc.stdout.fileno()]), bytes(
        collected[proc.stderr.fileno()]
    )


def _systemctl(
    args: list[str], *, timeout: float = 10
) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            [SYSTEMCTL, *args],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _unit_is_sealed(unit: str) -> bool:
    expected_cgroup = CGROUP_ROOT / unit
    load = _systemctl(["show", unit, "--property=LoadState", "--value"])
    if load is None or load.returncode != 0:
        # D-Bus/manager/permission/transport errors are ambiguity, never proof.
        return False
    load_state = load.stdout.decode("utf-8", "replace").strip()
    if load_state == "not-found":
        return not expected_cgroup.exists()
    state = _systemctl(["show", unit, "--property=ActiveState", "--value"])
    if state is None or state.returncode != 0:
        return False
    active_state = state.stdout.decode("utf-8", "replace").strip()
    if active_state not in {"inactive", "failed"}:
        return False
    control_group = _systemctl(["show", unit, "--property=ControlGroup", "--value"])
    if control_group is None:
        return False
    if control_group.returncode != 0:
        return False
    cgroup = control_group.stdout.decode("utf-8", "replace").strip()
    if not cgroup:
        return not expected_cgroup.exists()
    procs = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs"
    try:
        return not procs.exists() or not procs.read_text(encoding="ascii").strip()
    except OSError:
        return False


def _seal_unit(unit: str) -> bool:
    """Stop and prove the transient cgroup dead, escalating once to SIGKILL."""
    if _unit_is_sealed(unit):
        return True
    _systemctl(["stop", unit], timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _unit_is_sealed(unit):
            return True
        time.sleep(0.1)
    _systemctl(["kill", "--kill-whom=all", "--signal=KILL", unit])
    _systemctl(["stop", unit], timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _unit_is_sealed(unit):
            return True
        time.sleep(0.1)
    return False


def _quarantine_workspace(workspace: Path, run_id: str) -> Path:
    """Atomically detach a workspace if PID 1 cannot prove its old agent dead."""
    QUARANTINE_ROOT.mkdir(mode=0o700, exist_ok=True)
    target = QUARANTINE_ROOT / run_id
    try:
        os.replace(workspace, target)
        return target
    except OSError:
        # A mount/filesystem failure must still block broker reuse. The marker
        # is checked before permission preparation on every later dispatch.
        marker = QUARANTINE_ROOT / (
            hashlib.sha256(str(workspace).encode("utf-8")).hexdigest() + ".blocked"
        )
        marker.write_text(str(workspace), encoding="utf-8")
        marker.chmod(0o600)
        workspace.chmod(0o000)
        return workspace


def _workspace_is_quarantined(workspace: Path) -> bool:
    marker = QUARANTINE_ROOT / (
        hashlib.sha256(str(workspace).encode("utf-8")).hexdigest() + ".blocked"
    )
    return marker.is_file()


def _seal_or_quarantine(unit: str, workspace: Path, run_id: str) -> Path | None:
    if _seal_unit(unit):
        return None
    return _quarantine_workspace(workspace, run_id)


def _serve_connected_socket(sock: socket.socket) -> int:
    agent_home: Path | None = None
    unit: str | None = None
    workspace: Path | None = None
    try:
        peer = _peer_uid(sock)
        if not _authorised_peer(peer):
            raise LaunchRefused("socket peer is not an authorised publisher")
        manifest = _load_manifest(_readline_limited(sock.makefile("rb", buffering=0)))
        workspace_roots = _workspace_roots()
        workspace = _validated_workspace(manifest["workspace"], workspace_roots)
        if _workspace_is_quarantined(workspace):
            raise LaunchRefused("workspace is quarantined after an unsealed agent")
        _prepare_workspace_permissions(workspace)
        _validate_binary(EXECUTOR_BINARIES[manifest["executor"]])
        agent_home = _prepare_agent_home(manifest["executor"], manifest["run_id"])
        unit = f"aicc-agent-{manifest['run_id']}-{os.getpid()}.service"
        command = _systemd_command(
            manifest,
            workspace,
            agent_home,
            unit,
            _current_broker_unit(),
            workspace_roots[0],
        )
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result: dict[str, tuple[bytes, bytes] | BaseException] = {}

        def collect() -> None:
            try:
                result["value"] = _bounded_collect(proc, lambda: _seal_unit(unit))
            except (LaunchRefused, OSError, subprocess.SubprocessError) as exc:
                result["error"] = exc

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        while thread.is_alive():
            readable, _, _ = select.select([sock], [], [], 0.25)
            if readable:
                # Any post-manifest input or EOF is cancellation.  The fixed
                # client never sends a second frame.
                try:
                    extra = sock.recv(1)
                except OSError:
                    extra = b""
                _seal_unit(unit)
                if not extra:
                    break
            if proc.poll() is not None:
                break
        thread.join(timeout=25)
        if thread.is_alive():
            _seal_unit(unit)
            raise LaunchRefused("agent cgroup did not terminate")
        if "error" in result:
            raise LaunchRefused(str(result["error"]))
        stdout, stderr = result.get("value", (b"", b""))  # type: ignore[assignment]
        response = {
            "version": 1,
            "exit_code": proc.returncode,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        }
    except (LaunchRefused, OSError, subprocess.SubprocessError) as exc:
        response = {
            "version": 1,
            "exit_code": 125,
            "stdout_b64": "",
            "stderr_b64": base64.b64encode(f"{FAILURE}: {exc}\n".encode()).decode(
                "ascii"
            ),
        }
    finally:
        if unit is not None:
            try:
                assert workspace is not None
                quarantined = _seal_or_quarantine(unit, workspace, manifest["run_id"])
                if quarantined is not None:
                    response = {
                        "version": 1,
                        "exit_code": 125,
                        "stdout_b64": "",
                        "stderr_b64": base64.b64encode(
                            (
                                f"{FAILURE}: agent cgroup unsealed; workspace "
                                f"quarantined at {quarantined}\n"
                            ).encode()
                        ).decode("ascii"),
                    }
            except (AssertionError, OSError) as exc:
                response = {
                    "version": 1,
                    "exit_code": 125,
                    "stdout_b64": "",
                    "stderr_b64": base64.b64encode(
                        f"{FAILURE}: agent cgroup unsealed and quarantine failed: {exc}\n".encode()
                    ).decode("ascii"),
                }
        if agent_home is not None:
            shutil.rmtree(agent_home, ignore_errors=True)
    try:
        sock.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
    except OSError:
        return 125
    return int(response["exit_code"] or 0)


def _client() -> int:
    try:
        raw = _readline_limited(sys.stdin.buffer)
        # Local schema validation avoids waking a privileged service for
        # malformed input; the server repeats it as the actual authority.
        _load_manifest(raw)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(SOCKET_PATH)
            sock.sendall(raw)
            response_raw = _readline_limited(sock.makefile("rb", buffering=0))
        response = json.loads(response_raw)
        if set(response) != {"version", "exit_code", "stdout_b64", "stderr_b64"}:
            raise LaunchRefused("launcher response schema mismatch")
        stdout = base64.b64decode(response["stdout_b64"], validate=True)
        stderr = base64.b64decode(response["stderr_b64"], validate=True)
        sys.stdout.buffer.write(stdout)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
        code = response["exit_code"]
        return int(code) if isinstance(code, int) and 0 <= code <= 255 else 125
    except (LaunchRefused, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"{FAILURE}: {exc}", file=sys.stderr)
        return 125


def main() -> int:
    if sys.argv == [sys.argv[0], "--client"]:
        return _client()
    if sys.argv == [sys.argv[0], "--serve-socket"]:
        if os.geteuid() != 0:
            print(f"{FAILURE}: server requires root", file=sys.stderr)
            return 125
        # In an Accept=yes socket service stdin/stdout refer to the accepted
        # AF_UNIX stream.  Duplicate once so Python owns a normal socket object.
        sock = socket.fromfd(0, socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            return _serve_connected_socket(sock)
        finally:
            sock.close()
    print("usage: aicc-agent-launcher --client|--serve-socket", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
