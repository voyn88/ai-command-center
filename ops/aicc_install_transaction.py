#!/usr/bin/env python3
"""Atomic, reversible installation of the principal-isolation file set."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

RESTORABLE_UNIT_RE = re.compile(
    r"(?:voyn-aicc-worker@[^/@\s]+\.service|"
    r"voyn-aicc-worker(?:-2)?\.service|"
    r"aicc-worker\.service|"
    r"aicc-agent-launcher\.socket|aicc-principal-recovery\.service)"
)
TEMPLATE_WORKER_UNIT_RE = re.compile(
    r"voyn-aicc-worker@[^/@\s]+\.service"
)
# Ordered: aicc_staged_worker_rollout imports this and iterates it; every
# set-equality check wraps it in set(...). One definition, no drift.
SNAPSHOT_PROPERTIES = (
    "FragmentPath",
    "DropInPaths",
    "User",
    "Group",
    "ExecStart",
    "WorkingDirectory",
    "EnvironmentFiles",
    "SupplementaryGroups",
    "NoNewPrivileges",
    "ProtectSystem",
    "ProtectHome",
    "ProtectControlGroups",
)
INSTALL_LOCK = Path("/var/lib/aicc-principal-isolation/install-recovery.lock")
RECOVERY_ANCHOR_TARGET = (
    "/usr/lib/systemd/system-generators/aicc-principal-recovery"
)


@dataclass(frozen=True)
class FileSpec:
    source: Path
    target: str
    mode: int
    uid: int
    gid: int
    if_missing: bool = False


@dataclass(frozen=True)
class BackupRecord:
    target: str
    existed: bool
    backup: str | None
    original_mode: int | None
    original_uid: int | None
    original_gid: int | None
    original_sha256: str | None
    staged: str
    install_sha256: str
    install_mode: int
    install_uid: int
    install_gid: int
    # Set when the target was a SYMLINK that this generation replaced with a
    # real file: the link's literal target, so rollback restores the link
    # rather than leaving a file where one never was. Appended last to keep the
    # positional constructor contract of every existing record, and defaulted
    # so a journal written by an earlier generation still loads.
    original_symlink: str | None = None


@dataclass(frozen=True)
class FileState:
    payload: bytes
    sha256: str
    mode: int
    uid: int
    gid: int


UNINSTALL_JOURNAL_VERSION = 2


def _path_present(path: Path) -> bool:
    """Return pathname presence without hiding a dangling or malformed symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _release_selector(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "ABSENT"
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("release selector is not a symlink")
    selector = os.readlink(path)
    if not re.fullmatch(r"releases/[0-9a-f]{40}", selector):
        raise RuntimeError("release selector is invalid")
    return selector


def _trusted_journal(path: Path) -> dict[str, object]:
    state = _read_regular(path, max_bytes=64 * 1024)
    if (
        state.uid not in {0, os.geteuid()}
        or state.gid not in {0, os.getegid()}
        or state.mode != 0o600
    ):
        raise RuntimeError("uninstall journal ownership or mode drifted")
    try:
        payload = json.loads(state.payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("uninstall journal is malformed") from exc
    if not isinstance(payload, dict):
        raise TypeError("uninstall journal is malformed")
    return payload


def _trusted_uninstall_recovery(
    state_dir: Path, payload: dict[str, object]
) -> Path:
    transaction_id = payload.get("transaction_id")
    recovery_value = payload.get("recovery")
    recovery_sha256 = payload.get("recovery_sha256")
    if (
        not isinstance(transaction_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", transaction_id)
        or not isinstance(recovery_value, str)
        or not isinstance(recovery_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", recovery_sha256)
    ):
        raise RuntimeError("uninstall recovery identity is invalid")
    recovery = Path(recovery_value)
    expected = state_dir.resolve() / f"uninstall-{transaction_id}" / "recovery.py"
    try:
        resolved = recovery.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("uninstall recovery capsule is unavailable") from exc
    if recovery != resolved or resolved != expected:
        raise RuntimeError("uninstall recovery capsule path drifted")
    state = _read_regular(resolved)
    if (
        state.mode != 0o700
        or state.uid not in {0, os.geteuid()}
        or state.gid not in {0, os.getegid()}
        or state.sha256 != recovery_sha256
    ):
        raise RuntimeError("uninstall recovery capsule drifted")
    return resolved


def _uninstall_identity(
    *, baseline_selector: str, current_selector: Path, lane_registry: Path
) -> dict[str, str]:
    if baseline_selector != "ABSENT" and not re.fullmatch(
        r"releases/[0-9a-f]{40}", baseline_selector
    ):
        raise RuntimeError("baseline release selector is invalid")
    registry = _read_regular(lane_registry, max_bytes=64 * 1024)
    if registry.mode & 0o022 or registry.uid not in {0, os.geteuid()}:
        raise RuntimeError("worker lane registry is not trusted")
    return {
        "baseline_selector": baseline_selector,
        "start_selector": _release_selector(current_selector),
        "registry_sha256": registry.sha256,
    }


def begin_uninstall(
    state_dir: Path,
    *,
    baseline_selector: str,
    current_selector: Path,
    lane_registry: Path,
) -> str:
    """Durably record uninstall intent before its service snapshot is made."""
    journal_path = state_dir / "uninstall.json"
    if _path_present(journal_path):
        payload = _trusted_journal(journal_path)
        required = {
            "version",
            "transaction_id",
            "phase",
            "baseline_selector",
            "start_selector",
            "registry_sha256",
            "snapshot_sha256",
            "recovery",
            "recovery_sha256",
        }
        if (
            set(payload) != required
            or payload["version"] != UNINSTALL_JOURNAL_VERSION
            or payload["phase"] not in {"INTENT", "ARMED", "COMPLETING"}
            or not isinstance(payload["transaction_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["transaction_id"])
            or payload["baseline_selector"] != baseline_selector
            or not isinstance(payload["start_selector"], str)
            or not isinstance(payload["registry_sha256"], str)
            or (
                payload["snapshot_sha256"] is not None
                and not isinstance(payload["snapshot_sha256"], str)
            )
            or not isinstance(payload["recovery"], str)
            or not isinstance(payload["recovery_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["recovery_sha256"])
        ):
            raise RuntimeError("uninstall journal identity drifted")
        _trusted_uninstall_recovery(state_dir, payload)
        current = _release_selector(current_selector)
        if current not in {
            payload["start_selector"],
            payload["baseline_selector"],
        }:
            raise RuntimeError("release selector changed during uninstall")
        if _path_present(lane_registry):
            registry = _read_regular(lane_registry, max_bytes=64 * 1024)
            if registry.sha256 != payload["registry_sha256"]:
                raise RuntimeError("worker lane registry changed during uninstall")
        elif payload["phase"] == "INTENT":
            raise RuntimeError("worker lane registry disappeared before uninstall armed")
        return str(payload["phase"])

    identity = _uninstall_identity(
        baseline_selector=baseline_selector,
        current_selector=current_selector,
        lane_registry=lane_registry,
    )
    transaction_id = secrets.token_hex(16)
    capsule_dir = state_dir / f"uninstall-{transaction_id}"
    capsule_dir.mkdir(mode=0o700)
    recovery = capsule_dir / "recovery.py"
    source = _read_regular(Path(__file__))
    _atomic_bytes(
        recovery,
        source.payload,
        0o700,
        os.geteuid(),
        os.getegid(),
    )
    _fsync_dir(capsule_dir)
    _fsync_dir(state_dir)
    payload: dict[str, object] = {
        "version": UNINSTALL_JOURNAL_VERSION,
        "transaction_id": transaction_id,
        "phase": "INTENT",
        **identity,
        "snapshot_sha256": None,
        "recovery": str(recovery.resolve(strict=True)),
        "recovery_sha256": source.sha256,
    }
    _atomic_bytes(
        journal_path,
        (json.dumps(payload, sort_keys=True) + "\n").encode(),
        0o600,
        os.geteuid(),
        os.getegid(),
    )
    return "INTENT"


def arm_uninstall(state_dir: Path, service_snapshot: Path) -> None:
    """Bind the uninstall intent to one immutable service snapshot."""
    journal_path = state_dir / "uninstall.json"
    payload = _trusted_journal(journal_path)
    _trusted_uninstall_recovery(state_dir, payload)
    snapshot = _read_regular(service_snapshot, max_bytes=4 * 1024 * 1024)
    if snapshot.mode != 0o600 or snapshot.uid not in {0, os.geteuid()}:
        raise RuntimeError("uninstall service snapshot is not trusted")
    phase = payload.get("phase")
    if phase == "ARMED":
        if payload.get("snapshot_sha256") != snapshot.sha256:
            raise RuntimeError("uninstall service snapshot drifted")
        return
    if phase != "INTENT" or payload.get("snapshot_sha256") is not None:
        raise RuntimeError("uninstall journal cannot be armed")
    payload["phase"] = "ARMED"
    payload["snapshot_sha256"] = snapshot.sha256
    _atomic_bytes(
        journal_path,
        (json.dumps(payload, sort_keys=True) + "\n").encode(),
        0o600,
        os.geteuid(),
        os.getegid(),
    )


def complete_uninstall(state_dir: Path, service_snapshot: Path) -> None:
    """Clean adjuncts first and consume the uninstall WAL strictly last."""
    journal_path = state_dir / "uninstall.json"
    payload = _trusted_journal(journal_path)
    recovery = _trusted_uninstall_recovery(state_dir, payload)
    phase = payload.get("phase")
    if phase == "ARMED":
        snapshot = _read_regular(service_snapshot, max_bytes=4 * 1024 * 1024)
        if payload.get("snapshot_sha256") != snapshot.sha256:
            raise RuntimeError("uninstall completion evidence drifted")
        payload["phase"] = "COMPLETING"
        _atomic_bytes(
            journal_path,
            (json.dumps(payload, sort_keys=True) + "\n").encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
    elif phase != "COMPLETING":
        raise RuntimeError("uninstall is not ready for completion")
    for adjunct in (
        service_snapshot,
        state_dir / "baseline-units.json",
        state_dir / "baseline-release",
        state_dir / "attempt-units.json",
    ):
        adjunct.unlink(missing_ok=True)
        _fsync_dir(state_dir)
    journal_path.unlink()
    _fsync_dir(state_dir)
    # The WAL is the safety authority and is consumed only after every
    # privileged restore is durable.  Its self-contained capsule is harmless
    # after that point; cleanup is deliberately post-commit so a crash can
    # leave only an inert orphan, never a journal without executable recovery.
    try:
        recovery.unlink(missing_ok=True)
        recovery.parent.rmdir()
        _fsync_dir(state_dir)
    except OSError:
        pass


def uninstall_phase(state_dir: Path) -> str:
    payload = _trusted_journal(state_dir / "uninstall.json")
    phase = payload.get("phase")
    if phase not in {"INTENT", "ARMED", "COMPLETING"}:
        raise RuntimeError("uninstall journal phase is invalid")
    return str(phase)


def _print_uninstall_phase(phase: str) -> None:
    """Emit only a closed-vocabulary status, never journal-derived text."""
    if phase == "INTENT":
        print("INTENT")
    elif phase == "ARMED":
        print("ARMED")
    elif phase == "COMPLETING":
        print("COMPLETING")
    else:
        raise RuntimeError("uninstall journal phase is invalid")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _validate_directory_fd(descriptor: int, path: Path) -> None:
    """Prove that an opened path component cannot be relocated by an
    untrusted principal while the installer uses its pinned descriptor.

    ``O_NOFOLLOW`` prevents symlink traversal, but it does not stop a writer
    of the parent directory from renaming an already-open child elsewhere.
    Every component must therefore be owned by root or by the installer and
    must not grant group/other rename authority.  A sticky directory (for
    example ``/tmp`` in an unprivileged test root) is the one safe exception:
    only the trusted owner of the child, the directory owner, or root can
    rename a trusted-owned child there.
    """
    info = os.fstat(descriptor)
    mode = stat.S_IMODE(info.st_mode)
    trusted_uids = {0, os.geteuid()}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in trusted_uids:
        raise ValueError(f"directory chain component is not trusted: {path}")
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise ValueError(f"directory chain component is rename-writable: {path}")


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open `path` by walking every component from `/` with O_NOFOLLOW.

    `prepare()` validates each target's parent chain once via `lstat`, but a
    write executed later (a separate `apply` invocation, in production a
    separate process started by the installer) never revisited that check
    before calling through to the rename below. A writable ancestor —
    especially beneath a deployment-owned directory such as
    `/var/lib/aicc-agent` — could be swapped for a symlink in between,
    letting the root-run installer escape `--root` and write attacker-chosen
    content, ownership or mode wherever the symlink points (review finding on
    5f2f1dd). Pinning every component as a directory fd, all the way from the
    filesystem root, immediately before use closes that window: once opened
    here nothing can redirect the descriptors this function returns.
    """
    if not path.is_absolute():
        raise ValueError(f"directory chain must be absolute: {path}")
    current_fd = os.open("/", _DIR_OPEN_FLAGS)
    try:
        current_path = Path("/")
        _validate_directory_fd(current_fd, current_path)
        for part in path.parts[1:]:
            current_path /= part
            try:
                next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=current_fd)
                next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current_fd)
            try:
                _validate_directory_fd(next_fd, current_path)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _install_lock_fd(
    path: Path = INSTALL_LOCK,
    inherited_fd: int | None = None,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> int:
    """Adopt or acquire the same persistent lock used by exact bootstrap."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("host kernel lacks required no-follow lock support")
    if not path.parent.exists():
        raise RuntimeError("install lock directory is missing")
    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    descriptor = -1
    try:
        parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != trusted_uid
            or parent.st_gid != trusted_gid
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise RuntimeError("install lock directory is unsafe")
        if inherited_fd is None:
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, trusted_uid, trusted_gid)
                os.fsync(descriptor)
                os.fsync(parent_fd)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                descriptor = os.open(
                    path.name,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
        else:
            if inherited_fd < 0:
                raise RuntimeError("invalid inherited install lock descriptor")
            try:
                descriptor = os.dup(inherited_fd)
            except OSError as exc:
                raise RuntimeError(
                    "invalid inherited install lock descriptor"
                ) from exc
        observed = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != trusted_uid
            or observed.st_gid != trusted_gid
            or stat.S_IMODE(observed.st_mode) != 0o600
            or (observed.st_dev, observed.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RuntimeError("install/recovery lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EACCES}:
                raise RuntimeError(
                    "another install or recovery owns the host lock"
                ) from exc
            raise RuntimeError("cannot acquire install/recovery lock") from exc
        return descriptor
    except RuntimeError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeError("cannot safely open install/recovery lock") from exc
    finally:
        os.close(parent_fd)


def _atomic_bytes(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    directory_fd = _open_directory_chain(path.parent, create=True)
    try:
        temporary = f".{path.name}.aicc-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, gid)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)


def _exclusive_bytes(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    directory_fd = _open_directory_chain(path.parent, create=True)
    descriptor = -1
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("host lacks no-follow exclusive-write support")
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def install_recovery_anchor(source: Path, target: Path) -> None:
    """Atomically install the permanent boot-recovery generator.

    The anchor intentionally lives outside reversible generations: it must
    exist before the first transaction WAL is created and must survive an
    interrupted uninstall. With no journal it emits a fast no-op barrier.
    """
    expected = _read_regular(source)
    if expected.mode & 0o022 or expected.uid not in {0, os.geteuid()}:
        raise RuntimeError("recovery anchor source is not trusted")
    _atomic_bytes(target, expected.payload, 0o755, 0, 0)
    installed = _read_regular(target)
    if (
        installed.sha256 != expected.sha256
        or installed.mode != 0o755
        or installed.uid != 0
        or installed.gid != 0
    ):
        raise RuntimeError("recovery anchor installation could not be proven")


def _read_regular(path: Path, *, max_bytes: int = 128 * 1024 * 1024) -> FileState:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > max_bytes
        ):
            raise RuntimeError(f"protected file shape is unsafe: {path}")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError(f"protected file was truncated: {path}")
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
            or final.st_uid != info.st_uid
            or final.st_gid != info.st_gid
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(info.st_mode)
        ):
            raise RuntimeError(f"protected file changed while being read: {path}")
        return FileState(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            gid=info.st_gid,
        )
    finally:
        os.close(descriptor)


def _digest_regular(
    path: Path, *, max_bytes: int = 8 * 1024 * 1024 * 1024
) -> tuple[str, int, int, int, int]:
    """`_read_regular`'s guarantees for a file too large to hold in memory.

    Returns `(sha256, size, mode, uid, gid)` and never materialises the
    content. The release manifest needs a digest of every file in a tree that
    includes ~300 MB native binaries -- `_read_regular` would read each one
    whole, which is why its 128 MB bound refused them outright (found on the
    first live install: the copilot binary is 180 MB, claude 311 MB).

    The safety properties are identical and deliberately kept in lockstep:
    O_NOFOLLOW so a symlink cannot be followed, a single link so no other name
    aliases the inode, a regular file only, and an fstat before and after that
    must agree on device, inode, size, both timestamps, ownership and mode --
    so a file rewritten while it was being hashed is refused rather than
    recorded. The bound stays, just at a size an artifact can actually be.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > max_bytes
        ):
            raise RuntimeError(f"protected file shape is unsafe: {path}")
        digest = hashlib.sha256()
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4 * 1024 * 1024))
            if not chunk:
                raise RuntimeError(f"protected file was truncated: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
        if (
            final.st_dev != info.st_dev
            or final.st_ino != info.st_ino
            or final.st_size != info.st_size
            or final.st_mtime_ns != info.st_mtime_ns
            or final.st_ctime_ns != info.st_ctime_ns
            or final.st_uid != info.st_uid
            or final.st_gid != info.st_gid
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(info.st_mode)
        ):
            raise RuntimeError(f"protected file changed while being read: {path}")
        return (
            digest.hexdigest(),
            info.st_size,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
        )
    finally:
        os.close(descriptor)


def _matches(state: FileState, sha256: str, mode: int, uid: int, gid: int) -> bool:
    return (
        state.sha256 == sha256
        and state.mode == mode
        and state.uid == uid
        and state.gid == gid
    )


#: Fields systemd reports inside a command property that describe the *last
#: invocation*, not the configuration. `ExecStart` is rendered as
#: `{ path=… ; argv[]=… ; ignore_errors=… ; start_time=… ; stop_time=… ;
#: pid=… ; code=… ; status=… }`, and the tail of that changes every time the
#: unit runs. Comparing the whole string therefore fails as soon as the
#: service has started once since the snapshot -- which is exactly the state a
#: recovery runs in.
_RUNTIME_COMMAND_FIELDS = ("start_time", "stop_time", "pid", "code", "status")


def _normalise_property(value: str) -> str:
    """A property with its last-invocation fields dropped.

    Restoration means the unit is configured as it was, not that it has the
    same process id it had. Keeping the runtime fields in the comparison made
    a *successful* restore report failure, leave the WAL in place and refuse
    every later install -- observed live on worker-01, where the correct
    `ExecStart` was in place and the recovery still raised
    `service snapshot property did not restore: voyn-aicc-worker.service
    ExecStart` (2026-08-31).
    """
    if "{" not in value or ";" not in value:
        return value.strip()
    kept = [
        part.strip()
        for part in value.strip().lstrip("{").rstrip("}").split(";")
        if part.strip()
        and not part.strip().startswith(_RUNTIME_COMMAND_FIELDS)
    ]
    return "{ " + " ; ".join(kept) + " }"



def restore_service_snapshot(
    path: Path, *, run=subprocess.run, defer_starts: bool = False
) -> None:
    """Restore the pre-attempt unit state after file generation recovery."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    units = payload.get("units")
    version = payload.get("version")
    if version not in {2, 3} or not isinstance(units, dict):
        raise RuntimeError("invalid interrupted-install service snapshot")
    validated: list[tuple[str, dict[str, bool]]] = []
    for unit, state in sorted(units.items(), reverse=True):
        if (
            not isinstance(unit, str)
            or not RESTORABLE_UNIT_RE.fullmatch(unit)
            or not isinstance(state, dict)
            or not isinstance(state.get("exists"), bool)
            or not isinstance(state.get("enabled"), bool)
            or not isinstance(state.get("active"), bool)
            or (
                version == 3
                and (
                    not isinstance(state.get("properties"), dict)
                    or (
                        state["exists"]
                        and set(state["properties"]) != set(SNAPSHOT_PROPERTIES)
                    )
                    or any(
                        not isinstance(name, str) or not isinstance(value, str)
                        for name, value in state["properties"].items()
                    )
                )
            )
        ):
            raise RuntimeError("invalid interrupted-install service unit")
        validated.append((unit, state))

    def systemctl(*arguments: str) -> None:
        result = run(
            ["/usr/bin/systemctl", *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(
                result.stderr.strip()
                or f"systemctl {' '.join(arguments)} failed during recovery"
            )

    def probe(*arguments: str) -> tuple[int, str]:
        result = run(
            ["/usr/bin/systemctl", *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        return result.returncode, result.stdout.strip()

    def assert_restored(
        unit: str, state: dict[str, bool], *, queued_start: bool = False
    ) -> None:
        load_rc, load_state = probe("show", unit, "--property=LoadState", "--value")
        pid_rc, main_pid = probe("show", unit, "--property=MainPID", "--value")
        _active_rc, active = probe("is-active", unit)
        _enabled_rc, enabled = probe("is-enabled", unit)
        # `MainPID` is a service property. A `.socket`, `.timer` or `.path`
        # unit has none, so systemd returns an empty value and a non-zero
        # probe -- and a unit the snapshot records as absent has none either,
        # for the obvious reason. Demanding it unconditionally made recovery
        # unable to prove the state of `aicc-agent-launcher.socket`, and a
        # recovery that cannot finish blocks every install behind it
        # (observed live on worker-01, 2026-08-31).
        expects_main_pid = unit.endswith(".service") and state["exists"]
        if (
            load_rc
            or not load_state
            or (expects_main_pid and (pid_rc or not main_pid))
        ):
            raise RuntimeError(f"cannot prove restored service state: {unit}")
        expected_active = state["active"]
        expected_enabled = state["enabled"]
        self_recovery = (
            defer_starts
            and unit == "aicc-principal-recovery.service"
            and active == "active"
            and main_pid == str(os.getpid())
        )
        active_matches = (active == "active") is expected_active
        if self_recovery and not expected_active:
            # This oneshot cannot synchronously transition itself to inactive.
            # WAL remains until it returns; its restored file/enablement state
            # is authoritative for the next activation.
            active_matches = True
        if queued_start and expected_active:
            # Boot recovery is ordered before sysinit. A synchronous start of
            # a normal worker would deadlock on its dependency back to this
            # oneshot. A successfully queued --no-block job may still be
            # inactive until the recovery barrier exits successfully.
            active_matches = active in {"inactive", "activating", "active"}
        enabled_matches = (enabled == "enabled") is expected_enabled
        if state["exists"]:
            exists_matches = load_state not in {"", "not-found"}
        else:
            # The early-boot recovery process cannot synchronously stop
            # itself. It may remain loaded/active only when systemd proves
            # that this exact process is the service MainPID; its enablement
            # and every file target have already been rolled back durably.
            exists_matches = load_state in {"", "not-found"} or self_recovery
            active_matches = active != "active" or self_recovery
            enabled_matches = enabled != "enabled"
        if not (exists_matches and active_matches and enabled_matches):
            raise RuntimeError(f"service snapshot did not restore exactly: {unit}")
        if active != "active" and main_pid not in {"", "0"}:
            raise RuntimeError(f"inactive restored service retains MainPID: {unit}")
        if version == 3 and state["exists"] and not self_recovery:
            properties = state["properties"]
            for name, expected in properties.items():
                property_rc, actual = probe(
                    "show", unit, f"--property={name}", "--value"
                )
                if property_rc or _normalise_property(actual) != _normalise_property(
                    expected
                ):
                    raise RuntimeError(
                        f"service snapshot property did not restore: {unit} {name}"
                    )

    systemctl("daemon-reload")
    for unit, state in validated:
        _pid_rc, current_pid = probe(
            "show", unit, "--property=MainPID", "--value"
        )
        self_recovery = (
            defer_starts
            and unit == "aicc-principal-recovery.service"
            and current_pid == str(os.getpid())
        )
        if state["exists"] is False:
            # Best-effort mutations are followed by authoritative state
            # probes. A failed command is harmless only when the desired
            # state is nevertheless proven; otherwise recover() keeps WAL
            # and the service snapshot for the next retry.
            if not self_recovery:
                probe("stop", unit)
            probe("disable", unit)
            assert_restored(unit, state)
            continue
        if version == 3 and not self_recovery:
            for name, expected in state["properties"].items():
                property_rc, actual = probe(
                    "show", unit, f"--property={name}", "--value"
                )
                if property_rc or actual != expected:
                    raise RuntimeError(
                        f"refusing unsafe snapshot restart: {unit} {name}"
                    )
        systemctl("enable" if state["enabled"] else "disable", unit)
        queued_start = defer_starts and state["active"] and not self_recovery
        if not self_recovery:
            if queued_start:
                systemctl("--no-block", "start", unit)
            else:
                systemctl("start" if state["active"] else "stop", unit)
        assert_restored(unit, state, queued_start=queued_start)


def quiesce_service_snapshot(path: Path, *, run=subprocess.run) -> None:
    """Stop snapshotted admission/worker units before rollback mutation."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = payload["version"]
        units = payload["units"]
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("missing or invalid service snapshot for quiesce") from exc
    if version not in {2, 3} or not isinstance(units, dict):
        raise RuntimeError("invalid service snapshot for quiesce")
    validated: list[tuple[str, dict[str, object]]] = []
    for unit, state in sorted(units.items()):
        if (
            not isinstance(unit, str)
            or not RESTORABLE_UNIT_RE.fullmatch(unit)
            or not isinstance(state, dict)
            or not isinstance(state.get("exists"), bool)
            or not isinstance(state.get("enabled"), bool)
            or not isinstance(state.get("active"), bool)
            or (
                version == 3
                and (
                    not isinstance(state.get("properties"), dict)
                    or (
                        state["exists"]
                        and set(state["properties"]) != set(SNAPSHOT_PROPERTIES)
                    )
                    or any(
                        not isinstance(name, str) or not isinstance(value, str)
                        for name, value in state["properties"].items()
                    )
                )
            )
        ):
            raise RuntimeError("invalid service snapshot unit for quiesce")
        if unit != "aicc-principal-recovery.service":
            validated.append((unit, state))

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["/usr/bin/systemctl", *arguments],
            capture_output=True,
            check=False,
            text=True,
        )

    for unit, expected in validated:
        load = command("show", unit, "--property=LoadState", "--value")
        load_state = load.stdout.strip()
        if load_state == "not-found":
            if (
                expected["exists"] is False
                and expected["active"] is False
                and expected["enabled"] is False
            ):
                continue
            raise RuntimeError(f"expected unit disappeared before quiesce: {unit}")
        if load.returncode or not load_state:
            raise RuntimeError(f"cannot prove unit load state before quiesce: {unit}")
        stopped = command("stop", unit)
        if stopped.returncode:
            raise RuntimeError(f"cannot stop unit before rollback: {unit}")
        active = command("show", unit, "--property=ActiveState", "--value")
        main_pid = command("show", unit, "--property=MainPID", "--value")
        if (
            active.returncode
            or main_pid.returncode
            or active.stdout.strip() != "inactive"
            or main_pid.stdout.strip() not in {"", "0"}
        ):
            raise RuntimeError(f"unit did not quiesce exactly: {unit}")


def verify_service_snapshot_closure(
    path: Path, *, run=subprocess.run
) -> None:
    """Prove every installed or loaded template lane is in the bound snapshot.

    This check intentionally lives in the recovery capsule rather than only in
    the installer shell. A boot/runtime resume must not trust a point-in-time
    pre-crash audit before it removes or switches executable code.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = payload["version"]
        units = payload["units"]
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("missing or invalid service snapshot for closure") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "units"}
        or version not in {2, 3}
        or not isinstance(units, dict)
        or any(
            not isinstance(unit, str) or not RESTORABLE_UNIT_RE.fullmatch(unit)
            for unit in units
        )
    ):
        raise RuntimeError("invalid service snapshot for closure")

    discovered: set[str] = set()
    for arguments in (
        (
            "list-unit-files",
            "voyn-aicc-worker@*.service",
            "--no-legend",
            "--no-pager",
        ),
        (
            "list-units",
            "voyn-aicc-worker@*.service",
            "--all",
            "--no-legend",
            "--no-pager",
        ),
    ):
        result = run(
            ["/usr/bin/systemctl", *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        # `list-unit-files` exits 1 when a pattern matches nothing, and on a
        # control-plane host it matches nothing by design: that profile
        # installs no worker lanes at all. An empty result is the answer, not
        # a failure -- treating it as one made the control install die at
        # `cannot enumerate worker lanes for snapshot closure` (observed live
        # on control-01, 2026-08-31). A real failure still reports on stderr.
        if result.returncode and (result.stderr.strip() or result.stdout.strip()):
            raise RuntimeError(
                result.stderr.strip()
                or "cannot enumerate worker lanes for snapshot closure"
            )
        for line in result.stdout.splitlines():
            fields = line.split()
            if fields and fields[0] == "●":
                fields = fields[1:]
            candidate = fields[0] if fields else ""
            if TEMPLATE_WORKER_UNIT_RE.fullmatch(candidate):
                discovered.add(candidate)

    extras = sorted(discovered - set(units))
    if extras:
        raise RuntimeError(f"worker lanes exist outside service snapshot: {extras}")


class FileTransaction:
    """Install a complete file set or restore its exact pre-install state."""

    def __init__(self, root: Path, state_dir: Path):
        self.root = root.resolve()
        self.state_dir = state_dir.resolve()
        self.current = self.state_dir / "current.json"
        self.pending = self.state_dir / "pending.json"
        self.pending_release = self.state_dir / "pending-release"

    def _write_journal(self, manifest: Path, phase: str, next_index: int = 0) -> None:
        recovery = manifest.parent / "recovery.py"
        _atomic_bytes(
            self.pending,
            json.dumps(
                {
                    "version": 1,
                    "manifest": str(manifest),
                    "recovery": str(recovery),
                    "phase": phase,
                    "next_index": next_index,
                },
                sort_keys=True,
            ).encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )

    def _target(self, absolute: str) -> Path:
        if not absolute.startswith("/") or ".." in Path(absolute).parts:
            raise ValueError(f"unsafe installation target: {absolute}")
        # lstrip, not removeprefix: "//etc/passwd" minus ONE slash is still
        # absolute, and joining an absolute path onto self.root discards the
        # root entirely (PurePath.__truediv__) -- a silent sandbox escape
        # (independent-review finding on d661d8f).
        relative = absolute.lstrip("/")
        if not relative:
            raise ValueError(f"unsafe installation target: {absolute}")
        return self.root / relative

    def _validate_parent_chain(self, target: Path) -> None:
        current = target.parent
        while current != self.root:
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                continue
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError(
                    f"installation parent is not a real directory: {current}"
                )
            current = current.parent

    def _prepare_state_dir(self) -> None:
        try:
            info = self.state_dir.lstat()
        except FileNotFoundError:
            self.state_dir.mkdir(parents=True, mode=0o700)
            info = self.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("transaction state directory is not private and owned")

    @staticmethod
    def validate_sources(specs: Iterable[FileSpec]) -> tuple[FileSpec, ...]:
        validated = tuple(specs)
        targets: set[str] = set()
        for spec in validated:
            try:
                _read_regular(spec.source)
            except (OSError, RuntimeError) as exc:
                raise ValueError(
                    f"source is not a safe regular file: {spec.source}"
                ) from exc
            if spec.target in targets:
                raise ValueError(f"duplicate installation target: {spec.target}")
            targets.add(spec.target)
        return validated

    def prepare(self, specs: Iterable[FileSpec]) -> Path:
        """Durably journal backups and staged payloads without touching targets."""
        validated = self.validate_sources(specs)
        if _path_present(self.pending_release):
            raise RuntimeError(
                "pending release selector blocks a new install transaction"
            )
        source_states = {spec.target: _read_regular(spec.source) for spec in validated}
        previous_current = (
            self.current.read_text(encoding="utf-8") if self.current.exists() else None
        )
        # Target shape is part of preflight. Do not create the state directory
        # or a destination parent until every existing target is proven safe.
        for spec in validated:
            target = self._target(spec.target)
            self._validate_parent_chain(target)
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            if spec.if_missing:
                continue
            if stat.S_ISLNK(info.st_mode):
                # A symlink is the shape every legacy unit still has on these
                # hosts: /etc/systemd/system/<unit> pointing into the operator's
                # home. Replacing it with the repository's own root-owned file
                # is the entire point of taking these units under repository
                # ownership, so it is recorded and replaced rather than
                # refused. Nothing follows the link: the backup stores the
                # link's literal target, and the install renames a staged file
                # over the link itself. Found live on the first worker install.
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"existing target is not a regular file: {target}")
        self._prepare_state_dir()
        if _path_present(self.pending) or _path_present(self.pending_release):
            # A prior install was interrupted after prepare/apply but before
            # commit. Overwriting pending.json here would orphan that
            # generation's backups and permanently destroy the restore path
            # (review finding on 363e91d). The operator must run recover
            # first; this transaction refuses rather than clobbers.
            raise RuntimeError(
                "a pending install transaction already exists; run recover "
                "before installing again"
            )
        transaction = self.state_dir / f"generation-{secrets.token_hex(8)}"
        backups = transaction / "backups"
        staged = transaction / "staged"
        backups.mkdir(parents=True, mode=0o700)
        staged.mkdir(mode=0o700)
        records: list[BackupRecord] = []

        # Snapshot every target before the first mutation.
        try:
            _atomic_bytes(
                transaction / "recovery.py",
                Path(__file__).read_bytes(),
                0o700,
                os.geteuid(),
                os.getegid(),
            )
            for index, spec in enumerate(validated):
                target = self._target(spec.target)
                if spec.if_missing and target.exists():
                    continue
                staged_path = staged / f"{index:03d}.bin"
                _atomic_bytes(
                    staged_path,
                    source_states[spec.target].payload,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                )
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    records.append(
                        BackupRecord(
                            spec.target,
                            False,
                            None,
                            None,
                            None,
                            None,
                            None,
                            str(staged_path),
                            source_states[spec.target].sha256,
                            spec.mode,
                            spec.uid,
                            spec.gid,
                        )
                    )
                    continue
                if stat.S_ISLNK(info.st_mode):
                    records.append(
                        BackupRecord(
                            spec.target,
                            True,
                            None,
                            None,
                            None,
                            None,
                            None,
                            str(staged_path),
                            source_states[spec.target].sha256,
                            spec.mode,
                            spec.uid,
                            spec.gid,
                            os.readlink(target),
                        )
                    )
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"existing target is not a regular file: {target}")
                original = _read_regular(target)
                backup = backups / f"{index:03d}.bin"
                _atomic_bytes(
                    backup,
                    original.payload,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                )
                records.append(
                    BackupRecord(
                        spec.target,
                        True,
                        str(backup),
                        original.mode,
                        original.uid,
                        original.gid,
                        original.sha256,
                        str(staged_path),
                        source_states[spec.target].sha256,
                        spec.mode,
                        spec.uid,
                        spec.gid,
                    )
                )
        except BaseException:
            shutil.rmtree(transaction, ignore_errors=True)
            _fsync_dir(self.state_dir)
            raise

        manifest = transaction / "manifest.json"
        try:
            _atomic_bytes(
                manifest,
                json.dumps(
                    {
                        "version": 2,
                        "generation": transaction.name,
                        "records": [asdict(record) for record in records],
                        "previous_current": previous_current,
                    },
                    sort_keys=True,
                ).encode(),
                0o600,
                os.geteuid(),
                os.getegid(),
            )
            _fsync_dir(transaction)
            # Durable PREPARED intent is the only gateway to target writes.
            self._write_journal(manifest, "PREPARED")
        except BaseException:
            shutil.rmtree(transaction, ignore_errors=True)
            _fsync_dir(self.state_dir)
            raise
        return manifest

    def _pending_manifest(self) -> Path:
        value = _trusted_journal(self.pending)
        manifest = Path(value["manifest"]).resolve(strict=True)
        if (
            not manifest.is_relative_to(self.state_dir)
            or manifest.name != "manifest.json"
        ):
            raise RuntimeError("pending transaction manifest escaped state directory")
        return manifest

    def apply(self) -> None:
        """Apply one prepared generation; recovery stays armed until commit."""
        manifest = self._pending_manifest()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = [BackupRecord(**value) for value in payload["records"]]
        try:
            for index, record in enumerate(records):
                # Write-ahead index makes every destination mutation recoverable.
                self._write_journal(manifest, "APPLYING", index)
                staged = _read_regular(Path(record.staged))
                if not _matches(
                    staged,
                    record.install_sha256,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                ):
                    raise RuntimeError("staged generation payload SHA drifted")
                _atomic_bytes(
                    self._target(record.target),
                    staged.payload,
                    record.install_mode,
                    record.install_uid,
                    record.install_gid,
                )
            self._write_journal(manifest, "APPLIED", len(records))
        except BaseException:
            self.restore(manifest)
            shutil.rmtree(manifest.parent)
            _fsync_dir(self.state_dir)
            raise

    def commit(self) -> None:
        """Publish an applied generation only after service rollout succeeds."""
        manifest = self._pending_manifest()
        journal = _trusted_journal(self.pending)
        if journal.get("phase") != "APPLIED":
            raise RuntimeError("only a fully applied generation can be committed")
        self._write_journal(manifest, "COMMITTING", journal.get("next_index", 0))
        _atomic_bytes(
            self.current,
            json.dumps({"manifest": str(manifest)}, sort_keys=True).encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
        self.pending_release.unlink(missing_ok=True)
        _fsync_dir(self.state_dir)
        self.pending.unlink()
        # The snapshot is spent once committed; leaving it at the fixed path
        # lets a later recover() apply a stale snapshot against a different
        # generation (review on d8920b6).
        (self.state_dir / "attempt-units.json").unlink(missing_ok=True)
        _fsync_dir(self.state_dir)

    def _restore_release_selector(self, *, clear_pending: bool = True) -> None:
        if not _path_present(self.pending_release):
            return
        info = self.pending_release.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 64
        ):
            raise RuntimeError("pending release selector drifted")
        selector = self.pending_release.read_text(encoding="ascii").strip()
        if selector != "ABSENT" and not re.fullmatch(
            r"releases/[0-9a-f]{40}", selector
        ):
            raise RuntimeError("pending release selector is invalid")
        current = self._target("/opt/aicc/current")
        current.parent.mkdir(parents=True, exist_ok=True)
        if selector == "ABSENT":
            if current.is_symlink():
                current.unlink()
            elif current.exists():
                raise RuntimeError("current release selector is not a symlink")
        else:
            # Repointing the LIVE selector at a release that does not exist
            # would leave every worker ExecStart dereferencing a missing
            # directory (review on 6e22b93): the target must exist first.
            release_target = current.parent / selector
            if not release_target.is_dir():
                raise RuntimeError(
                    "pending release selector points at a missing release"
                )
            # Selecting a release is selecting the code every worker ExecStart
            # runs, and recovery is not a weaker moment than install: a prior
            # generation can have been replaced, truncated or drifted since it
            # was built. Independent review on cacfc257 found this path
            # admitting exactly the unattested release the forward path now
            # refuses. Fail secure -- an unproven release is never selected,
            # even to restore service.
            self.verify_release_selection(selector)
            temporary = current.parent / f".current-recover-{os.getpid()}"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(selector)
            os.replace(temporary, current)
        _fsync_dir(current.parent)
        if clear_pending:
            self.pending_release.unlink()
            _fsync_dir(self.state_dir)

    def release_manifest_path(self, release_id: str) -> Path:
        """The root-owned manifest recorded when this release was staged."""
        return self.state_dir / "releases" / f"{release_id}.json"

    def verify_release_selection(self, selector: str) -> None:
        """Prove a release before `/opt/aicc/current` may point at it.

        Every selection goes through here -- install, recovery, rollback and
        uninstall alike. The Git cross-check is not available on the boot
        recovery path (there is no repository checkout at that point), so the
        root-owned manifest is the authority there; the forward path adds the
        committed-tree comparison on top of it.
        """
        release_id = selector.split("/", 1)[-1]
        release_dir = self._target("/opt/aicc") / selector
        verify_release_manifest(
            release_dir,
            self.release_manifest_path(release_id),
            release_id,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )

    def select_release(self, release_id: str, repo_root: Path) -> str:
        """Arm rollback and atomically activate an exact, re-proven release."""
        if RELEASE_ID_RE.fullmatch(release_id) is None:
            raise ReleaseRefused(
                "release id must be exactly 40 lowercase hex characters"
            )
        selector = f"releases/{release_id}"
        release_dir = self._target("/opt/aicc") / selector
        manifest = self.release_manifest_path(release_id)
        if not _path_present(self.pending):
            raise RuntimeError("release selection requires an active install journal")
        install_journal = _trusted_journal(self.pending)
        if install_journal.get("phase") != "APPLIED":
            raise RuntimeError("release selection requires an applied install")
        if _path_present(self.pending_release):
            raise RuntimeError("pending release selector already exists")
        current = self._target("/opt/aicc/current")
        previous = _release_selector(current)
        _exclusive_bytes(
            self.pending_release,
            f"{previous}\n".encode("ascii"),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
        # Re-prove immediately before the single selector mutation. The
        # manifest was created before publication and Git is the independent
        # authority for the exact tree selected here.
        verify_release_manifest(
            release_dir,
            manifest,
            release_id,
            repo_root=repo_root,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )
        parent_fd = _open_directory_chain(current.parent, create=False)
        temporary = f".current-select-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            os.symlink(selector, temporary, dir_fd=parent_fd)
            os.replace(
                temporary,
                current.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        return previous

    def select_uninstall_baseline(self, selector: str) -> None:
        """Restore the pre-install selector under the armed uninstall WAL."""
        if selector != "ABSENT" and not re.fullmatch(
            r"releases/[0-9a-f]{40}", selector
        ):
            raise RuntimeError("baseline release selector is invalid")
        journal = _trusted_journal(self.state_dir / "uninstall.json")
        if journal.get("version") != UNINSTALL_JOURNAL_VERSION:
            raise RuntimeError("uninstall journal version is unsupported")
        _trusted_uninstall_recovery(self.state_dir, journal)
        if (
            journal.get("phase") != "ARMED"
            or journal.get("baseline_selector") != selector
        ):
            raise RuntimeError("uninstall baseline does not match armed journal")
        if selector != "ABSENT":
            self.verify_release_selection(selector)
        current = self._target("/opt/aicc/current")
        parent_fd = _open_directory_chain(current.parent, create=False)
        temporary = f".current-uninstall-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            if selector == "ABSENT":
                try:
                    os.unlink(current.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            else:
                os.symlink(selector, temporary, dir_fd=parent_fd)
                os.replace(
                    temporary,
                    current.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def install(self, specs: Iterable[FileSpec]) -> None:
        self.prepare(specs)
        self.apply()
        self.commit()

    def recover(self, *, boot: bool = False) -> None:
        """Idempotently roll back an interrupted prepared/applying generation."""
        if not _path_present(self.pending):
            if _path_present(self.pending_release):
                raise RuntimeError(
                    "pending release selector exists without install journal"
                )
            # A crash after a completed recovery can leave only the inert
            # fixed-name snapshot. With no governing WAL it has no authority
            # and must not bleed into the next transaction.
            snapshot = self.state_dir / "attempt-units.json"
            if _path_present(snapshot):
                _read_regular(snapshot, max_bytes=4 * 1024 * 1024)
                snapshot.unlink()
                _fsync_dir(self.state_dir)
            self._remove_orphan_generations()
            return
        manifest = self._pending_manifest()
        transaction = manifest.parent
        journal = _trusted_journal(self.pending)
        pending_release_present = _path_present(self.pending_release)
        if pending_release_present and journal.get("phase") not in {
            "APPLIED",
            "COMMITTING",
        }:
            raise RuntimeError(
                "pending release selector is not paired with an applied install"
            )
        current_manifest = None
        if self.current.exists():
            current_manifest = json.loads(self.current.read_text(encoding="utf-8")).get(
                "manifest"
            )
        if journal.get("phase") == "COMMITTING" or current_manifest == str(manifest):
            # commit() already durably published current.json but crashed
            # before unlinking pending.json. Restoring here would silently
            # revert a completed, live installation on the next boot
            # (independent-review finding on 8a881d3): finish the commit
            # instead of undoing it.
            _atomic_bytes(
                self.current,
                json.dumps({"manifest": str(manifest)}, sort_keys=True).encode(),
                0o600,
                os.geteuid(),
                os.getegid(),
            )
            self.pending_release.unlink(missing_ok=True)
            _fsync_dir(self.state_dir)
            self.pending.unlink()
            # The interrupted commit's service snapshot is spent: leaving it
            # at the fixed path lets a LATER recover() apply it against a
            # different generation (review on 0f4d77e).
            (self.state_dir / "attempt-units.json").unlink(missing_ok=True)
            self._remove_orphan_generations()
            _fsync_dir(self.state_dir)
            return
        snapshot = self.state_dir / "attempt-units.json"
        snapshot_present = _path_present(snapshot)
        if snapshot_present:
            verify_service_snapshot_closure(snapshot)
            quiesce_service_snapshot(snapshot)
            verify_service_snapshot_closure(snapshot)
        elif self.root == Path("/"):
            raise RuntimeError("production recovery requires a service snapshot")
        self.restore(manifest, clear_pending=False)
        self._restore_release_selector()
        if snapshot_present:
            if boot:
                restore_service_snapshot(snapshot, defer_starts=True)
            else:
                restore_service_snapshot(snapshot)
            verify_service_snapshot_closure(snapshot)
        self._clear_pending(manifest)
        if snapshot_present:
            snapshot.unlink()
            _fsync_dir(self.state_dir)
        shutil.rmtree(transaction)
        self._remove_orphan_generations()
        _fsync_dir(self.state_dir)

    def _current_generation_manifests(self) -> set[Path]:
        manifests: set[Path] = set()
        pointer = self.current.read_bytes() if self.current.exists() else None
        while pointer:
            value = json.loads(pointer)
            manifest = Path(value["manifest"]).resolve(strict=True)
            if not manifest.is_relative_to(self.state_dir) or manifest in manifests:
                raise RuntimeError("installed generation chain is invalid")
            manifests.add(manifest)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            previous = payload.get("previous_current")
            pointer = previous.encode() if previous else None
        return manifests

    def _remove_orphan_generations(self) -> None:
        if not self.state_dir.exists():
            return
        retained = {path.parent for path in self._current_generation_manifests()}
        for generation in self.state_dir.glob("generation-*"):
            if generation not in retained:
                shutil.rmtree(generation)

    def _clear_pending(self, manifest: Path) -> None:
        if not _path_present(self.pending):
            return
        pending = _trusted_journal(self.pending)
        if Path(pending.get("manifest", "")).resolve() == manifest.resolve():
            self.pending.unlink()
            _fsync_dir(self.state_dir)

    def restore(
        self, manifest: Path | None = None, *, clear_pending: bool = True
    ) -> None:
        if manifest is None:
            current = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(current["manifest"])
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = [BackupRecord(**value) for value in payload["records"]]
        for record in reversed(records):
            if (
                record.target == RECOVERY_ANCHOR_TARGET
                and _path_present(self.state_dir / "uninstall.json")
            ):
                # Historical generations treated the generator as reversible.
                # Preserve the permanent anchor until the uninstall WAL is
                # durably gone; without it a reboot could bypass recovery.
                continue
            target = self._target(record.target)
            if record.existed and record.original_symlink is not None:
                # The target was a symlink this generation replaced. Restore the
                # link, not a file: leaving a regular file where a link belonged
                # would silently change what the unit resolves to.
                #
                # Handled BEFORE `_read_regular`: that call opens with
                # O_NOFOLLOW, so on a target that is still (or already again) a
                # symlink it raises, and the generic branch below would abort
                # recovery with "target shape changed" on a generation that is
                # simply not applied yet, or already rolled back. Recovery has
                # to be idempotent -- it runs at boot and can itself be
                # interrupted (independent review on 2b8826a).
                if target.is_symlink():
                    if os.readlink(target) == record.original_symlink:
                        continue
                    raise RuntimeError(
                        f"generation target is a different symlink before restore: "
                        f"{target}"
                    )
                try:
                    current = _read_regular(target)
                except FileNotFoundError as exc:
                    # Neither the installed file nor the link is there. Something
                    # outside this transaction removed it; recreating the link
                    # would paper over that, exactly as the regular-file branch
                    # refuses to (independent review on 2b8826a).
                    raise RuntimeError(
                        f"generation target disappeared: {target}"
                    ) from exc
                except OSError as exc:
                    raise RuntimeError(
                        f"generation target shape changed before restore: {target}"
                    ) from exc
                if not _matches(
                    current,
                    record.install_sha256,
                    record.install_mode,
                    record.install_uid,
                    record.install_gid,
                ):
                    raise RuntimeError(
                        f"generation target changed before compare-and-restore: {target}"
                    )
                parent_fd = _open_directory_chain(target.parent, create=False)
                try:
                    temporary = f".{target.name}.restore.{secrets.token_hex(8)}"
                    os.symlink(record.original_symlink, temporary, dir_fd=parent_fd)
                    try:
                        os.rename(
                            temporary, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
                        )
                    except OSError:
                        os.unlink(temporary, dir_fd=parent_fd)
                        raise
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
                continue
            # Every other record: read the target as the generic branches expect.
            try:
                current = _read_regular(target)
            except FileNotFoundError:
                current = None
            except OSError as exc:
                raise RuntimeError(
                    f"generation target shape changed before restore: {target}"
                ) from exc
            if record.existed:
                assert record.backup is not None
                assert record.original_mode is not None
                assert record.original_uid is not None
                assert record.original_gid is not None
                assert record.original_sha256 is not None
                if current is None:
                    raise RuntimeError(f"generation target disappeared: {target}")
                if _matches(
                    current,
                    record.original_sha256,
                    record.original_mode,
                    record.original_uid,
                    record.original_gid,
                ):
                    continue
                if not _matches(
                    current,
                    record.install_sha256,
                    record.install_mode,
                    record.install_uid,
                    record.install_gid,
                ):
                    raise RuntimeError(
                        f"generation target changed before compare-and-restore: {target}"
                    )
                backup = _read_regular(Path(record.backup))
                if not _matches(
                    backup,
                    record.original_sha256,
                    0o600,
                    os.geteuid(),
                    os.getegid(),
                ):
                    raise RuntimeError(f"generation backup SHA drifted: {target}")
                _atomic_bytes(
                    target,
                    backup.payload,
                    record.original_mode,
                    record.original_uid,
                    record.original_gid,
                )
            else:
                if current is None:
                    continue
                if not _matches(
                    current,
                    record.install_sha256,
                    record.install_mode,
                    record.install_uid,
                    record.install_gid,
                ):
                    raise RuntimeError(
                        f"new generation target changed before removal: {target}"
                    )
                target.unlink()
                _fsync_dir(target.parent)
        previous_current = payload.get("previous_current")
        if previous_current is None:
            try:
                self.current.unlink()
            except FileNotFoundError:
                pass
        else:
            _atomic_bytes(
                self.current,
                previous_current.encode(),
                0o600,
                os.geteuid(),
                os.getegid(),
            )
        if clear_pending:
            self._clear_pending(manifest)

    def uninstall_all(self, *, boot: bool = False) -> None:
        """Unwind every installed generation to the original pre-install state."""
        self.recover(boot=boot)
        while self.current.exists():
            value = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(value["manifest"]).resolve(strict=True)
            transaction = manifest.parent
            self.restore(manifest)
            shutil.rmtree(transaction)
            _fsync_dir(self.state_dir)
        self._remove_orphan_generations()


def recover_uninstall(
    state_dir: Path, *, root: Path = Path("/"), boot: bool = False
) -> None:
    """Resume or safely abort a journalled uninstall after a crash/reboot."""
    journal_path = state_dir / "uninstall.json"
    payload = _trusted_journal(journal_path)
    if payload.get("version") != UNINSTALL_JOURNAL_VERSION:
        raise RuntimeError("uninstall recovery journal version is unsupported")
    recovery = _trusted_uninstall_recovery(state_dir, payload)
    phase = payload.get("phase")
    snapshot = state_dir / "uninstall-units.json"

    if phase == "INTENT":
        # No privileged uninstall mutation is permitted before ARMED. Prove
        # the installation identity is unchanged, then abort the intent with
        # the journal removed last. A partially written snapshot is inert.
        current = _release_selector(root / "opt/aicc/current")
        if current != payload.get("start_selector"):
            raise RuntimeError("release selector changed during uninstall intent")
        registry = _read_regular(root / "etc/aicc/worker-lanes", max_bytes=64 * 1024)
        if registry.sha256 != payload.get("registry_sha256"):
            raise RuntimeError("worker lane registry changed during uninstall intent")
        snapshot.unlink(missing_ok=True)
        _fsync_dir(state_dir)
        journal_path.unlink()
        _fsync_dir(state_dir)
        try:
            recovery.unlink(missing_ok=True)
            recovery.parent.rmdir()
            _fsync_dir(state_dir)
        except OSError:
            pass
        return

    if phase == "COMPLETING":
        complete_uninstall(state_dir, snapshot)
        return
    if phase != "ARMED":
        raise RuntimeError("uninstall recovery phase is invalid")

    bound_snapshot = _read_regular(snapshot, max_bytes=4 * 1024 * 1024)
    if bound_snapshot.sha256 != payload.get("snapshot_sha256"):
        raise RuntimeError("uninstall service snapshot drifted")
    verify_service_snapshot_closure(snapshot)
    quiesce_service_snapshot(snapshot)
    verify_service_snapshot_closure(snapshot)
    transaction = FileTransaction(root, state_dir)
    transaction.uninstall_all(boot=boot)
    baseline = payload.get("baseline_selector")
    if not isinstance(baseline, str):
        raise TypeError("uninstall baseline selector is invalid")
    transaction.select_uninstall_baseline(baseline)
    if boot:
        restore_service_snapshot(
            state_dir / "baseline-units.json", defer_starts=True
        )
    else:
        restore_service_snapshot(state_dir / "baseline-units.json")
    verify_service_snapshot_closure(snapshot)
    complete_uninstall(state_dir, snapshot)


# Kept identical to `GIT_CONFIG_FREE` in ops/aicc_exact_sha_bootstrap.py; the
# bootstrap runs as a standalone blob before this module exists, so the list is
# duplicated deliberately and pinned by tests/ops/test_aicc_release_manifest.py.
GIT_CONFIG_FREE = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.pager=cat",
    "-c",
    "core.sshCommand=/bin/false",
    "-c",
    "core.gitProxy=",
    "-c",
    "core.symlinks=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "credential.helper=",
    "-c",
    "diff.external=",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.clean=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "uploadpack.packObjectsHook=",
)


def _git_safe_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


RELEASE_MANIFEST_VERSION = 1
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{40}$")
# A content-addressed artifact identifies itself by its own sha256 rather than
# by a Git commit. The publication machinery is otherwise identical -- same
# ownership, mode, digest and symlink-target proof, same atomic rename, same
# crash reconciliation -- so it takes the identity pattern as a parameter
# instead of growing a second copy of itself
# (VOYN-W0-AICC-TOOLCHAIN-CONTENT-ADDRESSED). Widening RELEASE_ID_RE itself
# would weaken the Git path, which must keep refusing a 64-hex value that is
# not a commit.
ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_TREE_ENTRY_RE = re.compile(
    rb"([0-7]{6}) (blob|commit) ([0-9a-f]{40})\t(.+)", re.DOTALL
)


class ReleaseRefused(RuntimeError):
    """A staged or pre-existing immutable release could not be proven."""


def _git_blob_oid(blob_bytes: bytes) -> str:
    """Ask trusted Git for the repository-format identity of raw blob bytes."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "--no-replace-objects",
                *GIT_CONFIG_FREE,
                "hash-object",
                "--no-filters",
                "--stdin",
            ],
            cwd=Path("/"),
            env=_git_safe_environment(),
            input=blob_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ReleaseRefused("cannot execute trusted Git blob identity") from exc
    if result.returncode or result.stderr:
        raise ReleaseRefused("trusted Git blob identity failed")
    if re.fullmatch(rb"[0-9a-f]{40}\n", result.stdout) is None:
        raise ReleaseRefused("trusted Git returned an invalid blob identity")
    return result.stdout[:-1].decode("ascii")


def _release_entry(
    path: Path, relative: str, *, trusted_uid: int, trusted_gid: int
) -> dict[str, object]:
    """Describe one release path from its own `lstat`, refusing anything that
    a non-root principal could still influence.

    Ownership and the absence of group/other write authority are checked here
    rather than only against the recorded manifest: a manifest that faithfully
    records a world-writable tree must not make that tree acceptable.
    """
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != trusted_uid or info.st_gid != trusted_gid:
        raise ReleaseRefused(f"release path is not trusted-owned: {relative}")
    if mode & 0o022 and not stat.S_ISLNK(info.st_mode):
        raise ReleaseRefused(f"release path is group/world writable: {relative}")
    if stat.S_ISLNK(info.st_mode):
        # A symlink is legitimate inside the interpreter venv, but only as the
        # exact link recorded when root built the release. The target is data,
        # never followed during verification.
        return {
            "path": relative,
            "kind": "symlink",
            "mode": mode,
            "target": os.readlink(path),
        }
    if stat.S_ISDIR(info.st_mode):
        return {"path": relative, "kind": "dir", "mode": mode}
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseRefused(f"release path is not a regular file: {relative}")
    if info.st_nlink != 1:
        # A hardlink lets a writer of any other directory keep a mutable alias
        # to release content that `chmod -R a-w` appears to have frozen.
        raise ReleaseRefused(f"release file is hardlinked: {relative}")
    # Streamed rather than read whole: a release tree carries native binaries
    # of a few hundred megabytes, and holding each one in memory to hash it is
    # both wasteful and, at `_read_regular`'s 128 MB bound, a hard refusal.
    digest, size, mode, uid, gid = _digest_regular(path)
    if uid != trusted_uid or gid != trusted_gid:
        raise ReleaseRefused(f"release file ownership changed while read: {relative}")
    return {
        "path": relative,
        "kind": "file",
        "mode": mode,
        "sha256": digest,
        "size": size,
    }


def release_entries(
    root: Path, *, trusted_uid: int = 0, trusted_gid: int = 0
) -> list[dict[str, object]]:
    """Every path below `root`, deterministically ordered.

    `os.walk(followlinks=False)` never descends a symlink, so a symlinked
    directory is recorded as a link and its target tree is not absorbed into
    the release identity.
    """
    entries = [
        _release_entry(root, ".", trusted_uid=trusted_uid, trusted_gid=trusted_gid)
    ]
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted([*names, *files]):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            entries.append(
                _release_entry(
                    path, relative, trusted_uid=trusted_uid, trusted_gid=trusted_gid
                )
            )
    entries.sort(key=lambda entry: entry["path"])
    seen = {entry["path"] for entry in entries}
    if len(seen) != len(entries):
        raise ReleaseRefused("duplicate release path")
    return entries


def _manifest_document(release_id: str, entries: list[dict[str, object]]) -> bytes:
    body = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document = {
        "version": RELEASE_MANIFEST_VERSION,
        "release_id": release_id,
        "entry_count": len(entries),
        "entries_sha256": hashlib.sha256(body).hexdigest(),
        "entries": entries,
    }
    return (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")


def _git_tree_blobs(repo_root: Path, release_id: str) -> dict[str, tuple[str, int]]:
    """`path -> (blob oid, mode)` for the exact commit, read config-free.

    This is what makes a same-name/wrong-tree release detectable even if its
    manifest was rewritten: the committed content is the independent authority,
    not the manifest.
    """
    result = subprocess.run(
        [
            "/usr/bin/git",
            # This read is the INDEPENDENT authority a release is checked
            # against, so it must not inherit anything the release directory's
            # own repository can influence. Replacement refs would otherwise
            # let one planted `refs/replace/<release_id>` satisfy both the
            # manifest and this check with the same substituted tree
            # (independent review on aaf1a502). Kept in lockstep with
            # `_git_argv` in ops/aicc_exact_sha_bootstrap.py -- that module is
            # extracted from Git as a single blob and executed before this one
            # exists, so it cannot import from here; a fitness test pins the
            # two lists together instead.
            "--no-replace-objects",
            *GIT_CONFIG_FREE,
            "-C",
            str(repo_root),
            "ls-tree",
            "-rz",
            "--full-tree",
            release_id,
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        env=_git_safe_environment(),
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseRefused(detail or "cannot read trusted Git tree")
    blobs: dict[str, tuple[str, int]] = {}
    for raw in result.stdout.rstrip(b"\0").split(b"\0") if result.stdout else []:
        match = GIT_TREE_ENTRY_RE.fullmatch(raw)
        if match is None:
            raise ReleaseRefused("unparseable Git tree entry")
        mode_raw, kind, oid_raw, path_raw = match.groups()
        try:
            relative = path_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseRefused("non-UTF-8 path in trusted tree") from exc
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseRefused("unsafe path in trusted tree")
        if kind != b"blob" or mode_raw not in {b"100644", b"100755"}:
            raise ReleaseRefused(f"unsupported tree entry: {relative}")
        blobs[relative] = (
            oid_raw.decode("ascii"),
            0o755 if mode_raw == b"100755" else 0o644,
        )
    return blobs


def _verify_against_git_tree(
    release_dir: Path,
    repo_root: Path,
    release_id: str,
    entries: list[dict[str, object]],
    *,
    trusted_uid: int,
    trusted_gid: int,
) -> None:
    by_path = {entry["path"]: entry for entry in entries}
    for relative, (oid, git_mode) in _git_tree_blobs(repo_root, release_id).items():
        entry = by_path.get(relative)
        if entry is None or entry["kind"] != "file":
            raise ReleaseRefused(f"committed file missing from release: {relative}")
        target = release_dir / relative
        state = _read_regular(target)
        if state.uid != trusted_uid or state.gid != trusted_gid:
            raise ReleaseRefused(f"committed file is not trusted-owned: {relative}")
        if _git_blob_oid(state.payload) != oid:
            raise ReleaseRefused(
                f"release content does not match committed blob: {relative}"
            )
        # `chmod -R a-w` clears write bits; the executable bit is the part of
        # the committed mode a release must still carry faithfully.
        if bool(state.mode & 0o111) != bool(git_mode & 0o111):
            raise ReleaseRefused(f"release executable bit drifted: {relative}")


def record_release_manifest(
    release_tree: Path,
    manifest: Path,
    release_id: str,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    id_pattern: re.Pattern[str] = RELEASE_ID_RE,
) -> list[dict[str, object]]:
    """Write the root-owned content manifest for a freshly staged release.

    Recorded before the staging tree is renamed into place, so a release
    directory never exists without the manifest that authorises its reuse.
    """
    if id_pattern.fullmatch(release_id) is None:
        raise ReleaseRefused(
            f"release id does not match its identity pattern {id_pattern.pattern}"
        )
    entries = release_entries(
        release_tree, trusted_uid=trusted_uid, trusted_gid=trusted_gid
    )
    payload = _manifest_document(release_id, entries)
    directory_fd = _open_directory_chain(manifest.parent, create=True)
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if not hasattr(os, "O_NOFOLLOW"):
            raise ReleaseRefused("host lacks no-follow manifest support")
        flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                manifest.name, flags, 0o600, dir_fd=directory_fd
            )
        except FileExistsError:
            try:
                existing = _read_regular(manifest, max_bytes=64 * 1024 * 1024)
            except (OSError, RuntimeError) as exc:
                raise ReleaseRefused("existing release manifest is unsafe") from exc
            if (
                existing.uid != trusted_uid
                or existing.gid != trusted_gid
                or existing.mode != 0o600
                or existing.payload != payload
            ):
                raise ReleaseRefused("existing release manifest differs")
            return entries
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, trusted_uid, trusted_gid)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return entries


def publish_release_tree(
    staging: Path,
    release_root: Path,
    manifest: Path,
    release_id: str,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    id_pattern: re.Pattern[str] = RELEASE_ID_RE,
) -> Path:
    """Verify and publish one release atomically without replacing a name."""
    if id_pattern.fullmatch(release_id) is None:
        raise ReleaseRefused(
            f"release id does not match its identity pattern {id_pattern.pattern}"
        )
    if staging.parent != release_root or staging.name == release_id:
        raise ReleaseRefused("release staging path is outside the release root")
    # The publication primitive itself proves the authority it was given.
    # Callers cannot accidentally pass a manifest argument that is ignored.
    verify_release_manifest(
        staging,
        manifest,
        release_id,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        id_pattern=id_pattern,
    )
    root_fd = _open_directory_chain(release_root, create=False)
    try:
        root_state = os.fstat(root_fd)
        if (
            root_state.st_uid != trusted_uid
            or root_state.st_gid != trusted_gid
            or stat.S_IMODE(root_state.st_mode) & 0o022
        ):
            raise ReleaseRefused("release root is not trusted")
        staging_state = os.stat(
            staging.name, dir_fd=root_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(staging_state.st_mode)
            or staging_state.st_uid != trusted_uid
            or staging_state.st_gid != trusted_gid
        ):
            raise ReleaseRefused("release staging directory is not trusted")
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ReleaseRefused("kernel lacks atomic no-replace release publication")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        if renameat2(
            root_fd,
            os.fsencode(staging.name),
            root_fd,
            os.fsencode(release_id),
            1,  # RENAME_NOREPLACE
        ) != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise ReleaseRefused("release destination already exists")
            raise ReleaseRefused(
                f"atomic release publication failed: errno {error}"
            )
        os.fsync(root_fd)
        return release_root / release_id
    finally:
        os.close(root_fd)


def reconcile_release_publication(
    release_root: Path,
    manifest: Path,
    release_id: str,
    *,
    state_dir: Path = Path("/var/lib/aicc-principal-isolation"),
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    id_pattern: re.Pattern[str] = RELEASE_ID_RE,
) -> Path | None:
    """Recover every crash point around manifest + directory publication.

    The root-owned, non-writable release directory plus the host-global lock
    form the staging authority. A manifest with one exact staging directory is
    resumed; a manifest whose unpublished staging tree was already removed is
    safely discarded; an incomplete stage without a manifest is discarded and
    rebuilt. Ambiguous or untrusted state fails closed.
    """
    if id_pattern.fullmatch(release_id) is None:
        raise ReleaseRefused(
            f"release id does not match its identity pattern {id_pattern.pattern}"
        )
    if manifest.parent != state_dir / "releases":
        raise ReleaseRefused("release manifest path is outside trusted state")
    if manifest.name != f"{release_id}.json":
        raise ReleaseRefused("release manifest name does not match release id")
    root_fd = _open_directory_chain(release_root, create=False)
    try:
        root_state = os.fstat(root_fd)
        if (
            root_state.st_uid != trusted_uid
            or root_state.st_gid != trusted_gid
            or stat.S_IMODE(root_state.st_mode) & 0o022
        ):
            raise ReleaseRefused("release root is not trusted")
        prefix = f".stage-{release_id}."
        stages = sorted(
            release_root / entry.name
            for entry in os.scandir(root_fd)
            if entry.name.startswith(prefix)
        )
        destination = release_root / release_id
        destination_exists = destination.exists()
        if destination_exists:
            if stages:
                raise ReleaseRefused("published release coexists with staging state")
            verify_release_manifest(
                destination,
                manifest,
                release_id,
                trusted_uid=trusted_uid,
                trusted_gid=trusted_gid,
                id_pattern=id_pattern,
            )
            return destination
        if manifest.exists():
            if len(stages) > 1:
                raise ReleaseRefused("release publication staging is ambiguous")
            if len(stages) == 1:
                return publish_release_tree(
                    stages[0],
                    release_root,
                    manifest,
                    release_id,
                    trusted_uid=trusted_uid,
                    trusted_gid=trusted_gid,
                    id_pattern=id_pattern,
                )
            recorded = _read_regular(manifest, max_bytes=64 * 1024 * 1024)
            if (
                recorded.uid != trusted_uid
                or recorded.gid != trusted_gid
                or recorded.mode != 0o600
            ):
                raise ReleaseRefused("orphan release manifest is unsafe")
            manifest.unlink()
            _fsync_dir(manifest.parent)
            return None
        if len(stages) > 1:
            raise ReleaseRefused("unattested release staging is ambiguous")
        if stages:
            stage_info = stages[0].lstat()
            if (
                not stat.S_ISDIR(stage_info.st_mode)
                or stat.S_ISLNK(stage_info.st_mode)
                or stage_info.st_uid != trusted_uid
                or stage_info.st_gid != trusted_gid
                or stat.S_IMODE(stage_info.st_mode) & 0o022
            ):
                raise ReleaseRefused("unattested release staging is unsafe")
            shutil.rmtree(stages[0])
            os.fsync(root_fd)
        return None
    finally:
        os.close(root_fd)


def verify_release_manifest(
    release_dir: Path,
    manifest: Path,
    release_id: str,
    *,
    repo_root: Path | None = None,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
    id_pattern: re.Pattern[str] = RELEASE_ID_RE,
) -> list[dict[str, object]]:
    """Prove a pre-existing release directory before it may be selected.

    Refuses a missing manifest outright: an unattested `/opt/aicc/releases/<sha>`
    is exactly the case this gate exists for, and rebuilding trust from the
    directory itself would only re-record whatever an attacker left there.
    """
    if id_pattern.fullmatch(release_id) is None:
        raise ReleaseRefused(
            f"release id does not match its identity pattern {id_pattern.pattern}"
        )
    try:
        recorded = _read_regular(manifest)
    except (OSError, RuntimeError) as exc:
        raise ReleaseRefused(
            f"release manifest is missing or unsafe: {manifest}"
        ) from exc
    if (
        recorded.uid != trusted_uid
        or recorded.gid != trusted_gid
        or recorded.mode != 0o600
    ):
        raise ReleaseRefused("release manifest is not trusted-owned mode 0600")
    try:
        document = json.loads(recorded.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRefused("release manifest is malformed") from exc
    if not isinstance(document, dict):
        raise ReleaseRefused("release manifest is malformed")
    if document.get("version") != RELEASE_MANIFEST_VERSION:
        raise ReleaseRefused("unsupported release manifest version")
    if document.get("release_id") != release_id:
        raise ReleaseRefused("release manifest identity mismatch")
    expected = document.get("entries")
    if not isinstance(expected, list):
        raise ReleaseRefused("release manifest is malformed")
    body = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if document.get("entries_sha256") != hashlib.sha256(body).hexdigest():
        raise ReleaseRefused("release manifest content hash mismatch")
    if document.get("entry_count") != len(expected):
        raise ReleaseRefused("release manifest entry count mismatch")

    observed = release_entries(
        release_dir, trusted_uid=trusted_uid, trusted_gid=trusted_gid
    )
    expected_by_path = {entry["path"]: entry for entry in expected}
    observed_by_path = {entry["path"]: entry for entry in observed}
    missing = sorted(set(expected_by_path) - set(observed_by_path))
    if missing:
        raise ReleaseRefused(f"release is incomplete: {missing[:5]}")
    extra = sorted(set(observed_by_path) - set(expected_by_path))
    if extra:
        raise ReleaseRefused(f"release has unattested content: {extra[:5]}")
    for relative, entry in sorted(observed_by_path.items()):
        if entry != expected_by_path[relative]:
            raise ReleaseRefused(f"release path does not match manifest: {relative}")
    if repo_root is not None:
        _verify_against_git_tree(
            release_dir,
            repo_root,
            release_id,
            observed,
            trusted_uid=trusted_uid,
            trusted_gid=trusted_gid,
        )
    return observed


#: Targets that exist only because a host runs untrusted coding agents: the
#: launcher and its socket, the agent principal's sysusers/tmpfiles and env,
#: the worker lanes and units, the staged-rollout tool, and the agent model
#: credentials. A control-plane host runs none of them, and must not hold the
#: credentials in particular -- one secret on two hosts destroys exactly the
#: principal isolation this installer exists to create.
WORKER_ONLY_TARGETS = frozenset(
    {
        "/usr/lib/sysusers.d/aicc-agent.conf",
        "/usr/lib/tmpfiles.d/aicc-agent.conf",
        "/usr/libexec/aicc-agent-launcher",
        "/usr/libexec/aicc-staged-worker-rollout",
        "/etc/systemd/system/aicc-principal-recovery.service",
        "/etc/systemd/system/aicc-agent-launcher.socket",
        "/etc/systemd/system/aicc-agent-launcher@.service",
        "/etc/aicc/agent-workspace-roots",
        "/etc/aicc/worker-lanes",
        "/etc/aicc/agent.env",
        "/etc/systemd/system/voyn-aicc-worker@.service",
        "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf",
        "/etc/systemd/system/aicc-worker.service.d/20-principal-isolation.conf",
        "/var/lib/aicc-agent/claude/.claude/.credentials.json",
        "/var/lib/aicc-agent/codex/.codex/auth.json",
    }
)

PROFILES = ("worker", "control")


def default_specs(
    repo_root: Path,
    *,
    authority_env: Path,
    claude_auth: Path,
    codex_auth: Path,
    resolve_identities: bool = True,
    profile: str = "worker",
) -> tuple[FileSpec, ...]:
    """The files one host role installs.

    `worker` is every spec, unchanged -- the default, so an existing caller
    that knows nothing about profiles installs exactly what it always did.

    `control` drops `WORKER_ONLY_TARGETS`. Before this existed there was one
    profile for every host, and it demanded the agent's Claude and Codex
    credentials unconditionally: installing the control plane meant either
    placing agent secrets on a host that must never hold them, or not
    installing it at all. The live attempt on control-01 took the second
    branch and stopped at `source is not a safe regular file:
    /home/voynadmin/.claude/.credentials.json` -- a file whose *absence* was
    correct (2026-08-31).

    The control-plane's own units (planner, review, merge, reaper, rotation)
    are not added here: they are still symlinks into the operator's home and
    become repo-owned under VOYN-W0-AICC-CONTROL-PLANE-REPO-OWNED-UNITS. This
    profile makes that installation possible; it does not pre-empt it.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown installation profile: {profile!r}")
    root_uid, root_gid = 0, 0
    agent_gid = grp.getgrnam("aicc-agent").gr_gid if resolve_identities else 0
    publisher_gid = grp.getgrnam("aicc-publisher").gr_gid if resolve_identities else 0
    specs = (
        # The recovery generator is a permanent bootstrap anchor installed
        # atomically before prepare(), not part of reversible generations.
        FileSpec(
            repo_root / "ops/aicc_exact_sha_bootstrap.py",
            "/usr/local/sbin/voyn-aicc-bootstrap",
            0o755,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/sysusers.d/aicc-agent.conf",
            "/usr/lib/sysusers.d/aicc-agent.conf",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/tmpfiles.d/aicc-agent.conf",
            "/usr/lib/tmpfiles.d/aicc-agent.conf",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "ops/aicc_agent_launcher.py",
            "/usr/libexec/aicc-agent-launcher",
            0o755,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "ops/aicc_install_transaction.py",
            "/usr/libexec/aicc-install-transaction",
            0o755,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "ops/aicc_staged_worker_rollout.py",
            "/usr/libexec/aicc-staged-worker-rollout",
            0o755,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/systemd/aicc-principal-recovery.service",
            "/etc/systemd/system/aicc-principal-recovery.service",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/systemd/aicc-agent-launcher.socket",
            "/etc/systemd/system/aicc-agent-launcher.socket",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/systemd/aicc-agent-launcher@.service",
            "/etc/systemd/system/aicc-agent-launcher@.service",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/aicc/agent-workspace-roots",
            "/etc/aicc/agent-workspace-roots",
            0o644,
            root_uid,
            root_gid,
            True,
        ),
        FileSpec(
            repo_root / "deploy/aicc/worker-lanes",
            "/etc/aicc/worker-lanes",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/aicc/privileged-principals",
            "/etc/aicc/privileged-principals",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/aicc/agent.env",
            "/etc/aicc/agent.env",
            0o640,
            root_uid,
            agent_gid,
            True,
        ),
        FileSpec(
            repo_root / "deploy/aicc/publisher-secret-paths",
            "/etc/aicc/publisher-secret-paths",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/systemd/voyn-aicc-worker@.service",
            "/etc/systemd/system/voyn-aicc-worker@.service",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            repo_root / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf",
            "/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf",
            0o644,
            root_uid,
            root_gid,
        ),
        # The same fail-closed drop-in must reach the legacy single-lane unit
        # too -- asserting it only on the template family left
        # aicc-worker.service without a delivery path for the flag (review
        # finding on 63cb072).
        FileSpec(
            repo_root / "deploy/systemd/voyn-aicc-worker-principal-isolation.conf",
            "/etc/systemd/system/aicc-worker.service.d/20-principal-isolation.conf",
            0o644,
            root_uid,
            root_gid,
        ),
        FileSpec(
            authority_env,
            "/etc/aicc/workspace-authority.env",
            0o640,
            root_uid,
            publisher_gid,
        ),
        FileSpec(
            claude_auth,
            "/var/lib/aicc-agent/claude/.claude/.credentials.json",
            0o600,
            root_uid,
            root_gid,
        ),
        FileSpec(
            codex_auth,
            "/var/lib/aicc-agent/codex/.codex/auth.json",
            0o600,
            root_uid,
            root_gid,
        ),
    )
    if profile == "control":
        return tuple(spec for spec in specs if spec.target not in WORKER_ONLY_TARGETS)
    return specs


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.action == "recovery-anchor-install":
        if _path_present(args.state_dir / "uninstall.json"):
            raise RuntimeError("unfinished uninstall blocks recovery anchor update")
        install_recovery_anchor(
            args.repo_root / "ops/aicc_principal_recovery_generator.py",
            Path(RECOVERY_ANCHOR_TARGET),
        )
        return 0
    if args.action == "uninstall-status":
        _print_uninstall_phase(uninstall_phase(args.state_dir))
        return 0
    if args.action == "recover-uninstall-boot":
        recover_uninstall(args.state_dir, root=args.root, boot=True)
        return 0
    if args.action == "recover-uninstall-safe":
        # Runtime resume uses the same deferred-start semantics as boot. Any
        # claimer start is queued until the generated barrier has observed the
        # now-cleared WAL and reached active successfully.
        recover_uninstall(args.state_dir, root=args.root, boot=True)
        return 0
    if args.action == "release-select":
        if args.release_id is None:
            parser.error("release-select requires --release-id")
        previous = FileTransaction(args.root, args.state_dir).select_release(
            args.release_id, args.repo_root
        )
        print("" if previous == "ABSENT" else previous)
        return 0
    if args.action == "release-reconcile":
        if args.manifest is None or args.release_id is None:
            parser.error("--manifest and --release-id are required for release-reconcile")
        result = reconcile_release_publication(
            args.release_root,
            args.manifest,
            args.release_id,
            state_dir=args.state_dir,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
        )
        print("AICC_RELEASE_RECONCILED " + (str(result) if result else "ABSENT"))
        return 0
    if args.action in {"release-record", "release-verify", "release-publish"}:
        if (
            args.release_tree is None
            or args.manifest is None
            or args.release_id is None
        ):
            parser.error(
                "--release-tree, --manifest and --release-id are required for "
                f"{args.action}"
            )
        expected_manifest = args.state_dir / "releases" / f"{args.release_id}.json"
        if args.manifest != expected_manifest:
            raise ReleaseRefused("release action manifest is outside trusted state")
        if args.action == "release-record":
            record_release_manifest(
                args.release_tree,
                args.manifest,
                args.release_id,
                trusted_uid=os.geteuid(),
                trusted_gid=os.getegid(),
            )
            print(f"AICC_RELEASE_MANIFEST_RECORDED {args.release_id}")
        elif args.action == "release-verify":
            verify_release_manifest(
                args.release_tree,
                args.manifest,
                args.release_id,
                repo_root=args.repo_root if args.verify_against_git else None,
                trusted_uid=os.geteuid(),
                trusted_gid=os.getegid(),
            )
            print(f"AICC_RELEASE_MANIFEST_VERIFIED {args.release_id}")
        else:
            publish_release_tree(
                args.release_tree,
                args.release_root,
                args.manifest,
                args.release_id,
                trusted_uid=os.geteuid(),
                trusted_gid=os.getegid(),
            )
            print(f"AICC_RELEASE_PUBLISHED {args.release_id}")
        return 0
    if args.action == "uninstall-begin":
        if args.baseline_selector is None:
            parser.error("uninstall-begin requires --baseline-selector")
        _print_uninstall_phase(
            begin_uninstall(
                args.state_dir,
                baseline_selector=args.baseline_selector,
                current_selector=args.current_selector,
                lane_registry=args.lane_registry,
            )
        )
        return 0
    if args.action == "uninstall-arm":
        if args.service_snapshot is None:
            parser.error("uninstall-arm requires --service-snapshot")
        arm_uninstall(args.state_dir, args.service_snapshot)
        return 0
    if args.action == "uninstall-complete":
        if args.service_snapshot is None:
            parser.error("uninstall-complete requires --service-snapshot")
        complete_uninstall(args.state_dir, args.service_snapshot)
        return 0
    if args.action == "uninstall-select-baseline":
        if args.baseline_selector is None:
            parser.error("uninstall-select-baseline requires --baseline-selector")
        FileTransaction(args.root, args.state_dir).select_uninstall_baseline(
            args.baseline_selector
        )
        return 0
    if _path_present(args.state_dir / "uninstall.json") and args.action in {
        "validate",
        "prepare",
        "apply",
        "commit",
        "install",
    }:
        raise RuntimeError("unfinished uninstall journal blocks installation")
    transaction = FileTransaction(args.root, args.state_dir)
    if args.action in {"validate", "prepare", "install"}:
        specs = default_specs(
            args.repo_root,
            authority_env=args.authority_env,
            claude_auth=args.claude_auth,
            codex_auth=args.codex_auth,
            resolve_identities=args.action != "validate",
            profile=args.profile,
        )
    if args.action == "validate":
        transaction.validate_sources(specs)
    elif args.action == "prepare":
        transaction.prepare(specs)
    elif args.action == "apply":
        transaction.apply()
    elif args.action == "commit":
        transaction.commit()
    elif args.action == "quiesce":
        quiesce_service_snapshot(
            args.service_snapshot or args.state_dir / "attempt-units.json"
        )
    elif args.action == "install":
        transaction.install(specs)
    elif args.action in {"recover", "rollback", "recover-boot"}:
        transaction.recover(boot=args.action == "recover-boot")
    else:
        transaction.uninstall_all()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "validate",
            "prepare",
            "apply",
            "commit",
            "quiesce",
            "install",
            "recover",
            "recover-boot",
            "recover-uninstall-boot",
            "recover-uninstall-safe",
            "rollback",
            "uninstall",
            "uninstall-begin",
            "uninstall-arm",
            "uninstall-complete",
            "uninstall-select-baseline",
            "uninstall-status",
            "release-record",
            "release-verify",
            "release-publish",
            "release-reconcile",
            "release-select",
            "recovery-anchor-install",
        ),
    )
    parser.add_argument("--repo-root", type=Path, default=Path("/opt/aicc"))
    parser.add_argument("--root", type=Path, default=Path("/"))
    parser.add_argument(
        "--state-dir", type=Path, default=Path("/var/lib/aicc-principal-isolation")
    )
    parser.add_argument(
        "--authority-env", type=Path, default=Path("/etc/aicc/workspace-authority.env")
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="worker",
        help=(
            "Which host role to install. 'worker' is every spec and stays the "
            "default, so an existing caller installs exactly what it always "
            "did. 'control' omits the agent launcher, worker units and agent "
            "model credentials -- a control-plane host must not hold them."
        ),
    )
    parser.add_argument(
        "--claude-auth",
        type=Path,
        default=Path("/home/voynadmin/.claude/.credentials.json"),
    )
    parser.add_argument(
        "--codex-auth",
        type=Path,
        default=Path("/home/voynadmin/.codex/auth.json"),
    )
    parser.add_argument("--release-tree", type=Path)
    parser.add_argument("--release-root", type=Path, default=Path("/opt/aicc/releases"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--verify-against-git", action="store_true")
    parser.add_argument("--service-snapshot", type=Path)
    parser.add_argument("--baseline-selector")
    parser.add_argument(
        "--current-selector", type=Path, default=Path("/opt/aicc/current")
    )
    parser.add_argument(
        "--lane-registry", type=Path, default=Path("/etc/aicc/worker-lanes")
    )
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    inherited = args.lock_fd
    if inherited is None and os.environ.get("AICC_INSTALL_LOCK_FD") is not None:
        try:
            inherited = int(os.environ["AICC_INSTALL_LOCK_FD"])
        except ValueError as exc:
            raise RuntimeError("invalid inherited install lock descriptor") from exc
    if inherited is None and args.action not in {
        "recover",
        "recover-boot",
        "recover-uninstall-boot",
        "rollback",
    }:
        raise RuntimeError("mutating transaction requires inherited host lock")
    lock_fd = _install_lock_fd(inherited_fd=inherited)
    try:
        return _dispatch(args, parser)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
