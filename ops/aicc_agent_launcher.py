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
import fcntl
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

# Kept as a named tuple so the formatter (target py314, PEP 758) cannot
# rewrite an `except (A, B):` clause into the unparenthesized form that is a
# SyntaxError on the Python 3.13 runtimes this launcher must also run on.
_SYSTEMCTL_ERRORS = (OSError, subprocess.SubprocessError)
_PROC_STAT_MISSING_ERRORS = (FileNotFoundError, ProcessLookupError)

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
    # No copilot entry: ADR-0010 keeps it disabled under principal isolation
    # (its credential carries GitHub authority). The .get() guard below turns
    # an unknown executor into a clean LaunchRefused (review findings on
    # 63cb072 and b311666).
}
MODEL_AUTH_TARGETS = {
    "claude": Path(".claude/.credentials.json"),
    "codex": Path(".codex/auth.json"),
}
EPHEMERAL_HOME_ROOT = Path("/run/aicc-agent-homes")
# The provider CLIs live in the content-addressed toolchain, not in
# /usr/local/bin: production no longer installs them with `npm install
# --global` as root (VOYN-W0-AICC-TOOLCHAIN-CONTENT-ADDRESSED). `current` is an
# atomically replaced symlink into a release proven by its own sha256 and a
# root-owned content manifest, so this path resolves to verified bytes or to
# nothing at all -- never to a half-installed package tree.
TOOLCHAIN_BIN = "/opt/aicc/toolchains/current/bin"
EXECUTOR_BINARIES = {
    "claude": f"{TOOLCHAIN_BIN}/claude",
    "codex": f"{TOOLCHAIN_BIN}/codex",
}
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
MOUNT = "/usr/bin/mount"
UMOUNT = "/usr/bin/umount"
NSENTER = "/usr/bin/nsenter"
HOST_MOUNT_NAMESPACE = "/proc/1/ns/mnt"
QUARANTINE_ROOT = Path("/srv/aicc-quarantine")
ACTIVE_UNIT_ROOT = Path("/run/aicc-agent-launcher/active")
WORKSPACE_BIND_ROOT = Path("/run/aicc-agent-workspace-binds")
CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 128 * 1024
# Both streams are embedded as base64 in one response frame. Keep their
# combined encoded size below MAX_MANIFEST_BYTES, which is also the client's
# bounded line-reader limit; oversized output becomes retryable infrastructure
# while its potentially modified task workspace is preserved.
MAX_OUTPUT_BYTES = 512 * 1024
MAX_GIT_INDEX_BYTES = 64 * 1024 * 1024
MAX_GIT_INDEX_ENTRIES = 1_000_000
MAX_MODEL_AUTH_BYTES = 16 * 1024 * 1024
PROFILES = frozenset({"read_only", "trusted_development"})
MODEL_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
BROKER_UNIT_RE = re.compile(r"aicc-agent-launcher@[^/]{1,200}\.service")
AGENT_UNIT_RE = re.compile(r"aicc-agent-[a-f0-9]{32}-[0-9]{1,20}\.service")
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
    "AICC_PG_",
    "AICC_PUBLISH_",
    "AICC_WORKSPACE_AUTHORITY_",
    "VOYN_LEASE_",
    "GH_",
    "GITHUB_",
    "GIT_",
    "SSH_",
)

# Host authority trees are denied independently of their current ownership or
# mode.  Some credentials are intentionally group-readable by the worker or by
# the transient model-auth group; a mode-only check would therefore regress as
# soon as the executor joins that group.  Keep this list explicit and rooted at
# durable authorities rather than individual filenames so rotations and new
# credential generations remain covered automatically.
SENSITIVE_AUTHORITY_TREES = (
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
    "/srv/aicc-quarantine",
)
SYSTEMD_RUN_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

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


class WorkspaceBind:
    """A broker-owned mount whose root is the already verified workspace fd."""

    __slots__ = ("identity", "journal", "path")

    def __init__(self, path: Path, journal: Path, identity: tuple[int, int]):
        self.path = path
        self.journal = journal
        self.identity = identity


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


