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
import math
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path, PurePosixPath

RESTORABLE_UNIT_RE = re.compile(
    r"(?:voyn-aicc-worker@[^/@\s]+\.service|"
    r"voyn-aicc-worker(?:-2)?\.service|"
    r"aicc-worker\.service|"
    r"aicc-agent-launcher@[^/@\s]+\.service|"
    r"aicc-agent-launcher\.socket|aicc-principal-recovery\.service)"
)
TEMPLATE_WORKER_UNIT_RE = re.compile(
    r"voyn-aicc-worker@[^/@\s]+\.service"
)
# The broker socket spawns one `aicc-agent-launcher@<connection>.service` per
# accepted connection. Those instances run the agent launcher off
# `/etc/systemd/system/aicc-agent-launcher@.service` -- a unit file the
# control generation removes -- so they have to be discovered, snapshotted,
# stopped and rolled back exactly like the worker lanes are. Naming only the
# socket left every live instance running on a fragment that was about to be
# deleted (independent review on 9eb07f8).
TEMPLATE_LAUNCHER_UNIT_RE = re.compile(
    r"aicc-agent-launcher@[^/@\s]+\.service"
)
#: `(systemctl glob, accepting pattern)` for every template family whose
#: concrete instances are discovered from systemd rather than from a file.
TEMPLATE_INSTANCE_FAMILIES = (
    ("voyn-aicc-worker@*.service", TEMPLATE_WORKER_UNIT_RE),
    ("aicc-agent-launcher@*.service", TEMPLATE_LAUNCHER_UNIT_RE),
)
#: `prefix@instance.suffix`, the systemd spelling of a template instance.
_UNIT_INSTANCE_RE = re.compile(
    r"(?P<prefix>[^@\s/]+)@(?P<instance>[^/@\s]+)(?P<suffix>\.[a-z]+)"
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


def _template_unit_of(unit: str) -> str | None:
    """The template unit file `unit` is an instance of, or None."""
    match = _UNIT_INSTANCE_RE.fullmatch(unit)
    if match is None:
        return None
    return f"{match['prefix']}@{match['suffix']}"


def _is_restorable_unit(unit: str, restorable: frozenset[str]) -> bool:
    """True when this rollback's journal puts `unit`'s fragment back.

    A concrete lane such as `voyn-aicc-worker@1.service` has no unit file of
    its own: systemd instantiates it from `voyn-aicc-worker@.service`, and
    that template is the only name a generation manifest can carry. Matching
    the journal's targets by exact name therefore never recognised a single
    running lane as restorable, so a rollback interrupted between removing
    the template and restoring it deadlocked on the first instance --
    the same trap `restorable_units` exists to prevent, one level down
    (independent review on 9eb07f8).
    """
    if unit in restorable:
        return True
    template = _template_unit_of(unit)
    return template is not None and template in restorable


@dataclass(frozen=True)
class FileSpec:
    source: Path
    target: str
    mode: int
    uid: int
    gid: int
    if_missing: bool = False
    # True means: after this generation, `target` must not exist. `source`,
    # `mode`, `uid` and `gid` describe nothing installed and are never read --
    # see `removal_spec`. Folding removal into the same spec list as ordinary
    # installs lets one generation (one prepare/apply/commit) both purge and
    # install, so a profile transition gets a single rollback boundary instead
    # of an uninstall that commits before the install it precedes is proven.
    remove: bool = False
    # Only meaningful with `remove`: the target is a DIRECTORY the generation
    # must leave absent. Removing the files under a worker-only tree without
    # removing the tree leaves an empty `/var/lib/aicc-agent/claude/.claude`
    # advertising exactly which secret used to live there, and a directory
    # systemd/tmpfiles would repopulate. A directory removal is snapshotted as
    # mode/uid/gid only: `prepare()` refuses outright if it holds anything
    # this same generation is not already removing, so the removal is
    # reversible by recreating an empty directory and nothing else.
    directory: bool = False
    # The target holds model credentials. A committed removal of one is
    # finalised: its backup is destroyed rather than retained, because a
    # retained backup is the same secret in a different place.
    # See `SENSITIVE_TARGETS` and `FileTransaction._run_sensitive_retirement`.
    sensitive: bool = False


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
    # Mirrors FileSpec.remove: True means this generation's "install" of
    # `target` is its removal, not a write. Defaulted for the same reason as
    # `original_symlink` -- a historical journal predates this field.
    remove: bool = False
    # Mirrors FileSpec.directory. With `remove`, the recorded mode/uid/gid
    # describe a directory that was empty of everything this generation did
    # not itself remove; `backup`, `staged` and both digests are unused.
    directory: bool = False
    # Mirrors FileSpec.sensitive.
    sensitive: bool = False
    # Set by the post-commit retirement phase: this record's backup held
    # credential bytes and was destroyed once the generation became terminal.
    # The record deliberately survives saying so -- an absent field would make
    # an unrecoverable rollback look like an ordinary one.
    sensitive_retired: bool = False


def removal_spec(target: str, *, sensitive: bool = False) -> FileSpec:
    """A spec that removes `target` instead of installing anything.

    `source` is a dummy: `remove=True` short-circuits every codepath that
    would otherwise read it.
    """
    return FileSpec(
        Path(os.devnull), target, 0, 0, 0, remove=True, sensitive=sensitive
    )


def directory_removal_spec(target: str) -> FileSpec:
    """A spec that leaves the DIRECTORY `target` absent after this generation.

    Ordered deepest-first by its caller and always after the file removals
    that empty it, so `apply()` reaches a directory only once everything this
    generation removes from it is gone.
    """
    return FileSpec(
        Path(os.devnull), target, 0, 0, 0, remove=True, directory=True
    )


@dataclass(frozen=True)
class FileState:
    payload: bytes
    sha256: str
    mode: int
    uid: int
    gid: int


UNINSTALL_JOURNAL_VERSION = 2

#: Generation-manifest format this build WRITES.
#:
#: 1 -- the original twelve positional `BackupRecord` fields.
#: 2 -- adds `original_symlink` and `remove`.
#: 3 -- adds `directory`, `sensitive` and `sensitive_retired`.
#:
#: Compatibility is deliberately asymmetric, in both directions:
#:
#: *New code reads old journals.* Every field added after version 1 carries a
#: default that reproduces the older semantics exactly, so a version-1 or
#: version-2 record loads unchanged. `SUPPORTED_MANIFEST_VERSIONS` is what
#: this build accepts; a manifest declaring anything else -- a FUTURE format
#: whose records this build would silently misread -- is refused by
#: `_generation_records` before a single target is touched, as is a record
#: carrying a field this build does not know.
#:
#: *Old readers refuse deterministically.* An older exact-SHA reader builds
#: `BackupRecord(**value)` from the same dicts, so a record carrying a field
#: it predates raises `TypeError` on construction -- in `apply()` and
#: `restore()` alike, that happens while assembling the record list, strictly
#: before the first mutation, so the refusal is total and leaves the host
#: untouched. It is not a graceful message, but it is deterministic and fails
#: closed, and it cannot be improved retroactively in code already deployed.
#: What this build controls is the trigger: the three version-3 fields are
#: emitted only on the records that actually use them, so a generation with
#: no directory purge and no sensitive removal still loads in an older
#: reader, and one that has them refuses there rather than being half
#: understood. Each generation additionally carries its own `recovery.py`
#: capsule, so its own recovery path always runs the build that wrote it.
MANIFEST_VERSION = 3
SUPPORTED_MANIFEST_VERSIONS = frozenset({1, 2, 3})
_BACKUP_RECORD_FIELDS = frozenset(field.name for field in fields(BackupRecord))
#: Record fields introduced by version 3, defaulted to the version-2 meaning.
_MANIFEST_V3_DEFAULTS = {
    "directory": False,
    "sensitive": False,
    "sensitive_retired": False,
}


def _record_document(record: BackupRecord) -> dict[str, object]:
    """Serialise one record, omitting version-3 fields it does not use."""
    document = asdict(record)
    for name, default in _MANIFEST_V3_DEFAULTS.items():
        if document[name] == default:
            del document[name]
    return document


def _record_from_document(value: object) -> BackupRecord:
    if not isinstance(value, dict):
        raise RuntimeError("generation manifest record is malformed")
    unsupported = sorted(set(value) - _BACKUP_RECORD_FIELDS)
    if unsupported:
        raise RuntimeError(
            f"generation manifest record has unsupported fields: {unsupported}"
        )
    return BackupRecord(**value)


def _generation_records(payload: object) -> list[BackupRecord]:
    """Every record of one generation, or a refusal before any mutation."""
    if not isinstance(payload, dict):
        raise RuntimeError("generation manifest is malformed")
    version = payload.get("version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise RuntimeError(f"unsupported generation manifest version: {version!r}")
    records = payload.get("records")
    if not isinstance(records, list):
        raise RuntimeError("generation manifest has no records")
    return [_record_from_document(value) for value in records]


#: Targets whose bytes are model credentials. A generation that REMOVES one
#: has to back it up to stay reversible, and that backup is the same secret
#: in a second place -- so a control commit is not finished until the backup,
#: and every reachable older copy of the same target in this state directory,
#: is destroyed. See `FileTransaction._run_sensitive_retirement`.
SENSITIVE_TARGETS = frozenset(
    {
        "/var/lib/aicc-agent/claude/.claude/.credentials.json",
        "/var/lib/aicc-agent/codex/.codex/auth.json",
    }
)
SENSITIVE_RETIREMENT_VERSION = 2
SENSITIVE_RETIREMENT_JOURNAL = "sensitive-retirement.json"

#: The publisher group owns `/etc/aicc/workspace-authority.env` (0640
#: root:aicc-publisher) on a WORKER host, and `deploy/sysusers.d/aicc-agent.conf`
#: puts `aicc-worker` and `voynadmin` in it. sysusers never takes a membership
#: away, so a host converted from worker to control kept two principals able
#: to read the authority key the control profile is supposed to hold alone.
#:
#: The conversion removes those memberships -- but a membership removal is
#: not a revocation of credentials already held. `/etc/group` is consulted
#: when a process acquires its groups; a process that is already running
#: keeps the numeric gid in its supplementary set until it exits, and every
#: `open()` it makes afterwards is checked against that set, not against the
#: file. A long-lived worker-era daemon therefore goes on reading a 0640
#: root:aicc-publisher file for as long as it lives, no matter what
#: `gpasswd -d` did (independent review on 0a205a0).
#:
#: So the file itself moves. On the control profile the authority key is
#: owned by a group created for this profile and this file --
#: `aicc-control-authority` -- whose gid no worker-era process can be
#: holding, because it did not exist when those processes started. That is
#: what actually revokes a running credential. The membership removal stays,
#: for the principals that are not running yet.
AUTHORITY_GROUP = "aicc-publisher"
#: The control profile's own authority group: declared by
#: `deploy/sysusers.d/aicc-control.conf`, deliberately with no members, so
#: the authority key is root-only in practice until a control-plane unit is
#: given that membership on purpose. A future dedicated publisher principal
#: joins THIS group; nothing is added to `aicc-publisher` on a control host
#: again.
CONTROL_AUTHORITY_GROUP = "aicc-control-authority"
LEGACY_AUTHORITY_MEMBERS = ("aicc-worker", "voynadmin")
AUTHORITY_MEMBERSHIP_VERSION = 3
AUTHORITY_MEMBERSHIP_JOURNAL = "authority-membership.json"
CONTROL_AUTHORITY_PRECONDITION = "control-authority.json"
GPASSWD = "/usr/sbin/gpasswd"


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


def _rename_noreplace(
    source_fd: int, source: str, destination_fd: int, destination: str
) -> None:
    """A native atomic rename that never replaces an existing name.

    Plain `rename` silently destroys whatever is already at the destination,
    which is exactly wrong for the two places this is used -- publishing a
    release, and putting a quarantined object back under its own name. Both
    need "claim this name only if nobody else has". EEXIST is therefore an
    outcome the caller acts on, not a failure to paper over.

    Linux exposes ``renameat2(RENAME_NOREPLACE)``; Darwin exposes the same
    guarantee as ``renameatx_np(RENAME_EXCL)``.  There is deliberately no
    check-then-rename fallback because that would recreate the race this
    helper closes.  ENOSYS means the platform has neither primitive.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    flags = 1  # Linux RENAME_NOREPLACE
    rename = renameat2
    if rename is None:
        rename = getattr(libc, "renameatx_np", None)
        flags = 0x00000004  # Darwin RENAME_EXCL
    if rename is None:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


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


def _read_regular(
    path: Path | str,
    *,
    max_bytes: int = 128 * 1024 * 1024,
    dir_fd: int | None = None,
) -> FileState:
    """Read a regular file whole, proving it did not change underneath.

    `dir_fd` makes `path` a single name resolved against an already-pinned
    directory descriptor instead of a pathname walked from the root. That is
    what lets the purge compare a file it has just moved to a quarantine name
    nothing else knows: no component of the walk can be swapped, because
    there is no walk.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, dir_fd=dir_fd)
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


def _sensitive_blob_directory(generation: Path, directory: str) -> int:
    """Open a generation blob directory with every parent pinned/no-follow."""
    generation_fd = _open_directory_chain(generation, create=False)
    try:
        generation_info = os.fstat(generation_fd)
        if (
            stat.S_IMODE(generation_info.st_mode) != 0o700
            or generation_info.st_uid not in {0, os.geteuid()}
            or generation_info.st_gid not in {0, os.getegid()}
        ):
            raise RuntimeError(
                f"sensitive generation directory is untrusted: {generation}"
            )
        try:
            blob_fd = os.open(directory, _DIR_OPEN_FLAGS, dir_fd=generation_fd)
        except OSError as exc:
            raise RuntimeError(
                f"sensitive blob directory is untrusted: {generation / directory}"
            ) from exc
        try:
            blob_info = os.fstat(blob_fd)
            if (
                stat.S_IMODE(blob_info.st_mode) != 0o700
                or blob_info.st_uid not in {0, os.geteuid()}
                or blob_info.st_gid not in {0, os.getegid()}
            ):
                raise RuntimeError(
                    "sensitive blob directory is untrusted: "
                    f"{generation / directory}"
                )
            return blob_fd
        except BaseException:
            os.close(blob_fd)
            raise
    finally:
        os.close(generation_fd)


def _validate_sensitive_blob(
    generation: Path,
    directory: str,
    name: str,
    expected_sha256: str,
) -> bool:
    """Validate one blob through a pinned parent; False means retired already."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("sensitive blob has no valid manifest digest")
    parent_fd = _sensitive_blob_directory(generation, directory)
    try:
        try:
            blob = _read_regular(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        if (
            blob.sha256 != expected_sha256
            or blob.mode != 0o600
            or blob.uid not in {0, os.geteuid()}
            or blob.gid not in {0, os.getegid()}
        ):
            raise RuntimeError(
                f"sensitive blob drifted: {generation / directory / name}"
            )
        return True
    finally:
        os.close(parent_fd)


def _destroy_blob(
    generation: Path,
    directory: str,
    name: str,
    expected_sha256: str,
) -> None:
    """Overwrite a credential backup in place, then unlink it.

    The unlink is the guarantee -- after it, no name in this state directory
    reaches those bytes. The overwrite is a best effort on top of it: on a
    copy-on-write or log-structured filesystem the old blocks may survive
    until they are reused, and nothing this process can do changes that. It
    is done anyway because on the ext4/xfs hosts this installs on it does
    take the plaintext out of the block that held it.
    """
    path = generation / directory / name
    parent_fd = _sensitive_blob_directory(generation, directory)
    try:
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                raise RuntimeError(
                    f"cannot retire sensitive backup: {path}"
                ) from exc
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid not in {0, os.geteuid()}
                or before.st_gid not in {0, os.getegid()}
            ):
                raise RuntimeError(f"sensitive blob shape drifted: {path}")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError(f"sensitive blob was truncated: {path}")
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != expected_sha256:
                raise RuntimeError(f"sensitive blob digest drifted: {path}")
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"sensitive blob name changed: {path}")
            # Unlink and durably fsync the pinned directory before the
            # best-effort overwrite.  A crash can now occur only while the
            # inode is already unreachable; retry observes the exact name as
            # absent and can redact the manifest idempotently.  Overwriting
            # first left a half-zeroed, digest-mismatching named blob after a
            # power loss and made recovery permanently fail closed.
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            remaining = before.st_size
            offset = 0
            while remaining:
                chunk = min(remaining, 1024 * 1024)
                os.pwrite(descriptor, b"\0" * chunk, offset)
                offset += chunk
                remaining -= chunk
            os.fsync(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent_fd)


def _group_snapshot(
    group: str,
    *,
    getgrnam=grp.getgrnam,
    getpwall=pwd.getpwall,
) -> tuple[int, frozenset[str], frozenset[str]]:
    try:
        entry = getgrnam(group)
    except KeyError as exc:
        raise RuntimeError(f"authority group is missing: {group}") from exc
    primary = frozenset(
        account.pw_name for account in getpwall() if account.pw_gid == entry.gr_gid
    )
    return entry.gr_gid, frozenset(entry.gr_mem), primary


def _group_members(group: str, *, getgrnam=grp.getgrnam) -> frozenset[str]:
    return _group_snapshot(group, getgrnam=getgrnam)[1]


def _assert_fresh_control_authority_group(
    *,
    getgrnam=None,
    getpwall=None,
    proc_root: Path = Path("/proc"),
) -> int:
    """Prove the control-only authority gid has never been delegated.

    A group name is not an authority boundary: processes retain numeric gids
    after `/etc/group` changes.  The control profile therefore accepts its
    fresh group only when it is distinct from the publisher gid, has no
    supplementary or primary members, and no live process already holds it.
    """
    group_lookup = grp.getgrnam if getgrnam is None else getgrnam
    passwd_entries = pwd.getpwall if getpwall is None else getpwall
    control = group_lookup(CONTROL_AUTHORITY_GROUP)
    publisher = group_lookup(AUTHORITY_GROUP)
    if control.gr_gid == publisher.gr_gid:
        raise RuntimeError("control authority group reuses publisher gid")
    if control.gr_mem:
        raise RuntimeError("control authority group has supplementary members")
    primary = [
        account.pw_name
        for account in passwd_entries()
        if account.pw_gid == control.gr_gid
    ]
    if primary:
        raise RuntimeError(
            f"control authority group has primary members: {sorted(primary)}"
        )
    if proc_root.is_dir():
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            try:
                status = (process / "status").read_text(
                    encoding="utf-8", errors="replace"
                )
            except FileNotFoundError:
                continue
            held: set[int] = set()
            for line in status.splitlines():
                if line.startswith("Gid:") or line.startswith("Groups:"):
                    held.update(
                        int(value) for value in line.split()[1:] if value.isdigit()
                    )
            if control.gr_gid in held:
                raise RuntimeError(
                    "control authority gid is retained by a live process"
                )
    return control.gr_gid


def record_control_authority_precondition(state_dir: Path) -> int:
    gid = _assert_fresh_control_authority_group()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    _atomic_bytes(
        state_dir / CONTROL_AUTHORITY_PRECONDITION,
        (
            json.dumps(
                {
                    "version": 1,
                    "group": CONTROL_AUTHORITY_GROUP,
                    "gid": gid,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode(),
        0o600,
        os.geteuid(),
        os.getegid(),
    )
    return gid


def verify_control_authority_precondition(
    state_dir: Path, manifest: Path | None = None
) -> int:
    payload = _trusted_journal(state_dir / CONTROL_AUTHORITY_PRECONDITION)
    if (
        set(payload) != {"version", "group", "gid"}
        or payload.get("version") != 1
        or payload.get("group") != CONTROL_AUTHORITY_GROUP
        or not isinstance(payload.get("gid"), int)
    ):
        raise RuntimeError("control authority precondition is invalid")
    gid = _assert_fresh_control_authority_group()
    if gid != payload["gid"]:
        raise RuntimeError("control authority gid changed after validation")
    if manifest is not None:
        records = _generation_records(_trusted_journal(manifest))
        authority = [
            record
            for record in records
            if record.target == "/etc/aicc/workspace-authority.env"
            and not record.remove
        ]
        if len(authority) != 1 or authority[0].install_gid != gid:
            raise RuntimeError("control authority gid is not bound to generation")
    return gid


def _is_control_generation(manifest: Path) -> bool:
    """Identify a control cutover from durable records, not caller flags."""
    records = _generation_records(_trusted_journal(manifest))
    removals = {record.target for record in records if record.remove}
    return WORKER_ONLY_TARGETS <= removals


def _verify_generation_control_authority(state_dir: Path, manifest: Path) -> None:
    """Make the authority boundary intrinsic to every control generation."""
    if _is_control_generation(manifest):
        verify_control_authority_precondition(state_dir, manifest)


def _authority_membership_journal(state_dir: Path) -> dict[str, object]:
    """The membership WAL, or a refusal: it is never partially trusted.

    Bound to one generation. The revocation belongs to a specific control
    transition, and the direction it has to be resolved in -- undone, or made
    terminal -- is decided by what happened to that generation. A journal
    that names no generation, or names one whose manifest path disagrees with
    it, could be resolved against a transaction that never made it (review on
    0a205a0), so it is refused instead.
    """
    payload = _trusted_journal(state_dir / AUTHORITY_MEMBERSHIP_JOURNAL)
    revoked = payload.get("revoked")
    members_before = payload.get("members_before")
    members_after = payload.get("members_after")
    primary_members = payload.get("primary_members")
    manifest = payload.get("manifest")
    if (
        payload.get("version") != AUTHORITY_MEMBERSHIP_VERSION
        or payload.get("group") != AUTHORITY_GROUP
        or not isinstance(payload.get("group_gid"), int)
        or payload["group_gid"] < 0
        or not isinstance(manifest, str)
        or not isinstance(payload.get("generation"), str)
        or Path(manifest).parent.name != payload["generation"]
        or not isinstance(revoked, list)
        or not isinstance(members_before, list)
        or not isinstance(members_after, list)
        or not isinstance(primary_members, list)
        or any(not isinstance(member, str) for member in revoked)
        or any(not isinstance(member, str) for member in members_before)
        or any(not isinstance(member, str) for member in members_after)
        or any(not isinstance(member, str) for member in primary_members)
        or not set(revoked) <= set(LEGACY_AUTHORITY_MEMBERS)
        or not set(revoked) <= set(members_before)
        or sorted(set(members_before) - set(revoked)) != sorted(members_after)
    ):
        raise RuntimeError("authority membership journal is invalid")
    return payload


def _authority_membership_bound(
    payload: dict[str, object], manifest: Path | None
) -> dict[str, object]:
    """Refuse a journal that belongs to a different generation."""
    if manifest is not None and payload["manifest"] != str(manifest):
        raise RuntimeError(
            "authority membership journal is bound to another generation: "
            f"{payload['generation']}"
        )
    return payload


def _authority_membership_state(
    payload: dict[str, object], *, getgrnam=grp.getgrnam, getpwall=pwd.getpwall
) -> str:
    """Where the group is now: exactly `before`, exactly `after`, or refused.

    Both directions are idempotent, so both have to tolerate finding the
    group already in the state they were going to produce. What neither may
    tolerate is a THIRD state -- a member this transaction never recorded,
    or one it recorded and something else has since taken out. Either way the
    group is no longer described by this journal, and both `gpasswd -a` and
    `gpasswd -d` would be acting on a membership list nobody in this
    transaction has seen (review on 0a205a0). Refuse before mutating, with
    the journal retained.
    """
    gid, current, primary = _group_snapshot(
        AUTHORITY_GROUP, getgrnam=getgrnam, getpwall=getpwall
    )
    if gid != payload["group_gid"]:
        raise RuntimeError("authority group numeric gid changed during transaction")
    if primary != frozenset(payload["primary_members"]):
        raise RuntimeError("authority group primary membership drifted")
    before = frozenset(payload["members_before"])
    after = frozenset(payload["members_after"])
    revoked = frozenset(payload["revoked"])
    if current - revoked != after:
        raise RuntimeError(
            f"authority group drifted outside this transaction: {sorted(current)}"
        )
    if current == after:
        return "after"
    if current == before:
        return "before"
    return "partial"


def revoke_legacy_authority_membership(
    state_dir: Path,
    manifest: Path,
    *,
    run=subprocess.run,
    getgrnam=grp.getgrnam,
) -> tuple[str, ...]:
    """Take the worker-era principals out of the publisher group.

    On a converted host `aicc-worker` and `voynadmin` are still in the
    publisher group, and sysusers can add a membership but never removes one,
    so the conversion has to. This is what stops those principals acquiring
    the group the NEXT time they start; it is not what revokes the credential
    a process already running holds, which is why the control profile also
    moves the authority key to `CONTROL_AUTHORITY_GROUP`.

    Reversible, journalled before the first mutation, and bound to the
    generation that is making the transition: the pre-state and the exact
    post-state are written durably to `authority-membership.json` naming
    `manifest`, so a failure here -- or a crash, or a later failure anywhere
    before commit -- is undone by `restore_legacy_authority_membership`,
    which the same `recover` the installer's rollback trap runs calls.
    `commit()` consumes the journal, which is what makes the revocation
    terminal. Both directions need root (`gpasswd`), both compare the group
    against exactly one of the two states this journal describes, and both
    fail closed: a revocation that cannot be proven raises, and so does a
    restore that cannot be proven, leaving the durable journal for the next
    attempt rather than reporting a rollback that did not happen.

    Returns the members actually revoked.
    """
    journal = state_dir / AUTHORITY_MEMBERSHIP_JOURNAL
    if _path_present(journal):
        payload = _authority_membership_bound(
            _authority_membership_journal(state_dir), manifest
        )
    else:
        group_gid, members, primary_members = _group_snapshot(
            AUTHORITY_GROUP, getgrnam=getgrnam
        )
        if set(LEGACY_AUTHORITY_MEMBERS) & set(primary_members):
            raise RuntimeError(
                "legacy authority principal uses publisher as its primary group"
            )
        revoked_members = [
            member for member in LEGACY_AUTHORITY_MEMBERS if member in members
        ]
        payload = {
            "version": AUTHORITY_MEMBERSHIP_VERSION,
            "group": AUTHORITY_GROUP,
            "group_gid": group_gid,
            "generation": manifest.parent.name,
            "manifest": str(manifest),
            "members_before": sorted(members),
            "members_after": sorted(members - set(revoked_members)),
            "revoked": revoked_members,
            "primary_members": sorted(primary_members),
        }
        _atomic_bytes(
            journal,
            (json.dumps(payload, sort_keys=True) + "\n").encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
        _fsync_dir(state_dir)
    _authority_membership_state(payload, getgrnam=getgrnam)
    revoked = tuple(payload["revoked"])
    for member in revoked:
        if member not in _group_members(AUTHORITY_GROUP, getgrnam=getgrnam):
            continue
        result = run(
            [GPASSWD, "-d", member, AUTHORITY_GROUP],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode and member in _group_members(
            AUTHORITY_GROUP, getgrnam=getgrnam
        ):
            raise RuntimeError(
                f"cannot revoke legacy authority membership: {member}"
            )
        _authority_membership_state(payload, getgrnam=getgrnam)
    if _authority_membership_state(payload, getgrnam=getgrnam) != "after":
        raise RuntimeError(
            "legacy authority membership survived revocation: "
            f"{sorted(_group_members(AUTHORITY_GROUP, getgrnam=getgrnam))}"
        )
    return revoked


def restore_legacy_authority_membership(
    state_dir: Path,
    *,
    manifest: Path | None = None,
    run=subprocess.run,
    getgrnam=grp.getgrnam,
) -> None:
    """Put back exactly the memberships this transaction revoked."""
    if not _path_present(state_dir / AUTHORITY_MEMBERSHIP_JOURNAL):
        return
    payload = _authority_membership_bound(
        _authority_membership_journal(state_dir), manifest
    )
    if _authority_membership_state(payload, getgrnam=getgrnam) == "before":
        # Already back, and provably exactly back. A retried rollback must
        # not hand `gpasswd` a list it has already applied.
        (state_dir / AUTHORITY_MEMBERSHIP_JOURNAL).unlink()
        _fsync_dir(state_dir)
        return
    for member in payload["revoked"]:
        if member in _group_members(AUTHORITY_GROUP, getgrnam=getgrnam):
            continue
        result = run(
            [GPASSWD, "-a", member, AUTHORITY_GROUP],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(
                f"cannot restore legacy authority membership: {member}"
            )
        _authority_membership_state(payload, getgrnam=getgrnam)
    if _authority_membership_state(payload, getgrnam=getgrnam) != "before":
        raise RuntimeError(
            "legacy authority membership did not restore: "
            f"{sorted(_group_members(AUTHORITY_GROUP, getgrnam=getgrnam))}"
        )
    (state_dir / AUTHORITY_MEMBERSHIP_JOURNAL).unlink()
    _fsync_dir(state_dir)


def finalize_authority_membership(
    state_dir: Path, manifest: Path | None = None, *, getgrnam=grp.getgrnam
) -> None:
    """Consume the membership journal: the revocation is now terminal."""
    journal = state_dir / AUTHORITY_MEMBERSHIP_JOURNAL
    if not _path_present(journal):
        return
    payload = _authority_membership_bound(
        _authority_membership_journal(state_dir), manifest
    )
    # The generation is live, so this journal is spent in the forward
    # direction -- but only a group this journal still describes may be
    # declared terminal. A third state means the membership list changed
    # under a transaction that is still holding the only record of what it
    # was, and consuming the record would destroy the evidence.
    if _authority_membership_state(payload, getgrnam=getgrnam) != "after":
        raise RuntimeError("authority membership revocation is incomplete")
    journal.unlink()
    _fsync_dir(state_dir)


def _sensitive_removal_targets(manifest: Path) -> frozenset[str]:
    """Every credential target one generation removes.

    `existed` is deliberately not consulted. Whether the LIVE file was there
    when `prepare()` looked says nothing about whether this state directory
    still holds the bytes: a worker generation that installed the credential
    staged a copy of it, and that copy survives every later removal of the
    file itself. A host whose credential was deleted by hand months ago, then
    converted to control, armed nothing and kept the secret in
    `generation-0001/staged/` forever (independent review on 0a205a0). The
    intent is about the target NAME, and the retirement phase then destroys
    every reachable copy of it.
    """
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return frozenset(
        record.target
        for record in _generation_records(payload)
        if record.remove
        and not record.directory
        and (record.sensitive or record.target in SENSITIVE_TARGETS)
    )


def _redact_sensitive_records(manifest: Path, retired: frozenset[str]) -> bool:
    """Destroy one generation's copies of `retired`, and say so in its journal.

    Both blob kinds are taken: `backup` is the copy a removal made to stay
    reversible, and `staged` is the copy an earlier worker generation wrote
    on its way to installing the same credential. Either one is the secret.
    """
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    records = payload.get("records")
    if not isinstance(records, list):
        raise RuntimeError(f"generation manifest has no records: {manifest}")
    changed = False
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("target") not in retired:
            continue
        destroyed = False
        for key, directory, digest_key in (
            ("backup", "backups", "original_sha256"),
            ("staged", "staged", "install_sha256"),
        ):
            blob = record.get(key)
            if isinstance(blob, str) and blob:
                digest = record.get(digest_key)
                if not isinstance(digest, str):
                    raise RuntimeError(
                        f"sensitive {key} has no bound digest: {manifest}"
                    )
                _destroy_blob(
                    manifest.parent,
                    directory,
                    f"{index:03d}.bin",
                    digest,
                )
                destroyed = True
        if not destroyed:
            # This record held no copy of the secret -- a removal of a target
            # that was already absent, or a record a previous run of this
            # phase already emptied. `sensitive_retired` is what makes a
            # generation unrollbackable, and nothing here was destroyed, so
            # claiming it would refuse a rollback that is perfectly possible.
            continue
        record["backup"] = None
        record["original_sha256"] = None
        record["staged"] = ""
        record["install_sha256"] = ""
        record["sensitive"] = True
        record["sensitive_retired"] = True
        changed = True
    if not changed:
        return False
    payload["version"] = MANIFEST_VERSION
    _atomic_bytes(
        manifest,
        json.dumps(payload, sort_keys=True).encode(),
        0o600,
        os.geteuid(),
        os.getegid(),
    )
    return True


def _preflight_sensitive_records(
    state_dir: Path, manifest: Path, retired: frozenset[str]
) -> None:
    """Bind every destroyable blob to its exact generation-local name.

    Historical manifests are durable data, not executable authority.  A
    forged absolute `staged` path must never turn credential retirement into
    an arbitrary root unlink.  Validate every manifest and blob before the
    first generation is mutated so a bad later record leaves earlier secrets
    untouched for fail-closed recovery.
    """
    generation = manifest.parent
    if (
        not re.fullmatch(r"generation-[0-9a-f]{16}", generation.name)
        or generation.parent.resolve() != state_dir.resolve()
        or generation.resolve() != generation
        or manifest != generation / "manifest.json"
    ):
        raise RuntimeError(f"sensitive generation path is invalid: {generation}")
    info = generation.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid not in {0, os.geteuid()}
        or info.st_gid not in {0, os.getegid()}
    ):
        raise RuntimeError(f"sensitive generation directory is untrusted: {generation}")
    payload = _trusted_journal(manifest)
    for index, record in enumerate(_generation_records(payload)):
        if record.target not in retired:
            continue
        for field, directory, digest in (
            ("backup", "backups", record.original_sha256),
            ("staged", "staged", record.install_sha256),
        ):
            value = getattr(record, field)
            if not value:
                continue
            expected = generation / directory / f"{index:03d}.bin"
            if Path(value) != expected:
                raise RuntimeError(
                    f"sensitive {field} escaped its generation: {value}"
                )
            if not isinstance(digest, str):
                raise RuntimeError(
                    f"sensitive {field} has no bound digest: {manifest}"
                )
            if not _validate_sensitive_blob(
                generation, directory, expected.name, digest
            ):
                # A prior retirement attempt may have durably unlinked this
                # exact generation-local blob before crashing ahead of the
                # manifest redaction. The retry remains bound to the same
                # name and completes the redaction idempotently.
                continue


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


#: Drop-in directories systemd *generates* at boot. `aicc-principal-recovery`
#: writes its drop-in into `/run/systemd/generator.early/`, which is tmpfs:
#: it exists only while the generator's output from this boot is live, and is
#: absent after `/run` is cleared or before the generator has run again.
#:
#: Requiring it to match a snapshot therefore demands a value that by
#: construction does not persist -- the restore refused with
#: `refusing unsafe snapshot restart: voyn-aicc-worker.service DropInPaths`
#: on a host where nothing was wrong (worker-01, 2026-08-31). Drop-ins from
#: any other location are compared exactly, because those are administrator
#: configuration and a silently added one can weaken the very isolation this
#: snapshot exists to preserve.
_GENERATED_DROPIN_PREFIXES = ("/run/systemd/generator",)


def _properties_match(name: str, actual: str, expected: str) -> bool:
    """Whether a restored property equals its snapshot, comparing what the
    property actually asserts rather than its rendered text.

    Two properties render values that change without the configuration
    changing: a command carries its last invocation's pid and timestamps, and
    `DropInPaths` carries boot-generated entries from tmpfs. Everything else
    is compared verbatim.
    """
    if name == "DropInPaths":
        return _persistent_dropins(actual) == _persistent_dropins(expected)
    return _normalise_property(actual) == _normalise_property(expected)


def _persistent_dropins(value: str) -> tuple[str, ...]:
    """DropInPaths with the boot-generated ones removed, order-independent."""
    return tuple(
        sorted(
            path
            for path in value.split()
            if not path.startswith(_GENERATED_DROPIN_PREFIXES)
        )
    )


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
    """Restore the pre-attempt unit state after file generation recovery.

    Everything the snapshot describes except the per-connection launcher
    instances, which are restored by not being touched: see the loop below.
    """
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
        if TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit):
            # `aicc-agent-launcher.socket` is `Accept=yes`: each of these
            # instances exists because systemd accepted one connection and
            # passed it in as the service's stdin. That descriptor belongs to
            # a client this rollback has already stopped; it cannot be
            # recreated, and `systemctl start` on the instance would run the
            # launcher against a socket nobody is on the other end of.
            # Restoring "active" here is therefore not restoring anything --
            # it is starting a new, connectionless process under a session's
            # name (independent review on 0a205a0). The sessions are gone
            # with the workers that opened them; what this rollback restores
            # is the workers and the socket, and the next connection makes
            # the next instance.
            continue
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
        load_rc, load_state = probe("show", unit, "--property=LoadState", "--value")
        if (
            not load_rc
            and load_state == "not-found"
            and unit in RETIRED_LEGACY_UNITS
        ):
            # Retiring the pre-template workers is what installing DOES, and
            # `disable` removes the symlink that was their fragment. Their
            # snapshot still describes the running configuration from before
            # that, so restoring it would revive a unit the rollout just
            # deliberately took out of service -- and, because the unit is
            # gone, every property it recorded now reads empty and the
            # comparison refuses.
            #
            # That refusal is what the live host produced, one property at a
            # time: DropInPaths, then EnvironmentFiles, each on a unit that
            # was correctly absent (worker-01, 2026-08-31). The snapshot is
            # not wrong; it simply predates a removal the installer intended.
            # Nothing to restore, so nothing is attempted.
            continue
        if version == 3 and not self_recovery:
            for name, expected in state["properties"].items():
                property_rc, actual = probe(
                    "show", unit, f"--property={name}", "--value"
                )
                if property_rc or not _properties_match(name, actual, expected):
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


#: Where systemd reads administrator-installed units. Unit files the journal
#: restores live here; a target anywhere else is not a unit.
_SYSTEMD_UNIT_DIR = "/etc/systemd/system/"


def _units_restored_by(manifest: Path) -> frozenset[str]:
    """Unit names whose files this rollback's journal will put back.

    Read from the generation manifest rather than from the filesystem: what
    matters is what `restore()` is going to do, not what happens to be on disk
    at this instant.
    """
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = payload["records"]
    except (OSError, ValueError, KeyError):
        # An unreadable journal is not a licence to skip refusals; the caller
        # fails on it a moment later, with a better message than this one.
        return frozenset()
    units = set()
    for record in records:
        target = str(record.get("target", ""))
        if target.startswith(_SYSTEMD_UNIT_DIR):
            name = target[len(_SYSTEMD_UNIT_DIR) :].lstrip("/")
            if name and "/" not in name:
                units.add(name)
    # Template unit files are returned under their own name
    # (`voyn-aicc-worker@.service`). `_is_restorable_unit` resolves a concrete
    # instance back to it, which is the only way a lane -- which has no unit
    # file of its own -- can be recognised as restorable.
    return frozenset(units)


#: Pre-template worker units the staged rollout retires as a normal part of
#: installing. `retire_legacy_units` stops and *disables* each one, and
#: disabling a unit whose fragment is a symlink in /etc/systemd/system removes
#: that symlink -- so after a rollout these units are legitimately gone, while
#: the snapshot taken before it still records them as present.
#:
#: Quiesce must therefore tolerate their absence. Without that, each install
#: attempt retired one more of them and the next attempt died on it: four
#: consecutive attempts on worker-01 failed on `voyn-aicc-worker-2.service`,
#: then `voyn-aicc-worker.service`, with nothing wrong except this
#: bookkeeping (2026-08-31).
#:
#: Duplicated from `LEGACY_WORKER_UNITS` in ops/aicc_staged_worker_rollout.py
#: and from the `--include-unit` arguments in
#: deploy/install-agent-principal-isolation.sh -- this module is imported by
#: the bootstrap before the rollout module exists on disk, so it cannot import
#: it. The three lists are held in lockstep by a fitness test.
RETIRED_LEGACY_UNITS = frozenset(
    {
        "voyn-aicc-worker.service",
        "voyn-aicc-worker-2.service",
        "aicc-worker.service",
    }
)


def quiesce_service_snapshot(
    path: Path, *, run=subprocess.run, restorable_units: frozenset[str] = frozenset()
) -> None:
    """Stop snapshotted admission/worker units before rollback mutation.

    `restorable_units` names the units this same rollback is about to put back
    on disk, including the templates whose instances it thereby restores. A
    unit whose file is currently missing has nothing to stop, and
    refusing to proceed on that ground creates a deadlock the operator cannot
    escape from: the file is restored by `restore()`, which runs *after* this
    function, so a rollback interrupted between removing a unit file and
    restoring it can never be resumed — every later attempt dies here, on the
    unit the previous attempt was in the middle of replacing. Observed live on
    worker-01 across four install attempts, each stopping on the next such
    unit (2026-08-31).

    A missing unit that this rollback does NOT intend to restore is still a
    refusal: that is genuinely unexplained divergence, and proceeding would
    mutate a host whose state nobody can account for.
    """
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
        if TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit):
            # An Accept=yes instance owns a live client descriptor.  Stopping
            # it is irreversible: restore_service_snapshot deliberately does
            # not restart such instances because a new process cannot regain
            # the accepted connection.  Restore the template on disk and let
            # existing sessions finish naturally.
            continue
        load = command("show", unit, "--property=LoadState", "--value")
        load_state = load.stdout.strip()
        if load_state == "not-found":
            if (
                expected["exists"] is False
                and expected["active"] is False
                and expected["enabled"] is False
            ):
                continue
            if _is_restorable_unit(unit, restorable_units):
                # Nothing to stop, and the file is coming back in this same
                # rollback -- either under its own name or, for a concrete
                # lane, as the template it is an instance of. See
                # `restorable_units` in the docstring and `_is_restorable_unit`.
                continue
            if unit in RETIRED_LEGACY_UNITS:
                # Retired on purpose by the rollout that preceded this
                # rollback. See RETIRED_LEGACY_UNITS.
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


def discover_template_instances(*, run=subprocess.run, message: str) -> set[str]:
    """Every concrete instance of every template family systemd knows about.

    Both template families are discovered the same way and for the same
    reason: neither `voyn-aicc-worker@<lane>` nor
    `aicc-agent-launcher@<connection>` has a fixed name, so the only source
    of truth for which ones exist is systemd itself.
    """
    discovered: set[str] = set()
    for pattern, accepts in TEMPLATE_INSTANCE_FAMILIES:
        for arguments in (
            ("list-unit-files", pattern, "--no-legend", "--no-pager"),
            ("list-units", pattern, "--all", "--no-legend", "--no-pager"),
        ):
            result = run(
                ["/usr/bin/systemctl", *arguments],
                capture_output=True,
                check=False,
                text=True,
            )
            # `list-unit-files` exits 1 when a pattern matches nothing, and on
            # a control-plane host it matches nothing by design: that profile
            # installs no worker lanes and no agent launcher at all. An empty
            # result is the answer, not a failure -- treating it as one made
            # the control install die at `cannot enumerate worker lanes for
            # snapshot closure` (observed live on control-01, 2026-08-31). A
            # real failure must not be inferred from output: systemctl can
            # fail silently (for example when its transport dies).  The only
            # non-zero result documented by this caller as an empty answer is
            # rc=1 from list-unit-files with no output at all.  In particular,
            # list-units and every other status remain fail-closed.
            expected_empty_no_match = (
                arguments[0] == "list-unit-files"
                and result.returncode == 1
                and not result.stderr.strip()
                and not result.stdout.strip()
            )
            if expected_empty_no_match:
                # rc=1 + empty output is also how a broken manager transport
                # can present.  Accept it as "no matches" only after an
                # independent manager query proves systemd is reachable.
                manager = run(
                    [
                        "/usr/bin/systemctl",
                        "show",
                        "--property=Version",
                        "--value",
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                expected_empty_no_match = (
                    manager.returncode == 0 and bool(manager.stdout.strip())
                )
            if result.returncode and not expected_empty_no_match:
                raise RuntimeError(result.stderr.strip() or message)
            for line in result.stdout.splitlines():
                fields = line.split()
                if fields and fields[0] == "●":
                    fields = fields[1:]
                candidate = fields[0] if fields else ""
                if accepts.fullmatch(candidate):
                    discovered.add(candidate)
    return discovered


#: The unit that listens for launcher connections. `Accept=yes`, so systemd
#: instantiates one `aicc-agent-launcher@<connection>.service` per accepted
#: connection and hands it the accepted file descriptor.
LAUNCHER_SOCKET_UNIT = "aicc-agent-launcher.socket"


def _control_purge_order(unit: str) -> tuple[int, str]:
    """Drain clients first; launcher instances are handled after admission.

    The workers are what connect to `/run/aicc-agent-launcher/control.sock`.
    Closing the socket first leaves every running worker making requests into
    a socket that has been removed (`RemoveOnStop=yes`) while its own unit
    file is still on disk and `Restart=always` keeps it coming back -- the
    purge tears the floor out from under a layer that is still running, and
    the in-flight agent sessions die as connection errors rather than as a
    drain (independent review on 0a205a0).

    So the static ordering places workers before the socket. The caller then
    closes and proves the socket before waiting for every already-accepted
    launcher instance to finish naturally. Waiting for instances while the
    socket is still accepting would leave an unbounded admission race.
    """
    if unit == LAUNCHER_SOCKET_UNIT:
        return (2, unit)
    if TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit):
        return (1, unit)
    return (0, unit)


def verify_service_snapshot_closure(
    path: Path,
    *,
    run=subprocess.run,
    preserve_unsnapshotted_launchers: bool = False,
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

    discovered = discover_template_instances(
        run=run, message="cannot enumerate template units for snapshot closure"
    )

    extras = discovered - set(units)
    if preserve_unsnapshotted_launchers:
        # A launcher accepted after the point-in-time snapshot owns a live
        # client descriptor which rollback cannot recreate. Normal install
        # recovery neither stops nor restarts these preserve-only transient
        # units; it restores their template and lets them finish. Worker lanes
        # remain restore-managed and therefore strict. Terminal uninstall does
        # stop launcher sessions and calls this function with the default
        # strict policy.
        extras = {
            unit
            for unit in extras
            if not TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit)
        }
    extras = sorted(extras)
    if extras:
        raise RuntimeError(
            f"template units exist outside service snapshot: {extras}"
        )


#: Statically-named units a control-profile host must not run. Neither the
#: `voyn-aicc-worker@<lane>` nor the `aicc-agent-launcher@<connection>`
#: template has a fixed name -- both are discovered from systemd by
#: `discover_template_instances`, the same enumeration
#: `verify_service_snapshot_closure` proves closure against.
#: `aicc-principal-recovery.service` is deliberately absent: it is the boot
#: recovery capsule, kept running through every transaction the same way
#: `quiesce_service_snapshot` already exempts it from being stopped.
#: The legacy worker units are taken from `RETIRED_LEGACY_UNITS` rather than
#: relisted: a name that drifts between the two would leave a worker unit
#: running on a control host whose unit file this generation just removed.
CONTROL_PURGE_UNITS = frozenset({"aicc-agent-launcher.socket"}) | RETIRED_LEGACY_UNITS


#: Ordinary worker clients and the admission socket should stop promptly.
DRAIN_ATTEMPTS = 60
DRAIN_INTERVAL_SECONDS = 1.0
#: Accepted launcher instances are different: they are never stopped because
#: rollback cannot recreate their accepted socket descriptors. Wait for the
#: longest task the broker can admit, then allow systemd time to reap the
#: released cgroup. A cross-module fitness test keeps the admission and drain
#: limits equal.
# Keep this value in lockstep with aicc_agent_launcher.MAX_TASK_TIMEOUT_SECONDS.
# The transaction recovery capsule is intentionally self-contained and cannot
# import the separately installed broker, so a fitness test enforces equality.
MAX_ACCEPTED_LAUNCHER_SECONDS = 3600
LAUNCHER_DRAIN_GRACE_SECONDS = 60
LAUNCHER_DRAIN_ATTEMPTS = math.ceil(
    (MAX_ACCEPTED_LAUNCHER_SECONDS + LAUNCHER_DRAIN_GRACE_SECONDS)
    / DRAIN_INTERVAL_SECONDS
) + 2
#: An inactive unit whose cgroup is gone. `ControlGroup` empties only once
#: systemd has released it, and `TasksCurrent` counts every process still in
#: it -- a `KillMode=mixed` service can have left children behind after its
#: MainPID exited, and those children are still running the agent layer this
#: generation is about to delete the unit files of.
_QUIESCED_ACTIVE_STATES = frozenset({"inactive", "failed"})
_QUIESCED_TASK_COUNTS = frozenset({"", "0", "[not set]"})
#: Asked for in one `systemctl show`, in `Name=value` form rather than with
#: `--value`, so the answer says which property it is answering.
DRAIN_PROPERTIES = ("ActiveState", "MainPID", "ControlGroup", "TasksCurrent")


def _unit_drained(unit: str, *, run=subprocess.run) -> tuple[bool, str]:
    """(drained, why-not) for one unit: inactive, no MainPID, empty cgroup."""
    result = run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            *(f"--property={name}" for name in DRAIN_PROPERTIES),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip()
    if result.returncode or set(values) != set(DRAIN_PROPERTIES):
        return False, f"{unit} cannot be proven drained"
    if values["ActiveState"] not in _QUIESCED_ACTIVE_STATES:
        return False, f"{unit} is {values['ActiveState'] or 'unknown'}"
    if values["MainPID"] not in {"", "0"}:
        return False, f"{unit} retains its main process"
    if values["ControlGroup"]:
        return False, f"{unit} still holds a control group"
    if values["TasksCurrent"] not in _QUIESCED_TASK_COUNTS:
        return False, f"{unit} still has tasks in its cgroup"
    return True, ""


def quiesce_worker_only_units(*, run=subprocess.run, sleep=time.sleep) -> None:
    """Drain and disable every unit a control-profile host must not run.

    Call once `prepare()` has validated and staged the control generation
    but before `apply()` removes the underlying unit files, so a failure
    here still leaves an intact, recoverable pending generation rather than
    unit files gone out from under a service still running on them.
    Tolerates a unit that was never loaded -- a control host that never ran
    the worker profile has none of these.

    Ordered as a drain (`_control_purge_order`): the worker lanes that create
    connections go first, then admission closes. Already accepted launcher
    instances are NEVER stopped: their socket descriptors cannot be recreated
    by rollback. The listener unit therefore must not carry Requires= from an
    instance back to the socket. Once admission is proven closed, wait for
    every accepted instance to finish naturally and bracket discovery with
    systemd's activation-job list. No jobs plus no live cgroup is the barrier
    that makes removal of the launcher template safe; a timeout fails the
    transaction and rollback reopens admission without killing a session.
    """

    def systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        return run(
            ["/usr/bin/systemctl", *arguments],
            capture_output=True,
            check=False,
            text=True,
        )

    def stop(unit: str) -> bool:
        """Stop and disable `unit`; False when there was nothing loaded."""
        load = systemctl("show", unit, "--property=LoadState", "--value")
        if load.returncode:
            raise RuntimeError(
                f"cannot prove worker-only unit load state: {unit}"
            )
        load_state = load.stdout.strip()
        if load_state == "not-found":
            return False
        if load_state not in {
            "loaded",
            "masked",
            "error",
            "bad-setting",
            "stub",
            "merged",
            "alias",
            "generated",
            "transient",
        }:
            raise RuntimeError(
                f"cannot prove worker-only unit load state: {unit}"
            )
        stopped = systemctl("disable", "--now", unit)
        if stopped.returncode:
            raise RuntimeError(
                f"cannot stop worker-only unit before control purge: {unit}"
            )
        return True

    def enumerate_units() -> list[str]:
        return sorted(
            set(CONTROL_PURGE_UNITS)
            | discover_template_instances(
                run=run,
                message="cannot enumerate template units before control purge",
            ),
            key=_control_purge_order,
        )

    def launcher_jobs() -> frozenset[str]:
        result = systemctl("list-jobs", "--no-legend", "--no-pager")
        if result.returncode:
            raise RuntimeError(
                "cannot prove launcher activation queue before control purge"
            )
        jobs: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            # systemctl list-jobs: JOB UNIT TYPE STATE. Ignore its optional
            # footer and unrelated units, but never a launcher activation.
            if len(fields) >= 2 and TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(fields[1]):
                jobs.add(fields[1])
        return frozenset(jobs)

    def wait_drained(units: set[str], *, message: str) -> None:
        for attempt in range(DRAIN_ATTEMPTS):
            outstanding = [
                reason
                for unit in sorted(units)
                for is_drained, reason in (_unit_drained(unit, run=run),)
                if not is_drained
            ]
            if not outstanding:
                return
            if attempt == DRAIN_ATTEMPTS - 1:
                raise RuntimeError(f"{message}: {outstanding}")
            sleep(DRAIN_INTERVAL_SECONDS)

    # First stop the clients. Launcher instances are deliberately excluded:
    # they may still be serving the workers' final accepted requests.
    clients: set[str] = set()
    for unit in enumerate_units():
        if unit == LAUNCHER_SOCKET_UNIT or TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit):
            continue
        if stop(unit):
            clients.add(unit)
    wait_drained(
        clients,
        message="worker-only clients did not drain before control purge",
    )

    # Close admission before the final launcher enumeration. Enumerating
    # while Accept=yes is still listening always leaves a gap in which a new
    # instance can appear after the last snapshot.
    socket_loaded = stop(LAUNCHER_SOCKET_UNIT)
    if socket_loaded:
        wait_drained(
            {LAUNCHER_SOCKET_UNIT},
            message="launcher socket did not close before control purge",
        )

    launchers: set[str] = set()
    stable_passes = 0
    for attempt in range(LAUNCHER_DRAIN_ATTEMPTS):
        jobs_before = launcher_jobs()
        discovered = {
            unit
            for unit in enumerate_units()
            if TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit)
        }
        new = discovered - launchers
        launchers.update(discovered)
        outstanding = [
            reason
            for unit in sorted(launchers)
            for is_drained, reason in (_unit_drained(unit, run=run),)
            if not is_drained
        ]
        jobs_after = launcher_jobs()
        closing = {
            unit
            for unit in enumerate_units()
            if TEMPLATE_LAUNCHER_UNIT_RE.fullmatch(unit)
        }
        unseen = closing - launchers
        launchers.update(closing)
        if (
            not outstanding
            and not unseen
            and not new
            and not jobs_before
            and not jobs_after
        ):
            stable_passes += 1
            if stable_passes == 2:
                return
        else:
            stable_passes = 0
        if attempt == LAUNCHER_DRAIN_ATTEMPTS - 1:
            raise RuntimeError(
                "launcher instances did not reach stable closure before "
                "control purge: "
                f"outstanding={outstanding}, unseen={sorted(unseen)}, "
                f"jobs={sorted(jobs_before | jobs_after)}"
            )
        sleep(DRAIN_INTERVAL_SECONDS)


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
            # mkdir's mode is masked by the caller's umask; the state
            # directory's privacy must not be.
            os.chmod(self.state_dir, 0o700)
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
            if not spec.remove:
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
        source_states = {
            spec.target: _read_regular(spec.source)
            for spec in validated
            if not spec.remove
        }
        removed = frozenset(spec.target for spec in validated if spec.remove)
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
            if spec.remove and spec.directory:
                # Shape and emptiness are proven together in
                # `_snapshot_directory_removal`, which needs the full removal
                # set this preflight does not carry.
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
        # Created and chmod'ed explicitly, never with `parents=True`: pathlib
        # creates an intermediate parent with the default 0o777 masked by the
        # CALLER's umask, so under the common 0o002 the generation directory
        # -- which holds the credential backups -- came out group-writable,
        # and under 0o000 world-writable. mkdir's own mode argument is masked
        # too, so the chmod after it is what makes the result independent of
        # the umask the installer happened to inherit.
        for directory in (transaction, backups, staged):
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
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
                if spec.remove and spec.directory:
                    records.append(
                        self._snapshot_directory_removal(spec, target, removed)
                    )
                    continue
                if spec.remove:
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
                                "",
                                "",
                                0,
                                0,
                                0,
                                remove=True,
                                sensitive=spec.sensitive,
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
                                "",
                                "",
                                0,
                                0,
                                0,
                                os.readlink(target),
                                remove=True,
                                sensitive=spec.sensitive,
                            )
                        )
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(
                            f"existing target is not a regular file: {target}"
                        )
                    original = _read_regular(target)
                    backup = backups / f"{index:03d}.bin"
                    _atomic_bytes(
                        backup, original.payload, 0o600, os.geteuid(), os.getegid()
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
                            "",
                            "",
                            0,
                            0,
                            0,
                            remove=True,
                            sensitive=spec.sensitive,
                        )
                    )
                    continue
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
                        "version": MANIFEST_VERSION,
                        "generation": transaction.name,
                        "records": [
                            _record_document(record) for record in records
                        ],
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

    def _snapshot_directory_removal(
        self, spec: FileSpec, target: Path, removed: frozenset[str]
    ) -> BackupRecord:
        """Record a worker-only directory, refusing anything unaccounted for.

        The control transition must not leave a residual agent tree behind --
        an empty `/var/lib/aicc-agent/claude/.claude` still names the secret
        that used to be in it, and `/run/aicc-agent-homes` is where the
        launcher materialises ephemeral copies of both credentials. So the
        directories go too.

        What it must not do is delete a file it never examined. A directory
        removal is reversible only as "recreate an empty directory with this
        mode and owner", so the only content this generation may find here is
        content it is itself removing in the same generation. Anything else
        -- an operator's file, a session the quiesce step failed to stop, a
        provider cache nobody declared -- fails the generation closed, before
        `prepare()` has touched a single target, and the operator decides
        what it was.
        """
        try:
            info = target.lstat()
        except FileNotFoundError:
            return BackupRecord(
                spec.target,
                False,
                None,
                None,
                None,
                None,
                None,
                "",
                "",
                0,
                0,
                0,
                remove=True,
                directory=True,
            )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(
                f"worker-only directory target is not a directory: {target}"
            )
        expected = {
            PurePosixPath(other).name
            for other in removed
            if str(PurePosixPath(other).parent) == spec.target
        }
        unexpected = sorted(set(os.listdir(target)) - expected)
        if unexpected:
            raise RuntimeError(
                f"unexpected content under worker-only directory {target}: "
                f"{unexpected}"
            )
        return BackupRecord(
            spec.target,
            True,
            None,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            None,
            "",
            "",
            0,
            0,
            0,
            remove=True,
            directory=True,
        )

    def _pending_manifest(self) -> Path:
        value = _trusted_journal(self.pending)
        manifest = Path(value["manifest"]).resolve(strict=True)
        if (
            not manifest.is_relative_to(self.state_dir)
            or manifest.name != "manifest.json"
        ):
            raise RuntimeError("pending transaction manifest escaped state directory")
        return manifest

    def _assert_removal_snapshot(self, record: BackupRecord) -> Path:
        """Refuse unless the purge target is still exactly what prepare() saw.

        A removal is undoable only through the snapshot this generation took,
        and that snapshot describes one specific file: content, mode, owning
        uid and gid, or -- for a link -- its literal target. Unlinking
        anything else would destroy a file this transaction never examined
        and could not put back, which is the one mutation no rollback here
        can honour. So drift between prepare() and apply() is refused rather
        than absorbed: the generation fails closed with the target intact.
        """
        target = self._target(record.target)
        if not record.existed:
            # prepare() recorded absence, so there is no backup to restore. A
            # file that appeared since belongs to something other than this
            # transaction and is not ours to delete.
            if _path_present(target):
                raise RuntimeError(f"purge target appeared after prepare: {target}")
            return target
        try:
            parent_fd = _open_directory_chain(target.parent, create=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"purge target disappeared before removal: {target}"
            ) from exc
        try:
            self._assert_removal_at(record, parent_fd, target.name, target)
        finally:
            os.close(parent_fd)
        return target

    def _assert_removal_at(
        self, record: BackupRecord, parent_fd: int, name: str, target: Path
    ) -> None:
        """`_assert_removal_snapshot`'s comparison, bound to a pinned parent.

        `name` is resolved against `parent_fd` alone -- no path is walked, so
        no component of one can be swapped between this check and whatever
        the caller does next. `target` is carried only to name the real path
        in an error; the object compared is whatever `name` denotes.
        """
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"purge target disappeared before removal: {target}"
            ) from exc
        if record.original_symlink is not None:
            # Compared against the link itself, never what it resolves to:
            # `_read_regular` opens O_NOFOLLOW and would reject the link.
            if not stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"purge target is no longer a symlink: {target}")
            if os.readlink(name, dir_fd=parent_fd) != record.original_symlink:
                raise RuntimeError(
                    f"purge target symlink changed before removal: {target}"
                )
            return
        assert record.original_sha256 is not None
        assert record.original_mode is not None
        assert record.original_uid is not None
        assert record.original_gid is not None
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"purge target shape changed before removal: {target}")
        try:
            current = _read_regular(name, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError(
                f"purge target shape changed before removal: {target}"
            ) from exc
        if not _matches(
            current,
            record.original_sha256,
            record.original_mode,
            record.original_uid,
            record.original_gid,
        ):
            raise RuntimeError(
                f"purge target changed before compare-and-remove: {target}"
            )

    @staticmethod
    def _release_quarantine(parent_fd: int, quarantine: str, target: Path) -> None:
        """Put a quarantined object back under its own name, replacing nothing.

        `link` rather than `rename`: this process emptied the original name,
        so it is empty unless something else has since claimed it -- and that
        something belongs to whoever created it. EEXIST therefore keeps both
        objects, leaving the quarantine entry in place and naming it in the
        error, instead of silently destroying one of them. Either way the
        purge target's bytes still exist under some name in its own
        directory: refusing a removal never costs data.
        """
        try:
            os.link(
                quarantine,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RuntimeError(
                f"purge target could not be put back and is retained at "
                f"{target.parent / quarantine}: {target}"
            ) from exc
        os.unlink(quarantine, dir_fd=parent_fd)
        os.fsync(parent_fd)

    def _apply_removal(self, record: BackupRecord) -> None:
        """Destroy one purge target, with nothing between compare and destroy.

        Comparing the path and then unlinking the path are two resolutions of
        the same name, and an untrusted writer of the directory only has to
        win the gap between them to have the installer -- running as root --
        destroy a file it never examined. No ordering of those two operations
        closes that: the second one always resolves the name again
        (independent review on 9eb07f8).

        So the name is resolved exactly once, and by a rename: the object is
        moved to a quarantine entry in its own directory whose name is
        unpredictable and whose parent is a descriptor pinned before the
        rename. From that point the pathname is out of the picture. The
        comparison that authorises destruction runs against the quarantined
        object, and the unlink destroys that same entry -- there is no name
        left for anyone to interpose on.

        If the object that lands in quarantine is NOT what `prepare()`
        snapshotted -- because the swap happened before the rename rather than
        after it -- the comparison fails and `_release_quarantine` puts it
        back under its own name without replacing anything. The generation
        fails closed, and nothing was destroyed.
        """
        target = self._target(record.target)
        if not record.existed:
            if _path_present(target):
                raise RuntimeError(f"purge target appeared after prepare: {target}")
            return
        try:
            parent_fd = _open_directory_chain(target.parent, create=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"purge target disappeared before removal: {target}"
            ) from exc
        try:
            # A pathname-shaped pre-check first: in every non-adversarial run
            # it is what produces the precise drift message, and it means the
            # quarantine below almost never has to be undone.
            self._assert_removal_at(record, parent_fd, target.name, target)
            for _attempt in range(16):
                quarantine = f".{target.name}.aicc-purge-{secrets.token_hex(8)}"
                try:
                    _rename_noreplace(
                        parent_fd, target.name, parent_fd, quarantine
                    )
                    break
                except FileExistsError:
                    continue
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"purge target disappeared before removal: {target}"
                    ) from exc
            else:
                raise RuntimeError(
                    f"cannot allocate collision-free purge quarantine: {target}"
                )
            os.fsync(parent_fd)
            try:
                self._assert_removal_at(record, parent_fd, quarantine, target)
            except BaseException:
                self._release_quarantine(parent_fd, quarantine, target)
                raise
            os.unlink(quarantine, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    @staticmethod
    def _assert_directory_state(
        record: BackupRecord, info: os.stat_result, target: Path
    ) -> None:
        """The comparison that authorises destroying one directory."""
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != record.original_mode
            or info.st_uid != record.original_uid
            or info.st_gid != record.original_gid
        ):
            raise RuntimeError(
                f"purge directory changed before compare-and-remove: {target}"
            )

    def _assert_directory_removal_snapshot(self, record: BackupRecord) -> None:
        """Prove a purge directory is still the one `prepare()` recorded."""
        target = self._target(record.target)
        if not record.existed:
            if _path_present(target):
                raise RuntimeError(
                    f"purge directory appeared after prepare: {target}"
                )
            return
        try:
            info = target.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"purge directory disappeared before removal: {target}"
            ) from exc
        self._assert_directory_state(record, info, target)

    @staticmethod
    def _release_directory_quarantine(
        parent_fd: int, quarantine: str, target: Path
    ) -> None:
        """Put a quarantined directory back under its own name, replacing nothing.

        A directory cannot be hard-linked, so the file path's `linkat` trick
        is unavailable; `renameat2(RENAME_NOREPLACE)` gives the same
        guarantee. A plain `rename` would succeed against an empty directory
        someone else created at that name in the meantime and destroy it, and
        this runs on the failure path of a removal that was already refused
        -- the last place to start deleting objects nobody examined.
        """
        try:
            _rename_noreplace(parent_fd, quarantine, parent_fd, target.name)
        except OSError as exc:
            raise RuntimeError(
                f"purge directory could not be put back and is retained at "
                f"{target.parent / quarantine}: {target}"
            ) from exc
        os.fsync(parent_fd)

    def _apply_directory_removal(self, record: BackupRecord) -> None:
        """Remove one worker-only directory once this generation emptied it.

        `rmdir` refuses a non-empty directory, so it is its own guard against
        destroying content nobody accounted for -- but it is not a guard
        against destroying the wrong *object*. Stat a name and then `rmdir`
        the same name and the kernel resolves that name twice: a writer of
        the parent that wins the gap has the installer, running as root,
        remove an empty directory it never examined. `/run/aicc-agent-homes`
        and `/run/aicc-agent-workspace-binds` sit under a parent the launcher
        writes, so that parent is influenceable and the gap is real
        (independent review on 0a205a0).

        So this takes the same shape as `_apply_removal`, bound to an inode
        rather than to a name: the identity is captured, the directory is
        renamed once into an unpredictable quarantine entry under a pinned
        parent descriptor, and the object destroyed is then proven -- by
        st_dev/st_ino, and again by mode and owner -- to be that same inode.
        Anything else, including a directory that refilled since `prepare()`,
        is put back by `_release_directory_quarantine` and fails closed.
        """
        target = self._target(record.target)
        if not record.existed:
            if _path_present(target):
                raise RuntimeError(
                    f"purge directory appeared after prepare: {target}"
                )
            return
        try:
            parent_fd = _open_directory_chain(target.parent, create=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"purge directory disappeared before removal: {target}"
            ) from exc
        try:
            try:
                info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"purge directory disappeared before removal: {target}"
                ) from exc
            self._assert_directory_state(record, info, target)
            for _attempt in range(16):
                quarantine = f".{target.name}.aicc-purge-{secrets.token_hex(8)}"
                try:
                    _rename_noreplace(
                        parent_fd, target.name, parent_fd, quarantine
                    )
                    break
                except FileExistsError:
                    continue
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"purge directory disappeared before removal: {target}"
                    ) from exc
            else:
                raise RuntimeError(
                    f"cannot allocate collision-free purge quarantine: {target}"
                )
            os.fsync(parent_fd)
            try:
                held = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
                if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError(
                        f"purge directory changed before compare-and-remove: "
                        f"{target}"
                    )
                self._assert_directory_state(record, held, target)
                try:
                    os.rmdir(quarantine, dir_fd=parent_fd)
                except OSError as exc:
                    raise RuntimeError(
                        f"worker-only directory is not empty at removal: {target}"
                    ) from exc
            except BaseException:
                self._release_directory_quarantine(parent_fd, quarantine, target)
                raise
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def apply(self) -> None:
        """Apply one prepared generation; recovery stays armed until commit."""
        manifest = self._pending_manifest()
        _verify_generation_control_authority(self.state_dir, manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = _generation_records(payload)
        # Prove every purge target still matches its snapshot before the first
        # mutation of any record. The per-record check below is the one that
        # governs the unlink, but refusing here means a drifted target fails
        # the generation with the host bit-for-bit untouched, instead of after
        # half the installs have been written and must be rolled back.
        for record in records:
            if record.remove and record.directory:
                self._assert_directory_removal_snapshot(record)
            elif record.remove:
                self._assert_removal_snapshot(record)
        try:
            for index, record in enumerate(records):
                # Write-ahead index makes every destination mutation recoverable.
                self._write_journal(manifest, "APPLYING", index)
                if record.remove and record.directory:
                    self._apply_directory_removal(record)
                    continue
                if record.remove:
                    self._apply_removal(record)
                    continue
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
            # A synchronous apply failure has the same durability guarantee as
            # a later `recover()` after SIGKILL, but it must not leave a host in
            # a known half-applied state merely because the caller is still
            # alive. Undo immediately. If comparison proves rollback unsafe,
            # `restore()` leaves pending.json armed for boot/operator recovery.
            try:
                self.restore(manifest)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "generation apply failed and immediate rollback failed; "
                    "durable recovery remains armed"
                ) from rollback_error
            self._remove_orphan_generations()
            raise

    def commit(self) -> None:
        """Publish an applied generation only after service rollout succeeds."""
        manifest = self._pending_manifest()
        _verify_generation_control_authority(self.state_dir, manifest)
        journal = _trusted_journal(self.pending)
        if journal.get("phase") != "APPLIED":
            raise RuntimeError("only a fully applied generation can be committed")
        # Armed while the main WAL still says APPLIED, and durably, BEFORE
        # anything writes COMMITTING. Arming after it inverted the guarantee:
        # a failure inside the arming step left a generation whose journal
        # said "finish this commit" and whose intent to destroy the
        # credentials had never been written, so `recover()` completed the
        # commit and the control host kept both secrets in its backups
        # (independent review on 0a205a0). From APPLIED the ordinary rollback
        # is still available, so a failure here costs nothing: `recover()`
        # unwinds the generation, puts both credentials back byte-for-byte,
        # and discards the intent along with the generation it named.
        self._arm_sensitive_retirement(manifest)
        self._write_journal(manifest, "COMMITTING", journal.get("next_index", 0))
        _atomic_bytes(
            self.current,
            json.dumps({"manifest": str(manifest)}, sort_keys=True).encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
        # The revocation becomes terminal here, one step BEFORE pending.json
        # is consumed: while that journal exists `recover()` is still the
        # authority on this generation, and it finalises or restores the
        # membership to match whichever way it resolves the generation.
        finalize_authority_membership(self.state_dir, manifest)
        self.pending_release.unlink(missing_ok=True)
        _fsync_dir(self.state_dir)
        # Keep the primary WAL until every auxiliary forward action is
        # terminal. A crash during retirement must still boot the exact
        # generation capsule rather than leave an auxiliary-only journal.
        # Keep the auxiliary intent until the primary pending WAL is gone.
        # If we crash after redaction, COMMITTING recovery still has the
        # durable authority it requires and can repeat the idempotent work.
        self._run_sensitive_retirement(consume_intent=False)
        self.pending.unlink()
        _fsync_dir(self.state_dir)
        self._run_sensitive_retirement()
        # The snapshot is spent once committed; leaving it at the fixed path
        # lets a later recover() apply a stale snapshot against a different
        # generation (review on d8920b6).
        (self.state_dir / "attempt-units.json").unlink(missing_ok=True)
        _fsync_dir(self.state_dir)

    def _arm_sensitive_retirement(self, manifest: Path) -> None:
        """Journal the intent to destroy this generation's secret copies."""
        targets = _sensitive_removal_targets(manifest)
        if not targets:
            return
        _atomic_bytes(
            self.state_dir / SENSITIVE_RETIREMENT_JOURNAL,
            (
                json.dumps(
                    {
                        "version": SENSITIVE_RETIREMENT_VERSION,
                        "generation": manifest.parent.name,
                        "manifest": str(manifest),
                        "targets": sorted(targets),
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
            0o600,
            os.geteuid(),
            os.getegid(),
        )
        _fsync_dir(self.state_dir)

    def _sensitive_retirement_intent(self) -> dict[str, object] | None:
        """The armed intent, validated, or None when nothing is armed."""
        journal = self.state_dir / SENSITIVE_RETIREMENT_JOURNAL
        if not _path_present(journal):
            return None
        payload = _trusted_journal(journal)
        targets = payload.get("targets")
        if payload.get("version") == 1:
            generation = payload.get("generation")
            if (
                set(payload) != {"version", "generation", "targets"}
                or not isinstance(generation, str)
                or not re.fullmatch(r"generation-[0-9a-f]{16}", generation)
                or not isinstance(targets, list)
                or not targets
                or any(not isinstance(target, str) for target in targets)
                or not set(targets) <= SENSITIVE_TARGETS
            ):
                raise RuntimeError("sensitive retirement journal is invalid")
            manifest = self.state_dir / generation / "manifest.json"
            _trusted_journal(manifest)
            expected = _sensitive_removal_targets(manifest)
            if not set(targets) <= expected:
                raise RuntimeError("legacy sensitive retirement targets drifted")
            bound: set[str] = set()
            if _path_present(self.pending):
                bound.add(str(self._pending_manifest()))
            if _path_present(self.current):
                current = _trusted_journal(self.current).get("manifest")
                if isinstance(current, str):
                    bound.add(current)
            if str(manifest) not in bound:
                raise RuntimeError(
                    "legacy sensitive retirement journal is not bound to a live generation"
                )
            return {
                "version": SENSITIVE_RETIREMENT_VERSION,
                "generation": generation,
                "manifest": str(manifest),
                "targets": sorted(expected),
            }
        if (
            payload.get("version") != SENSITIVE_RETIREMENT_VERSION
            or set(payload) != {"version", "generation", "manifest", "targets"}
            or not isinstance(payload.get("generation"), str)
            or not re.fullmatch(
                r"generation-[0-9a-f]{16}", str(payload.get("generation"))
            )
            or not isinstance(payload.get("manifest"), str)
            or not isinstance(targets, list)
            or not targets
            or any(not isinstance(target, str) for target in targets)
        ):
            raise RuntimeError("sensitive retirement journal is invalid")
        generation = str(payload["generation"])
        manifest = self.state_dir / generation / "manifest.json"
        if payload["manifest"] != str(manifest):
            raise RuntimeError("sensitive retirement journal is invalid")
        _trusted_journal(manifest)
        expected = _sensitive_removal_targets(manifest)
        supplied = frozenset(targets)
        if (
            len(supplied) != len(targets)
            or not supplied <= SENSITIVE_TARGETS
            or supplied != expected
        ):
            raise RuntimeError("sensitive retirement targets drifted")
        return {
            "version": SENSITIVE_RETIREMENT_VERSION,
            "generation": generation,
            "manifest": str(manifest),
            "targets": sorted(expected),
        }

    def _require_sensitive_retirement_intent(self, manifest: Path) -> None:
        """Refuse to finish a control-sensitive commit without its intent.

        The COMMITTING phase is reached only after `commit()` has armed, so a
        generation that removes a credential and has no bound intent is a
        state this code cannot produce: either the arming was lost, or this
        journal belongs to a different generation. Both mean the destruction
        nobody recorded would silently never happen, leaving a committed
        control host holding the secret -- so recovery stops and says which.
        """
        expected = _sensitive_removal_targets(manifest)
        intent = self._sensitive_retirement_intent()
        if intent is None:
            if not expected:
                return
            raise RuntimeError(
                "sensitive retirement intent is missing for a committing "
                f"generation: {sorted(expected)}"
            )
        if intent["manifest"] != str(manifest):
            raise RuntimeError(
                "sensitive retirement journal is bound to another generation: "
                f"{intent['generation']}"
            )
        if frozenset(intent["targets"]) != expected:
            raise RuntimeError(
                "sensitive retirement intent does not match its generation: "
                f"{sorted(expected)}"
            )

    def _discard_sensitive_retirement(self, manifest: Path) -> None:
        """Drop the intent armed for a generation that is being rolled back.

        The rollback puts every purged credential back from the backups the
        intent would have destroyed, so the intent is void -- but only for
        the generation it names. One bound to any other generation is
        unexplained state, and destroying secrets on unexplained state, or
        silently dropping an intent that still applies, are both worse than
        stopping.
        """
        intent = self._sensitive_retirement_intent()
        if intent is None:
            return
        if intent["manifest"] != str(manifest):
            raise RuntimeError(
                "sensitive retirement journal is bound to another generation: "
                f"{intent['generation']}"
            )
        (self.state_dir / SENSITIVE_RETIREMENT_JOURNAL).unlink()
        _fsync_dir(self.state_dir)

    def _preflight_sensitive_retirement(
        self,
    ) -> tuple[frozenset[str], list[Path]] | None:
        """Validate the complete retirement set without mutating any state."""
        intent = self._sensitive_retirement_intent()
        if intent is None:
            return None
        committed = None
        if _path_present(self.current):
            committed = _trusted_journal(self.current).get("manifest")
        if intent["manifest"] != committed:
            raise RuntimeError(
                "sensitive retirement journal is not bound to the committed "
                f"generation: {intent['generation']}"
            )
        retired = frozenset(intent["targets"])
        manifests = sorted(self.state_dir.glob("generation-*/manifest.json"))
        for manifest in manifests:
            _preflight_sensitive_records(self.state_dir, manifest, retired)
        return retired, manifests

    def _run_sensitive_retirement(self, *, consume_intent: bool = True) -> None:
        """Destroy every reachable copy of a credential this host purged.

        A removal is reversible only because `prepare()` copied the target
        into the generation's backups first -- which means that for the two
        model credentials, the price of a rollback boundary is a second copy
        of the secret, in a root-owned directory, on the host that exists to
        not hold it. Keeping it after the control generation commits would
        make "the control plane holds no agent credentials" false in the one
        place nobody looks.

        So the removal of a sensitive target is finalised rather than merely
        committed. Once the generation is terminal this destroys the backup
        blob, and every older copy of the same target anywhere in this state
        directory -- a previous worker generation staged those bytes as well
        as backing them up -- and redacts the records that pointed at them.

        The redaction is explicit, not silent. `sensitive_retired` stays on
        the record, so the journal still says a secret was here and was
        deliberately destroyed, and `restore()` refuses that record by name
        instead of quietly doing nothing. `uninstall_all` is the one caller
        that accepts it, because unwinding an installation cannot mean
        resurrecting a credential the operator had removed on purpose.

        Idempotent, and driven by a durable journal: a crash at any point
        re-runs it from `recover()`.

        Destroying is bound to the generation the intent names becoming the
        LIVE one. Arming now happens before the WAL says COMMITTING, so an
        intent can also outlive a generation that never committed -- and for
        that one the credentials are back on the host and its backups are
        what put them there. `recover()` discards such an intent explicitly
        as part of the rollback; reaching here with one still bound to a
        generation that is not live is unexplained, and refused.
        """
        plan = self._preflight_sensitive_retirement()
        if plan is None:
            return
        retired, manifests = plan
        for manifest in manifests:
            _redact_sensitive_records(manifest, retired)
        if consume_intent:
            (self.state_dir / SENSITIVE_RETIREMENT_JOURNAL).unlink()
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
        """Idempotently roll back an interrupted prepared/applying generation.

        Also the single place the two auxiliary journals are resolved, in the
        same direction as the generation they belong to. A rollback restores
        the publisher-group memberships the control transition revoked; a
        finished commit finalises them and runs the sensitive-removal
        retirement it armed. Neither is a separate transaction the operator
        has to know about: the installer's rollback trap already calls this,
        and so does the boot recovery capsule.
        """
        if not _path_present(self.pending):
            if _path_present(self.pending_release):
                raise RuntimeError(
                    "pending release selector exists without install journal"
                )
            # Prevalidate every auxiliary WAL and bind them to the same live
            # generation before the first cleanup or secret mutation.
            current_manifest: Path | None = None
            if _path_present(self.current):
                value = _trusted_journal(self.current).get("manifest")
                if not isinstance(value, str):
                    raise RuntimeError("current generation pointer is invalid")
                current_manifest = Path(value)
            retirement = self._sensitive_retirement_intent()
            membership: dict[str, object] | None = None
            if _path_present(self.state_dir / AUTHORITY_MEMBERSHIP_JOURNAL):
                membership = _authority_membership_journal(self.state_dir)
                _authority_membership_bound(membership, current_manifest)
            if retirement is not None:
                if current_manifest is None or retirement["manifest"] != str(
                    current_manifest
                ):
                    raise RuntimeError(
                        "sensitive retirement journal is not bound to current generation"
                    )
                if membership is not None and membership["manifest"] != retirement["manifest"]:
                    raise RuntimeError("auxiliary journals disagree on generation")
            # Prove every historical secret path before even inert cleanup.
            # In particular, orphan removal must never delete a generation
            # whose retained secret journal has not passed global preflight.
            self._preflight_sensitive_retirement()
            # A crash after a completed recovery can leave only the inert
            # fixed-name snapshot. With no governing WAL it has no authority
            # and must not bleed into the next transaction.
            snapshot = self.state_dir / "attempt-units.json"
            if _path_present(snapshot):
                _read_regular(snapshot, max_bytes=4 * 1024 * 1024)
                snapshot.unlink()
                _fsync_dir(self.state_dir)
            # A crash between commit()'s last unlink and its retirement call
            # leaves the armed journal and nothing else; there is no pending
            # generation to roll back, so the only thing outstanding is
            # finishing that destruction.
            self._resolve_authority_membership()
            self._run_sensitive_retirement()
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
            _verify_generation_control_authority(self.state_dir, manifest)
            # commit() already durably published current.json but crashed
            # before unlinking pending.json. Restoring here would silently
            # revert a completed, live installation on the next boot
            # (independent-review finding on 8a881d3): finish the commit
            # instead of undoing it.
            #
            # Finishing it means finishing ALL of it. A generation that
            # removes a credential is committed on the promise that the
            # copies it made get destroyed, so the bound intent is proven
            # here -- before current.json is republished -- rather than
            # letting a missing one turn the destruction into a silent no-op.
            self._require_sensitive_retirement_intent(manifest)
            _atomic_bytes(
                self.current,
                json.dumps({"manifest": str(manifest)}, sort_keys=True).encode(),
                0o600,
                os.geteuid(),
                os.getegid(),
            )
            self.pending_release.unlink(missing_ok=True)
            _fsync_dir(self.state_dir)
            # The generation is live, so the revocation it made is terminal
            # and the secrets it purged must not survive in its backups.
            finalize_authority_membership(self.state_dir, manifest)
            # Do not consume the intent while pending.json still says that
            # recovery owns this COMMITTING generation.  A crash between the
            # two unlinks must retain enough authority for the next boot.
            self._run_sensitive_retirement(consume_intent=False)
            self.pending.unlink()
            _fsync_dir(self.state_dir)
            self._run_sensitive_retirement()
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
            verify_service_snapshot_closure(
                snapshot, preserve_unsnapshotted_launchers=True
            )
            quiesce_service_snapshot(
                snapshot, restorable_units=_units_restored_by(manifest)
            )
            verify_service_snapshot_closure(
                snapshot, preserve_unsnapshotted_launchers=True
            )
        elif self.root == Path("/"):
            raise RuntimeError("production recovery requires a service snapshot")
        # The rollback puts every purged credential back from the very
        # backups a still-armed intent would destroy, so the intent dies with
        # the generation that armed it -- before restore() reads those
        # backups, not after.
        self._discard_sensitive_retirement(manifest)
        self.restore(manifest, clear_pending=False)
        self._restore_release_selector()
        # Before a single unit is started, and after the files are back. The
        # worker units this rollback is about to start read
        # /etc/aicc/workspace-authority.env as members of the publisher
        # group; starting them first meant every one of them acquired its
        # supplementary groups WITHOUT that membership and then failed on the
        # authority key -- and a process cannot be given a group after the
        # fact, so the rollback ended with the files restored and the service
        # unable to use them (independent review on 0a205a0). A failure here
        # keeps the durable journal and reports an incomplete rollback rather
        # than starting units into a boundary this transaction moved.
        restore_legacy_authority_membership(self.state_dir, manifest=manifest)
        if snapshot_present:
            if boot:
                restore_service_snapshot(snapshot, defer_starts=True)
            else:
                restore_service_snapshot(snapshot)
            verify_service_snapshot_closure(
                snapshot, preserve_unsnapshotted_launchers=True
            )
        self._clear_pending(manifest)
        if snapshot_present:
            snapshot.unlink()
            _fsync_dir(self.state_dir)
        shutil.rmtree(transaction)
        self._remove_orphan_generations()
        _fsync_dir(self.state_dir)

    def _resolve_authority_membership(self) -> None:
        """Finish a membership journal no WAL governs any more.

        With no pending generation the direction is decided by the one thing
        that is still true: whether the generation the journal names is the
        live one. If it is, the revocation it made is part of what is live
        and the journal is spent; if it is not, that generation was rolled
        back and the memberships go back with it. Resolving it in a fixed
        direction instead -- as this did, always restoring -- puts two
        worker-era principals back into the publisher group of a committed
        control host on the next boot.
        """
        journal = self.state_dir / AUTHORITY_MEMBERSHIP_JOURNAL
        if not _path_present(journal):
            return
        payload = _authority_membership_journal(self.state_dir)
        manifest = Path(str(payload["manifest"]))
        committed = None
        if _path_present(self.current):
            committed = json.loads(self.current.read_text(encoding="utf-8")).get(
                "manifest"
            )
        if payload["manifest"] == committed:
            finalize_authority_membership(self.state_dir, manifest)
            return
        restore_legacy_authority_membership(self.state_dir, manifest=manifest)

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
        self,
        manifest: Path | None = None,
        *,
        clear_pending: bool = True,
        allow_retired_sensitive: bool = False,
    ) -> None:
        """Undo one generation, or refuse where undoing it is not possible.

        `allow_retired_sensitive` is the caller stating that a credential
        whose backup the commit destroyed is not expected back. Only
        `uninstall_all` says it: unwinding an installation means leaving the
        host without AICC state, and a credential the control profile removed
        on purpose is exactly that. Every other caller -- above all
        `recover()`, which rolls back generations that have NOT committed and
        therefore still have their backups -- leaves it false and gets a named
        refusal rather than a rollback that silently skipped a record.
        """
        if manifest is None:
            current = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(current["manifest"])
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = _generation_records(payload)
        if allow_retired_sensitive:
            # This is a preflight, deliberately before the reversed restore
            # loop: discovering a recreated credential after restoring another
            # record would make a fail-closed refusal partially destructive.
            self._assert_retired_sensitive_targets_absent(records)
        removed_targets = [
            PurePosixPath(record.target) for record in records if record.remove
        ]
        removed_children = {
            record.target: frozenset(
                target.name
                for target in removed_targets
                if str(target.parent) == record.target
            )
            for record in records
            if record.remove and record.directory
        }
        for record in reversed(records):
            target = self._target(record.target)
            if record.sensitive_retired:
                # The backup this record would restore from was destroyed
                # when the generation committed. See
                # `_run_sensitive_retirement`.
                if allow_retired_sensitive:
                    continue
                raise RuntimeError(
                    "sensitive backup was retired at commit and this "
                    f"generation cannot be rolled back: {record.target}"
                )
            if (
                record.target == RECOVERY_ANCHOR_TARGET
                and _path_present(self.state_dir / "uninstall.json")
            ):
                # Historical generations treated the generator as reversible.
                # Preserve the permanent anchor until the uninstall WAL is
                # durably gone; without it a reboot could bypass recovery.
                continue
            if record.remove and record.directory:
                self._restore_removed_directory(
                    record,
                    target,
                    expected_entries=removed_children[record.target],
                )
                continue
            if record.remove:
                self._restore_removed(record, target)
                continue
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

    def _assert_retired_sensitive_targets_absent(
        self, records: list[BackupRecord]
    ) -> None:
        """Preflight the one irreversible exception accepted by uninstall."""
        for record in records:
            if record.sensitive_retired and _path_present(
                self._target(record.target)
            ):
                raise RuntimeError(
                    "retired sensitive target reappeared before uninstall: "
                    f"{record.target}"
                )

    def _assert_uninstall_chain_retired_targets_absent(self) -> None:
        """Walk every committed generation before unwinding the first one."""
        # Reuse the canonical traversal: it proves containment in state_dir
        # and rejects cycles before this security preflight reads manifests.
        for manifest in self._current_generation_manifests():
            payload = _trusted_journal(manifest)
            self._assert_retired_sensitive_targets_absent(
                _generation_records(payload)
            )

    def _restore_removed_directory(
        self,
        record: BackupRecord,
        target: Path,
        *,
        expected_entries: frozenset[str],
    ) -> None:
        """Recreate a purged worker-only directory, empty and exactly as it was.

        Empty is the whole reason this is reversible: `prepare()` refused the
        generation unless the directory held nothing but the entries this
        same generation removes, and `restore()` walks the records in reverse,
        so the directory is back before the files that belonged in it are.
        """
        if not record.existed:
            if _path_present(target):
                raise RuntimeError(
                    f"generation target appeared where absence was recorded: {target}"
                )
            return
        assert record.original_mode is not None
        assert record.original_uid is not None
        assert record.original_gid is not None
        parent_fd = _open_directory_chain(target.parent, create=False)
        try:
            try:
                info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(target.name, record.original_mode, dir_fd=parent_fd)
                # mkdir's mode is masked by the caller's umask; the restored
                # directory must be what the snapshot recorded, not what the
                # process happened to inherit.
                os.chmod(target.name, record.original_mode, dir_fd=parent_fd)
                os.chown(
                    target.name,
                    record.original_uid,
                    record.original_gid,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                os.fsync(parent_fd)
                return
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != record.original_mode
                or info.st_uid != record.original_uid
                or info.st_gid != record.original_gid
            ):
                raise RuntimeError(
                    f"generation target directory changed before restore: {target}"
                )
            directory_fd = os.open(target.name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
            try:
                opened = os.fstat(directory_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError(
                        f"generation target directory changed before restore: {target}"
                    )
                unexpected = sorted(
                    set(os.listdir(directory_fd)) - expected_entries
                )
                if unexpected:
                    raise RuntimeError(
                        "generation target directory gained unexpected content "
                        f"before restore: {target}: {unexpected}"
                    )
            finally:
                os.close(directory_fd)
            # Unchanged and still containing only names this generation owns:
            # apply() has not reached this record, or a previous restore put
            # it back. Missing expected names are restored by their own records.
        finally:
            os.close(parent_fd)

    def _restore_removed(self, record: BackupRecord, target: Path) -> None:
        """Undo a removal record: put back exactly what a purge took away.

        The mirror image of the two branches above it: an ordinary record's
        "installed" state is specific content, so restore proceeds once the
        target no longer matches it. A removal record's installed state is
        *absence*, so every check here runs in the opposite direction.
        """
        if not record.existed:
            # Nothing existed before this generation touched the target, so
            # absence -- whether apply() ran or not -- is already correct.
            if _path_present(target):
                raise RuntimeError(
                    f"generation target appeared where absence was recorded: {target}"
                )
            return
        if record.original_symlink is not None:
            if target.is_symlink():
                if os.readlink(target) == record.original_symlink:
                    return  # apply() has not run yet
                raise RuntimeError(
                    f"generation target is a different symlink before restore: {target}"
                )
            if _path_present(target):
                raise RuntimeError(
                    f"generation target reappeared before restore: {target}"
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
            return
        assert record.backup is not None
        assert record.original_mode is not None
        assert record.original_uid is not None
        assert record.original_gid is not None
        assert record.original_sha256 is not None
        try:
            current = _read_regular(target)
        except FileNotFoundError:
            current = None
        except OSError as exc:
            raise RuntimeError(
                f"generation target shape changed before restore: {target}"
            ) from exc
        if current is not None:
            if _matches(
                current,
                record.original_sha256,
                record.original_mode,
                record.original_uid,
                record.original_gid,
            ):
                return  # apply() has not run yet
            raise RuntimeError(
                f"generation target changed before compare-and-restore: {target}"
            )
        backup = _read_regular(Path(record.backup))
        if not _matches(
            backup, record.original_sha256, 0o600, os.geteuid(), os.getegid()
        ):
            raise RuntimeError(f"generation backup SHA drifted: {target}")
        _atomic_bytes(
            target,
            backup.payload,
            record.original_mode,
            record.original_uid,
            record.original_gid,
        )

    def uninstall_all(self, *, boot: bool = False) -> None:
        """Unwind every installed generation to the original pre-install state.

        The one thing it cannot unwind is a credential removal the control
        profile finalised: those bytes were destroyed deliberately and are
        not recoverable from anything this host still holds. Uninstalling
        therefore leaves those targets absent -- which is also what
        uninstalling is for -- rather than wedging on a restore no host
        could perform.
        """
        self.recover(boot=boot)
        if self.current.exists():
            # Do not unwind or delete a newer generation before discovering a
            # recreated retired credential in an older one.
            self._assert_uninstall_chain_retired_targets_absent()
        while self.current.exists():
            value = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(value["manifest"]).resolve(strict=True)
            transaction = manifest.parent
            self.restore(manifest, allow_retired_sensitive=True)
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
        try:
            _rename_noreplace(root_fd, staging.name, root_fd, release_id)
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                raise ReleaseRefused(
                    "kernel lacks atomic no-replace release publication"
                ) from exc
            if exc.errno == errno.EEXIST:
                raise ReleaseRefused("release destination already exists") from exc
            raise ReleaseRefused(
                f"atomic release publication failed: errno {exc.errno}"
            ) from exc
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

#: Directories that exist only because the agent layer was installed, in the
#: order `apply()` removes them: every child before its parent, and all of
#: them after the file removals that empty them.
#:
#: `/run/aicc-agent-workspace-binds` is where the launcher stages the bind
#: mounts that give an agent its task workspace, plus one journal per staged
#: bind (`ops/aicc_agent_launcher.py`). It is created by the launcher rather
#: than by tmpfiles, which is exactly why it was missed: nothing in
#: deploy/tmpfiles.d names it, so a converted host kept a directory whose
#: entries name the workspaces an agent had open (independent review on
#: 0a205a0). Like every other entry here it is removed only if this
#: generation finds it empty -- a leftover staged bind, or a mount still
#: attached, fails the generation closed rather than being unlinked out from
#: under whatever holds it.
#:
#: Deliberately NOT here: `/srv/aicc-workspaces` and `/srv/aicc-quarantine`.
#: Those hold task working trees and quarantined output -- operator data that
#: happens to have been created by the agent layer, not artefacts of it. A
#: profile change is not a licence to delete work.
WORKER_ONLY_DIRECTORIES = (
    "/var/lib/aicc-agent/claude/.claude",
    "/var/lib/aicc-agent/claude",
    "/var/lib/aicc-agent/codex/.codex",
    "/var/lib/aicc-agent/codex",
    "/var/lib/aicc-agent",
    "/run/aicc-agent-homes",
    "/run/aicc-agent-workspace-binds",
    "/run/aicc-agent-launcher/active",
    "/run/aicc-agent-launcher",
    "/etc/systemd/system/voyn-aicc-worker@.service.d",
    "/etc/systemd/system/aicc-worker.service.d",
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

    `control` neither installs nor writes `WORKER_ONLY_TARGETS`, and also
    purges each one with an explicit `removal_spec` in this same generation.
    Before this existed there was one profile for every host, and it demanded
    the agent's Claude and Codex credentials unconditionally: installing the
    control plane meant either placing agent secrets on a host that must
    never hold them, or not installing it at all. The live attempt on
    control-01 took the second branch and stopped at `source is not a safe
    regular file: <legacy-home>/.claude/.credentials.json` -- a file whose
    *absence* was correct (2026-08-31). Dropping a target from the spec list
    only stops this transaction from writing it; it does nothing about a
    worker-only file a previous worker (or default) install already left on
    disk, which is exactly what independent review caught next -- so control
    always pairs the drop with a removal, whether or not the target
    currently exists. A host that never ran the worker profile purges
    nothing; a host that did gets its worker artefacts (including both agent
    credential files) removed atomically with the control install itself,
    not as a separate step that could commit while the other fails.

    The control-plane's own units (planner, review, merge, reaper, rotation)
    are not added here: they are still symlinks into the operator's home and
    become repo-owned under VOYN-W0-AICC-CONTROL-PLANE-REPO-OWNED-UNITS. This
    profile makes that installation possible; it does not pre-empt it.

    What the transition does and does not remove, stated exactly, because
    "the agent principal is absent" is a claim this cannot make:

    * Files, and the worker-only directories that held them
      (`WORKER_ONLY_DIRECTORIES`) -- removed, in this generation.
    * Authority: the control profile installs
      `/etc/aicc/workspace-authority.env` owned by `aicc-control-authority`,
      a group with no members that no worker-era process can be holding, and
      it takes `aicc-worker` and `voynadmin` out of `aicc-publisher`
      (`revoke_legacy_authority_membership`). The first is what revokes the
      key from processes that are already running -- credentials are held as
      numeric gids for the life of a process, so an `/etc/group` edit alone
      would leave a running worker reading the key until it exits. The
      second is what stops those principals acquiring the group again.
    * Unix principals -- the `aicc-agent` and `aicc-worker` users and the
      `aicc-workspace`/`aicc-agent-auth` groups -- are NOT removed. sysusers
      has no removal verb, and deleting a system account whose uid may still
      own inodes elsewhere on the host is not something an installer should
      do behind an operator's back. They are left inert: no files, no
      credentials, no group conferring access to anything this profile
      installs, and the `nologin` shells they were created with. An operator
      who wants the accounts gone removes them deliberately, with `userdel`.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown installation profile: {profile!r}")
    root_uid, root_gid = 0, 0
    # `aicc-agent` is resolved only where the agent layer is installed. That
    # identity comes from deploy/sysusers.d/aicc-agent.conf, which a control
    # host deliberately never runs (see the installer): demanding the group
    # here would fail a profile that installs nothing against it on a host
    # that was never a worker and therefore does not have it. It is used by
    # exactly one spec -- the worker-only /etc/aicc/agent.env. On a CONVERTED
    # host the group does still exist, inert, and is not resolved either; see
    # the docstring on what this profile does and does not remove.
    # The group that owns /etc/aicc/workspace-authority.env is chosen by
    # profile, and that choice IS the revocation. A worker host's authority
    # key is root:aicc-publisher, the group its workers hold. A control host
    # installs the same file owned by `aicc-control-authority`, a group
    # created for this profile with no members: a worker-era process still
    # running with the numeric aicc-publisher gid in its supplementary set
    # cannot read a file that group no longer owns, which removing it from
    # /etc/group could never accomplish (independent review on 0a205a0).
    agent_gid = (
        grp.getgrnam("aicc-agent").gr_gid
        if resolve_identities and profile != "control"
        else 0
    )
    authority_group = (
        CONTROL_AUTHORITY_GROUP if profile == "control" else AUTHORITY_GROUP
    )
    authority_gid = grp.getgrnam(authority_group).gr_gid if resolve_identities else 0
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
            authority_gid,
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
        # Dropping a target from the spec list only stops this transaction
        # from *writing* it -- it does nothing about a worker-only file a
        # previous worker (or default) install already put on disk. Pairing
        # the drop with an explicit removal spec for the same target folds
        # the purge into this same generation, so `prepare()`/`apply()`/
        # `commit()` gives it the identical atomicity and crash-recovery
        # guarantees as every ordinary install: nothing is removed unless
        # the whole control generation is proven and committed, and a
        # mid-transaction failure rolls the purge back exactly like any
        # other mutation (independent review on 5e50711 and its
        # predecessor). A host that never carried the worker profile simply
        # removes nothing -- every one of these targets is already absent.
        kept = tuple(spec for spec in specs if spec.target not in WORKER_ONLY_TARGETS)
        purge = tuple(
            removal_spec(target, sensitive=target in SENSITIVE_TARGETS)
            for target in sorted(WORKER_ONLY_TARGETS)
        )
        # Strictly after the file purges, deepest first: a directory is only
        # removable once this same generation has emptied it, and only if
        # what it held was nothing but those files.
        purge_directories = tuple(
            directory_removal_spec(target) for target in WORKER_ONLY_DIRECTORIES
        )
        return kept + purge + purge_directories
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
        "quiesce-worker-only",
        "validate-control-authority",
        "revoke-worker-authority",
    }:
        raise RuntimeError("unfinished uninstall journal blocks installation")
    if args.action == "install" and args.profile == "control":
        raise RuntimeError(
            "control profile requires the staged prepare, quiesce-worker-only, "
            "revoke-worker-authority, apply and commit path"
        )
    transaction = FileTransaction(args.root, args.state_dir)
    if args.action == "validate-control-authority":
        if args.profile != "control":
            raise RuntimeError(
                "validate-control-authority requires the control profile"
            )
        record_control_authority_precondition(args.state_dir)
        return 0
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
        if args.profile == "control":
            verify_control_authority_precondition(args.state_dir)
        transaction.prepare(specs)
    elif args.action == "apply":
        if args.profile == "control":
            verify_control_authority_precondition(
                args.state_dir, transaction._pending_manifest()
            )
        transaction.apply()
    elif args.action == "commit":
        if args.profile == "control":
            verify_control_authority_precondition(
                args.state_dir, transaction._pending_manifest()
            )
        transaction.commit()
    elif args.action == "quiesce":
        quiesce_service_snapshot(
            args.service_snapshot or args.state_dir / "attempt-units.json"
        )
    elif args.action == "quiesce-worker-only":
        # Stopping the agent layer is a control-profile step, paired with the
        # removal specs that same generation stages. Invoked under any other
        # profile it would disable units the transaction is installing.
        if args.profile != "control":
            raise RuntimeError("quiesce-worker-only requires the control profile")
        quiesce_worker_only_units()
    elif args.action == "revoke-worker-authority":
        # Same shape as quiesce-worker-only: a control-profile step, run
        # between prepare() and apply() so its journal is created while the
        # generation is still fully rollbackable. Under any other profile it
        # would strip the publisher group of the very members that profile
        # installs against.
        if args.profile != "control":
            raise RuntimeError("revoke-worker-authority requires the control profile")
        # Bound to the generation `prepare()` just wrote. Without a pending
        # generation there is nothing that could ever undo this, so there is
        # no such invocation: `_pending_manifest` refuses it.
        revoke_legacy_authority_membership(
            args.state_dir, transaction._pending_manifest()
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
            "quiesce-worker-only",
            "validate-control-authority",
            "revoke-worker-authority",
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