def _read_exact_protected_file(
    path: Path, *, expected_uid: int, expected_gid: int, exact_mode: int
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LaunchRefused(f"cannot open protected file safely: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != expected_uid
            or info.st_gid != expected_gid
            or stat.S_IMODE(info.st_mode) != exact_mode
            or info.st_size > MAX_MODEL_AUTH_BYTES
        ):
            raise LaunchRefused(f"protected file ownership or mode drifted: {path}")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise LaunchRefused(f"protected file was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            final.st_dev != info.st_dev
            or final.st_ino != info.st_ino
            or final.st_size != info.st_size
            or final.st_mtime_ns != info.st_mtime_ns
            or final.st_ctime_ns != info.st_ctime_ns
            or final.st_uid != expected_uid
            or final.st_gid != expected_gid
            or stat.S_IMODE(final.st_mode) != exact_mode
        ):
            raise LaunchRefused(f"protected file changed while being read: {path}")
        return payload
    finally:
        os.close(descriptor)


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
        # A DEDICATED group for ephemeral model-credential homes. The shared
        # aicc-workspace output group also contains the guarded publisher and
        # operators; keying the live provider token to it let every one of
        # them read every agent's credential (review finding on 363e91d).
        # aicc-agent-auth has no human or publisher members -- only the
        # transient agent units join it.
        auth_gid = grp.getgrnam("aicc-agent-auth").gr_gid
    except KeyError as exc:
        raise LaunchRefused("aicc-agent-auth group does not exist") from exc
    source = MODEL_AUTH_SOURCES.get(executor)
    if source is None:
        raise LaunchRefused(f"no model auth source for executor {executor!r}")
    source_payload = _read_exact_protected_file(
        source, expected_uid=0, expected_gid=0, exact_mode=0o600
    )

    home = EPHEMERAL_HOME_ROOT / run_id
    try:
        home.mkdir(mode=0o700)
        target = home / MODEL_AUTH_TARGETS[executor]
        target.parent.mkdir(mode=0o700, parents=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o660)
        try:
            os.fchmod(descriptor, 0o660)
            os.fchown(descriptor, 0, auth_gid)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(source_payload)
                stream.flush()
                os.fsync(stream.fileno())
            target_info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(target_info.st_mode)
                or target_info.st_nlink != 1
                or target_info.st_uid != 0
                or target_info.st_gid != auth_gid
                # 0660: Claude/Codex rewrite their auth file on token
                # refresh; 0640 broke refresh mid-run while the 0770 home
                # let the agent replace the file anyway (review on 67a0a96).
                or stat.S_IMODE(target_info.st_mode) != 0o660
                or target_info.st_size != len(source_payload)
            ):
                raise LaunchRefused("ephemeral model auth target failed validation")
        finally:
            os.close(descriptor)
        for path in (home, target.parent):
            os.chown(path, 0, auth_gid)
            os.chmod(path, 0o770)
    except (FileExistsError, OSError) as exc:
        shutil.rmtree(home, ignore_errors=True)
        raise LaunchRefused("cannot prepare ephemeral model home") from exc
    return home


def _parse_git_index_payload(data: bytes, workspace: Path) -> frozenset[Path]:
    signature, version, entry_count = struct.unpack_from("!4sLL", data)
    if (
        signature != b"DIRC"
        or version not in {2, 3, 4}
        or entry_count > MAX_GIT_INDEX_ENTRIES
    ):
        raise LaunchRefused("unsupported task-local Git index format")
    executable: set[Path] = set()
    offset = 12
    previous_path = b""
    for _ in range(entry_count):
        entry_start = offset
        if offset + 62 > len(data):
            raise LaunchRefused("truncated task-local Git index entry")
        mode = struct.unpack_from("!L", data, offset + 24)[0]
        index_flags = struct.unpack_from("!H", data, offset + 60)[0]
        offset += 62
        if index_flags & 0x4000:
            if version < 3 or offset + 2 > len(data):
                raise LaunchRefused("malformed extended Git index entry")
            offset += 2
        if version == 4:
            if offset >= len(data):
                raise LaunchRefused("truncated Git v4 path prefix")
            byte = data[offset]
            offset += 1
            strip_count = byte & 0x7F
            while byte & 0x80:
                if offset >= len(data):
                    raise LaunchRefused("truncated Git v4 path prefix")
                byte = data[offset]
                offset += 1
                strip_count = ((strip_count + 1) << 7) + (byte & 0x7F)
            if strip_count > len(previous_path):
                raise LaunchRefused("Git v4 path prefix escaped prior entry")
            end = data.find(b"\0", offset)
            if end < 0:
                raise LaunchRefused("unterminated Git v4 index path")
            path_bytes = (
                previous_path[: len(previous_path) - strip_count] + data[offset:end]
            )
            offset = end + 1
        else:
            declared_length = index_flags & 0x0FFF
            if declared_length < 0x0FFF:
                end = offset + declared_length
                if end >= len(data) or data[end] != 0:
                    raise LaunchRefused("malformed Git index path length")
            else:
                end = data.find(b"\0", offset)
                if end < 0:
                    raise LaunchRefused("unterminated Git index path")
            path_bytes = data[offset:end]
            entry_size = end + 1 - entry_start
            offset = entry_start + ((entry_size + 7) & ~7)
        if offset > len(data) or not path_bytes:
            raise LaunchRefused("malformed task-local Git index padding")
        if (
            path_bytes.startswith(b"/")
            or b"//" in path_bytes
            or any(
                component in {b"", b".", b".."} for component in path_bytes.split(b"/")
            )
        ):
            raise LaunchRefused("Git index path escaped the workspace")
        previous_path = path_bytes
        stage = (index_flags >> 12) & 0x3
        if stage:
            continue
        try:
            relative = Path(os.fsdecode(path_bytes))
        except ValueError as exc:
            raise LaunchRefused("invalid task-local Git index path") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise LaunchRefused("Git index path escaped the workspace")
        candidate = workspace / relative
        if mode == 0o100755:
            executable.add(candidate)
        elif mode not in {0o100644, 0o120000, 0o160000}:
            raise LaunchRefused("unsupported task-local Git index mode")
    while offset < len(data):
        if offset + 8 > len(data):
            raise LaunchRefused("truncated task-local Git index extension")
        extension = data[offset : offset + 4]
        extension_size = struct.unpack_from("!L", data, offset + 4)[0]
        offset += 8
        if offset + extension_size > len(data):
            raise LaunchRefused("truncated task-local Git index extension payload")
        # Split-index and sparse-index entries depend on state outside the
        # bounded entry table. Refuse them instead of silently clearing an
        # executable bit. Other optional extensions only cache derived data.
        if extension in {b"link", b"sdir"}:
            raise LaunchRefused("unsupported task-local Git index extension")
        # Git's on-disk contract (read-cache.c, read_index_extension):
        # an UPPERCASE-first signature is OPTIONAL -- readers that do not
        # understand it skip it ("All optional extensions are named with
        # capital letters"); a lowercase-first signature is MANDATORY -- a
        # reader that does not understand it must refuse the index. link/
        # sdir above are exactly such mandatory extensions, split out only
        # for their precise error messages. (A round-5 review claimed the
        # opposite polarity while citing read-cache.c; the code below
        # matches the actual reference implementation: skip unknown
        # uppercase, refuse unknown lowercase.)
        first = extension[:1]
        if not (b"A" <= first <= b"Z"):
            raise LaunchRefused("unknown mandatory task-local Git index extension")
        offset += extension_size
    return frozenset(executable)


def _tracked_executables(
    workspace: Path, workspace_fd: int | None = None
) -> frozenset[Path]:
    """Safely parse executable bits without invoking Git as root.

    The index is agent-controlled input. Running even a read-only Git command
    would load its config and may execute helpers such as ``core.fsmonitor``.
    This bounded parser accepts only the documented v2/v3/v4 on-disk formats,
    opens ``.git/index`` with O_NOFOLLOW, and treats every malformed field as
    infrastructure failure. No config, hook, object filter or helper runs.
    The parsed mode is convenience evidence only; it is never repository/SHA
    authority. Both Git object hash formats and documented all-zero skipHash
    trailers are accepted, then disambiguated by a complete structural parse.
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        workspace_descriptor = (
            os.dup(workspace_fd)
            if workspace_fd is not None
            else os.open(workspace, directory_flags)
        )
    except OSError as exc:
        raise LaunchRefused("task workspace is not a stable real directory") from exc
    try:
        git_descriptor = os.open(".git", directory_flags, dir_fd=workspace_descriptor)
    except OSError as exc:
        raise LaunchRefused("task-local Git metadata is not a real directory") from exc
    finally:
        os.close(workspace_descriptor)
    # O_NONBLOCK must be present on open, before fstat.  Otherwise an
    # attacker-controlled FIFO named `.git/index` can pin the root broker.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open("index", flags, dir_fd=git_descriptor)
    except OSError as exc:
        raise LaunchRefused("cannot open task-local Git index safely") from exc
    finally:
        os.close(git_descriptor)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 32
            or info.st_size > MAX_GIT_INDEX_BYTES
        ):
            raise LaunchRefused("task-local Git index shape is unsafe")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise LaunchRefused("task-local Git index was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_dev != info.st_dev
            or final.st_ino != info.st_ino
            or final.st_size != info.st_size
            or final.st_mtime_ns != info.st_mtime_ns
            or final.st_ctime_ns != info.st_ctime_ns
        ):
            raise LaunchRefused("task-local Git index changed while being read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    candidates: list[bytes] = []
    for checksum_size, digest in (
        (20, lambda value: hashlib.sha1(value, usedforsecurity=False).digest()),
        (32, lambda value: hashlib.sha256(value).digest()),
    ):
        if len(raw) < 12 + checksum_size:
            continue
        content = raw[:-checksum_size]
        checksum = raw[-checksum_size:]
        if checksum == bytes(checksum_size) or digest(content) == checksum:
            candidates.append(content)
    parsed: list[frozenset[Path]] = []
    errors: list[LaunchRefused] = []
    for candidate in candidates:
        try:
            parsed.append(_parse_git_index_payload(candidate, workspace))
        except (LaunchRefused, struct.error) as exc:
            errors.append(
                exc
                if isinstance(exc, LaunchRefused)
                else LaunchRefused("truncated task-local Git index header")
            )
    if len(parsed) == 1:
        return parsed[0]
    if not parsed and errors:
        raise errors[0]
    raise LaunchRefused("task-local Git index checksum or format is ambiguous")


def _prepare_workspace_permissions(
    workspace: Path, pinned_workspace_fd: int | None = None
) -> None:
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
    executable = (
        _tracked_executables(workspace, pinned_workspace_fd)
        if pinned_workspace_fd is not None
        else _tracked_executables(workspace)
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    try:
        workspace_fd = (
            os.dup(pinned_workspace_fd)
            if pinned_workspace_fd is not None
            else os.open(workspace, directory_flags)
        )
    except OSError as exc:
        raise LaunchRefused("cannot open task workspace safely") from exc
    owner_uid = os.fstat(workspace_fd).st_uid

    # Iterative walk with an explicit stack: recursion held one descriptor
    # per nesting level of AGENT-CONTROLLED depth -- a deep tree exhausted
    # fds/stack inside the root broker (review on 67a0a96). The stack owns
    # every fd it holds and the depth is bounded outright.
    max_depth = 128
    max_open_dirs = 256
    stack: list[tuple[int, Path, int]] = [(workspace_fd, Path(), 0)]
    try:
        while stack:
            directory_fd, relative, depth = stack.pop()
            try:
                if depth > max_depth:
                    raise LaunchRefused("workspace nesting exceeds supported depth")
                os.fchown(directory_fd, owner_uid, workspace_gid)
                os.fchmod(directory_fd, 0o2770)
                with os.scandir(os.dup(directory_fd)) as entries:
                    snapshot = list(entries)
                for entry in snapshot:
                    before = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(before.st_mode):
                        continue
                    child_relative = relative / entry.name
                    if not (
                        stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)
                    ):
                        # A socket/FIFO/device left in the workspace is a
                        # permanent condition, not a TOCTOU race: refuse it by
                        # kind BEFORE open() so the diagnosis is honest and
                        # os.open won't hang/ENXIO on it (review on d8920b6).
                        raise LaunchRefused(
                            f"unsupported workspace node refused: {child_relative}"
                        )
                    flags = (
                        directory_flags if stat.S_ISDIR(before.st_mode) else file_flags
                    )
                    try:
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    except OSError as exc:
                        raise LaunchRefused(
                            f"workspace entry changed while opening: {child_relative}"
                        ) from exc
                    keep_open = False
                    try:
                        current = os.fstat(child_fd)
                        if (current.st_dev, current.st_ino) != (
                            before.st_dev,
                            before.st_ino,
                        ):
                            raise LaunchRefused(
                                f"workspace entry changed while opening: {child_relative}"
                            )
                        if stat.S_ISDIR(current.st_mode):
                            # Bound the descriptors this walk holds at once: a
                            # pathologically wide/deep agent tree otherwise
                            # pins one fd per pending directory and exhausts
                            # the root broker (review on 4a0a878). Fail closed
                            # past the budget rather than run out of fds.
                            if len(stack) >= max_open_dirs:
                                raise LaunchRefused(
                                    "workspace directory fan-out exceeds supported budget"
                                )
                            stack.append((child_fd, child_relative, depth + 1))
                            keep_open = True
                        elif stat.S_ISREG(current.st_mode):
                            if current.st_nlink != 1:
                                raise LaunchRefused(
                                    f"hard-linked workspace file refused: {child_relative}"
                                )
                            os.fchown(child_fd, owner_uid, workspace_gid)
                            path = workspace / child_relative
                            os.fchmod(child_fd, 0o770 if path in executable else 0o660)
                        else:
                            raise LaunchRefused(
                                f"unsupported workspace node refused: {child_relative}"
                            )
                    finally:
                        if not keep_open:
                            os.close(child_fd)
            finally:
                os.close(directory_fd)
    finally:
        for pending_fd, _pending_relative, _pending_depth in stack:
            os.close(pending_fd)


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
    agent_home: Path,
    unit: str,
    broker_unit: str,
    workspace_root: Path,
    workspace_source: Path,
) -> list[str]:
    executor = manifest["executor"]
    timeout = int(manifest["timeout_seconds"])
    # No nesting hazard in this list: EPHEMERAL_HOME_ROOT is
    # /run/aicc-agent-homes -- a SIBLING of /run/aicc-agent-launcher, not a
    # child, so systemd never overmounts a parent before lstat'ing a nested
    # entry (nesting-question raised in review on 67a0a96).
    # systemd fails a unit when a non-ignored InaccessiblePaths entry is
    # missing; several of these trees are created lazily (/srv/aicc-quarantine
    # on first quarantine, /run/credentials only under LoadCredential=), so a
    # fresh host would hard-fail every launch. The '-' prefix tolerates
    # absence while still masking the path when it exists (review on
    # 4a0a878). The workspace root and ephemeral home root always exist by
    # this point, so they stay mandatory.
    optional_trees = " ".join(f"-{tree}" for tree in SENSITIVE_AUTHORITY_TREES)
    inaccessible_paths = " ".join(
        (optional_trees, str(EPHEMERAL_HOME_ROOT), str(workspace_root))
    )
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
        # Each concurrently active transient unit receives a distinct host
        # UID.  ProtectProc therefore enforces a per-run process boundary;
        # task isolation no longer relies on one shared aicc-agent UID.
        "--property=DynamicUser=yes",
        "--working-directory=/workspace",
        "--setenv=HOME=/agent-home",
        "--setenv=XDG_CONFIG_HOME=/agent-home/config",
        "--setenv=XDG_CACHE_HOME=/agent-home/cache",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "--setenv=GIT_CONFIG_NOSYSTEM=1",
        "--setenv=GIT_CONFIG_GLOBAL=/dev/null",
        "--setenv=GIT_TERMINAL_PROMPT=0",
        "--setenv=GCM_INTERACTIVE=never",
        "--property=SupplementaryGroups=aicc-workspace aicc-agent-auth",
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
        f"--property=InaccessiblePaths={inaccessible_paths}",
        # The source is always a broker-created bind mount. There is no
        # pathname fallback: PID 1 must never resolve the mutable workspace
        # argument supplied by the publisher.
        f"--property=BindPaths={workspace_source}:/workspace",
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
    except _SYSTEMCTL_ERRORS:
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


def _workspace_bind_root_ready() -> None:
    """Create and verify the root-only parent for broker-owned bind mounts."""
    try:
        WORKSPACE_BIND_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = WORKSPACE_BIND_ROOT.lstat()
    except OSError as exc:
        raise LaunchRefused("workspace bind staging root is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise LaunchRefused("workspace bind staging root is not immutable root-owned")


def _proc_starttime(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``: boot-relative start ticks.

    A bare PID is reusable, so ``kill(pid, 0)`` succeeding does not prove the
    *original* owner is still alive -- after the broker dies and its PID is
    reassigned, recovery would skip the stale journal forever and leak the
    mount.  The start-time is not reused within a boot, so pairing it with the
    PID gives a reuse-proof identity.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except _PROC_STAT_MISSING_ERRORS:
        return None
    except OSError:
        return None
    # comm (field 2) is parenthesised and may contain spaces/parens; the safe
    # split point is after the final ')'.  starttime is field 22, i.e. index 19
    # counting fields from 3 (state) after that split.
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _boot_id() -> str:
    """Per-boot random id; empty when unavailable so callers fail closed."""
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return ""


def _bind_owner_alive(
    owner_pid: int, recorded_starttime: object, recorded_boot_id: object
) -> bool:
    """Is the journal's recorded owner still the live process that staged it?

    Liveness is kill-based so it keeps working under a hardened ``/proc``; the
    recorded start-time (when both it and the live one are readable) turns a
    reused PID into a miss, and a changed boot id proves the owner is gone.
    """
    current_boot = _boot_id()
    # Both ids must be non-empty: "" means the read failed at journal time (or
    # now), which proves nothing about a reboot -- treating it as a mismatch
    # would umount a live owner's bind (review on 61248b7).
    if (
        isinstance(recorded_boot_id, str)
        and recorded_boot_id
        and current_boot
        and recorded_boot_id != current_boot
    ):
        return False
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # a live process owned by someone we cannot signal
    if isinstance(recorded_starttime, int):
        current_starttime = _proc_starttime(owner_pid)
        if current_starttime is not None and current_starttime != recorded_starttime:
            return False  # PID reused by a different process
    return True


def _workspace_bind_journal(path: Path, *, phase: str, pid: int) -> Path:
    journal = path.with_suffix(".json")
    payload = json.dumps(
        {
            "version": 1,
            "phase": phase,
            "pid": pid,
            "pid_starttime": _proc_starttime(pid),
            "boot_id": _boot_id(),
            "path": str(path),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_root_state(journal, payload)
    return journal


def _recover_workspace_bind_journals() -> None:
    """Unmount bind staging left by a broker killed before its finally block."""
    _workspace_bind_root_ready()
    for journal in WORKSPACE_BIND_ROOT.glob("*.json"):
        try:
            info = journal.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > 4096
            ):
                raise LaunchRefused("workspace bind recovery journal drifted")
            record = json.loads(journal.read_text(encoding="utf-8"))
            owner_pid = record.get("pid") if isinstance(record, dict) else None
            staging = (
                Path(record.get("path", "")) if isinstance(record, dict) else Path()
            )
            if not isinstance(record, dict) or (
                record.get("version") != 1
                or record.get("phase") not in {"PREPARED", "MOUNTED"}
                or not isinstance(owner_pid, int)
                or staging.parent != WORKSPACE_BIND_ROOT
                or staging.with_suffix(".json") != journal
            ):
                raise LaunchRefused("workspace bind recovery journal is invalid")
            if owner_pid != os.getpid() and _bind_owner_alive(
                owner_pid,
                record.get("pid_starttime"),
                record.get("boot_id"),
            ):
                continue
            # PREPARED may already be mounted if the broker died between the
            # mount syscall and the journal phase transition; umount is
            # harmless for the plain directory and removes that race too.
            subprocess.run(
                _host_mount_namespace_command([UMOUNT, "--", str(staging)]),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            journal.unlink(missing_ok=True)
            staging.rmdir()
        except FileNotFoundError:
            journal.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError) as exc:
            raise LaunchRefused("workspace bind recovery failed") from exc


def _validate_workspace_bind(binding: WorkspaceBind, workspace_fd: int) -> None:
    """Prove the staging mount still names exactly the pinned workspace inode."""
    try:
        parent = binding.path.parent.lstat()
        staging = binding.path.lstat()
        current = os.stat(binding.path, follow_symlinks=False)
        pinned = os.fstat(workspace_fd)
    except OSError as exc:
        raise LaunchRefused("workspace bind staging disappeared") from exc
    if (
        binding.path.parent != WORKSPACE_BIND_ROOT
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_gid != 0
        or stat.S_IMODE(parent.st_mode) != 0o700
        or stat.S_ISLNK(staging.st_mode)
        or not stat.S_ISDIR(staging.st_mode)
        or (current.st_dev, current.st_ino) != binding.identity
        or (pinned.st_dev, pinned.st_ino) != binding.identity
    ):
        raise LaunchRefused("workspace bind no longer names the verified inode")


def _host_mount_namespace_command(command: list[str]) -> list[str]:
    """Run a mount operation in PID 1's mount namespace, not the broker's.

    The broker unit uses ProtectSystem= and friends, so it lives in a slave
    mount namespace: mounts it creates never propagate back to the host, and
    PID 1 would resolve the BindPaths source to the empty staging directory
    underneath (review on 7d4391c).  Entering PID 1's namespace makes the
    bind visible to PID 1 and, through slave propagation, to the broker too.
    """
    return [NSENTER, f"--mount={HOST_MOUNT_NAMESPACE}", "--", *command]


def _prepare_workspace_bind(workspace_fd: int, run_id: str) -> WorkspaceBind:
    """Create an immutable mount source for PID 1 instead of passing a pathname.

    PID 1 cannot dereference the broker's ``/proc/self/fd/N``.  The broker
    therefore creates the bind mount while it still owns the verified fd and
    gives ``systemd-run`` only this root-owned staging mount.  The original fd
    remains open until cleanup, and the journal makes a killed broker's mount
    recoverable by the next socket activation.
    """
    _workspace_bind_root_ready()
    _recover_workspace_bind_journals()
    staging = WORKSPACE_BIND_ROOT / f"{run_id}-{os.getpid()}"
    journal = staging.with_suffix(".json")
    try:
        staging.mkdir(mode=0o700)
        identity_info = os.fstat(workspace_fd)
        identity = (identity_info.st_dev, identity_info.st_ino)
        _workspace_bind_journal(staging, phase="PREPARED", pid=os.getpid())
        result = subprocess.run(
            _host_mount_namespace_command(
                [MOUNT, "--bind", f"/proc/self/fd/{workspace_fd}", str(staging)]
            ),
            check=False,
            pass_fds=(workspace_fd,),
            capture_output=True,
            env=SYSTEMD_RUN_ENVIRONMENT,
        )
        if result.returncode:
            raise LaunchRefused("cannot create workspace bind staging mount")
        binding = WorkspaceBind(staging, journal, identity)
        _validate_workspace_bind(binding, workspace_fd)
        _workspace_bind_journal(staging, phase="MOUNTED", pid=os.getpid())
        return binding
    except (FileExistsError, OSError) as exc:
        raise LaunchRefused("cannot prepare workspace bind staging mount") from exc
    except BaseException:
        subprocess.run(
            _host_mount_namespace_command([UMOUNT, "--", str(staging)]),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        journal.unlink(missing_ok=True)
        staging.rmdir()
        raise


def _cleanup_workspace_bind(binding: WorkspaceBind) -> None:
    result = subprocess.run(
        _host_mount_namespace_command([UMOUNT, "--", str(binding.path)]),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise LaunchRefused(
            "workspace bind staging mount could not be unmounted; journal retained"
        )
    binding.journal.unlink(missing_ok=True)
    binding.path.rmdir()


def _open_pinned_workspace(workspace: Path) -> int:
    """Pin the validated directory inode until PID 1 consumes the bind source."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = workspace.stat(follow_symlinks=False)
        descriptor = os.open(workspace, flags)
        current = os.fstat(descriptor)
    except OSError as exc:
        raise LaunchRefused("task workspace changed before pinning") from exc
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        os.close(descriptor)
        raise LaunchRefused("task workspace was replaced before pinning")
    # Exclude the rename/replace race on the BindPaths source: systemd
    # resolves the bind SOURCE by path in PID 1 after this call, and
    # close_fds means the pinned descriptor is not conveyed to it (review on
    # 52ced1f). A rename can only happen if the PARENT directory is writable
    # by a non-root principal; require it rename-proof so the untrusted agent
    # cannot swap the workspace entry between here and PID 1's resolution.
    if not _parent_is_rename_proof(workspace):
        os.close(descriptor)
        raise LaunchRefused("task workspace parent is renamable by non-root")
    return descriptor


def _parent_is_rename_proof(workspace: Path) -> bool:
    """True iff the workspace's parent is root-owned and not group/other-
    writable -- i.e. no non-root principal can rename the workspace entry."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(workspace.parent, flags)
    except OSError as exc:
        raise LaunchRefused("task workspace parent is unavailable") from exc
    try:
        parent = os.fstat(parent_fd)
    finally:
        os.close(parent_fd)
    return parent.st_uid == 0 and not (parent.st_mode & 0o022)


def _workspace_is_quarantined(workspace: Path) -> bool:
    marker = QUARANTINE_ROOT / (
        hashlib.sha256(str(workspace).encode("utf-8")).hexdigest() + ".blocked"
    )
    return marker.is_file()


def _seal_or_quarantine(unit: str, workspace: Path, run_id: str) -> Path | None:
    if _seal_unit(unit):
        return None
    return _quarantine_workspace(workspace, run_id)


def _active_workspace_name(workspace: Path) -> str:
    return hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()


def _open_workspace_lock(workspace: Path) -> int:
    """Hold one root-owned lock for the complete broker/workspace lifecycle."""
    ACTIVE_UNIT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_info = ACTIVE_UNIT_ROOT.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise LaunchRefused("active-agent registry ownership or mode drifted")
    path = ACTIVE_UNIT_ROOT / f"{_active_workspace_name(workspace)}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise LaunchRefused("active-agent workspace lock drifted")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaunchRefused("workspace already has an active agent broker") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _active_workspace_record(workspace: Path) -> Path:
    return ACTIVE_UNIT_ROOT / f"{_active_workspace_name(workspace)}.json"


def _write_active_workspace_unit(workspace: Path, unit: str) -> None:
    if not AGENT_UNIT_RE.fullmatch(unit):
        raise LaunchRefused("active-agent unit name is invalid")
    payload = json.dumps(
        {"version": 1, "workspace": str(workspace), "unit": unit},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_root_state(_active_workspace_record(workspace), payload)


def _atomic_root_state(path: Path, payload: bytes) -> None:
    directory = path.parent
    directory_fd = os.open(
        directory,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    temporary = f".{path.name}.{os.getpid()}.{threading.get_ident()}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("active-agent registry write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _read_active_workspace_unit(workspace: Path) -> str | None:
    record = _active_workspace_record(workspace)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(record, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise LaunchRefused("active-agent registry record drifted")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise LaunchRefused("active-agent registry record changed while read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LaunchRefused("active-agent registry record is malformed") from exc
    unit = value.get("unit") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "workspace", "unit"}
        or value.get("version") != 1
        or value.get("workspace") != str(workspace)
        or not isinstance(unit, str)
        or not AGENT_UNIT_RE.fullmatch(unit)
    ):
        raise LaunchRefused("active-agent registry record is invalid")
    return unit


def _clear_active_workspace_unit(workspace: Path, unit: str) -> None:
    if _read_active_workspace_unit(workspace) != unit:
        return
    _active_workspace_record(workspace).unlink()
    directory = os.open(ACTIVE_UNIT_ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _seal_previous_workspace_agent(workspace: Path) -> None:
    """Prove a crash-left cgroup dead before touching shared file modes."""
    previous = _read_active_workspace_unit(workspace)
    if previous is None:
        return
    if not _seal_unit(previous):
        quarantine_id = hashlib.sha256(previous.encode("ascii")).hexdigest()[:32]
        quarantined = _quarantine_workspace(workspace, quarantine_id)
        raise LaunchRefused(
            f"previous agent cgroup is unsealed; workspace quarantined at {quarantined}"
        )
    _clear_active_workspace_unit(workspace, previous)


def _prepare_reusable_workspace(
    workspace: Path, workspace_fd: int | None = None
) -> None:
    # Never recurse over/chmod a workspace while a prior agent could still be
    # mutating it. The durable active-unit record survives a broker SIGKILL;
    # BindsTo normally kills that cgroup, and this check proves the result.
    _seal_previous_workspace_agent(workspace)
    if workspace_fd is None:
        _prepare_workspace_permissions(workspace)
    else:
        _prepare_workspace_permissions(workspace, workspace_fd)


def _serve_connected_socket(sock: socket.socket) -> int:
    agent_home: Path | None = None
    unit: str | None = None
    workspace: Path | None = None
    workspace_lock: int | None = None
    workspace_fd: int | None = None
    workspace_bind: WorkspaceBind | None = None
    unit_recorded = False
    try:
        peer = _peer_uid(sock)
        if not _authorised_peer(peer):
            raise LaunchRefused("socket peer is not an authorised publisher")
        manifest = _load_manifest(_readline_limited(sock.makefile("rb", buffering=0)))
        workspace_roots = _workspace_roots()
        workspace = _validated_workspace(manifest["workspace"], workspace_roots)
        # Lock and quarantine-check FIRST, pin under the lock: pinning before
        # the lock left the two path-based checks guarding a DIFFERENT object
        # than the pinned inode if the workspace was replaced in between
        # (review on 0f4d77e).
        if _workspace_is_quarantined(workspace):
            raise LaunchRefused("workspace is quarantined after an unsealed agent")
        workspace_lock = _open_workspace_lock(workspace)
        workspace_fd = _open_pinned_workspace(workspace)
        if _workspace_is_quarantined(workspace):
            raise LaunchRefused("workspace is quarantined after an unsealed agent")
        _prepare_reusable_workspace(workspace, workspace_fd)
        _validate_binary(EXECUTOR_BINARIES[manifest["executor"]])
        agent_home = _prepare_agent_home(manifest["executor"], manifest["run_id"])
        unit = f"aicc-agent-{manifest['run_id']}-{os.getpid()}.service"
        workspace_bind = _prepare_workspace_bind(workspace_fd, manifest["run_id"])
        _write_active_workspace_unit(workspace, unit)
        unit_recorded = True
        command = _systemd_command(
            manifest,
            agent_home,
            unit,
            _current_broker_unit(),
            workspace_roots[0],
            # PID 1 receives only the broker-created mount, never the mutable
            # workspace pathname. Revalidate the mount and fd immediately
            # before constructing the request to PID 1.
            workspace_bind.path,
        )
        # Keep this check adjacent to the syscall that hands the source to
        # PID 1; a future command-builder refactor must not move it earlier.
        _validate_workspace_bind(workspace_bind, workspace_fd)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=SYSTEMD_RUN_ENVIRONMENT,
            close_fds=True,
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
                elif unit_recorded:
                    _clear_active_workspace_unit(workspace, unit)
            except (AssertionError, LaunchRefused, OSError) as exc:
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
        if workspace_bind is not None:
            try:
                _cleanup_workspace_bind(workspace_bind)
            except (LaunchRefused, OSError) as exc:
                response = {
                    "version": 1,
                    "exit_code": 125,
                    "stdout_b64": "",
                    "stderr_b64": base64.b64encode(
                        f"{FAILURE}: workspace bind cleanup failed: {exc}\n".encode()
                    ).decode("ascii"),
                }
        if workspace_lock is not None:
            os.close(workspace_lock)
        if workspace_fd is not None:
            os.close(workspace_fd)
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
