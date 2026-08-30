#!/usr/bin/env python3
"""Atomic, reversible installation of the principal-isolation file set."""

from __future__ import annotations

import argparse
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


@dataclass(frozen=True)
class FileState:
    payload: bytes
    sha256: str
    mode: int
    uid: int
    gid: int


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


def _matches(state: FileState, sha256: str, mode: int, uid: int, gid: int) -> bool:
    return (
        state.sha256 == sha256
        and state.mode == mode
        and state.uid == uid
        and state.gid == gid
    )


def restore_service_snapshot(path: Path, *, run=subprocess.run) -> None:
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

    def assert_restored(unit: str, state: dict[str, bool]) -> None:
        load_rc, load_state = probe("show", unit, "--property=LoadState", "--value")
        pid_rc, main_pid = probe("show", unit, "--property=MainPID", "--value")
        _active_rc, active = probe("is-active", unit)
        _enabled_rc, enabled = probe("is-enabled", unit)
        if load_rc or pid_rc or not load_state or not main_pid:
            raise RuntimeError(f"cannot prove restored service state: {unit}")
        expected_active = state["active"]
        expected_enabled = state["enabled"]
        active_matches = (active == "active") is expected_active
        enabled_matches = (enabled == "enabled") is expected_enabled
        if state["exists"]:
            exists_matches = load_state not in {"", "not-found"}
        else:
            # The early-boot recovery process cannot synchronously stop
            # itself. It may remain loaded/active only when systemd proves
            # that this exact process is the service MainPID; its enablement
            # and every file target have already been rolled back durably.
            self_recovery = (
                unit == "aicc-principal-recovery.service"
                and active == "active"
                and main_pid == str(os.getpid())
            )
            exists_matches = load_state in {"", "not-found"} or self_recovery
            active_matches = active != "active" or self_recovery
            enabled_matches = enabled != "enabled"
        if not (exists_matches and active_matches and enabled_matches):
            raise RuntimeError(f"service snapshot did not restore exactly: {unit}")
        if active != "active" and main_pid not in {"", "0"}:
            raise RuntimeError(f"inactive restored service retains MainPID: {unit}")
        if version == 3 and state["exists"]:
            properties = state["properties"]
            for name, expected in properties.items():
                property_rc, actual = probe(
                    "show", unit, f"--property={name}", "--value"
                )
                if property_rc or actual != expected:
                    raise RuntimeError(
                        f"service snapshot property did not restore: {unit} {name}"
                    )

    systemctl("daemon-reload")
    for unit, state in validated:
        if state["exists"] is False:
            # Best-effort mutations are followed by authoritative state
            # probes. A failed command is harmless only when the desired
            # state is nevertheless proven; otherwise recover() keeps WAL
            # and the service snapshot for the next retry.
            probe("stop", unit)
            probe("disable", unit)
            assert_restored(unit, state)
            continue
        if version == 3:
            for name, expected in state["properties"].items():
                property_rc, actual = probe(
                    "show", unit, f"--property={name}", "--value"
                )
                if property_rc or actual != expected:
                    raise RuntimeError(
                        f"refusing unsafe snapshot restart: {unit} {name}"
                    )
        systemctl("enable" if state["enabled"] else "disable", unit)
        systemctl("start" if state["active"] else "stop", unit)
        assert_restored(unit, state)


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
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError(f"existing target is not a regular file: {target}")
        self._prepare_state_dir()
        if self.pending.exists():
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
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
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
        value = json.loads(self.pending.read_text(encoding="utf-8"))
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
        journal = json.loads(self.pending.read_text(encoding="utf-8"))
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
        self.pending.unlink()
        # The snapshot is spent once committed; leaving it at the fixed path
        # lets a later recover() apply a stale snapshot against a different
        # generation (review on d8920b6).
        (self.state_dir / "attempt-units.json").unlink(missing_ok=True)
        _fsync_dir(self.state_dir)

    def _restore_release_selector(self) -> None:
        if not self.pending_release.exists():
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
            temporary = current.parent / f".current-recover-{os.getpid()}"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(selector)
            os.replace(temporary, current)
        self.pending_release.unlink()
        _fsync_dir(current.parent)
        _fsync_dir(self.state_dir)

    def install(self, specs: Iterable[FileSpec]) -> None:
        self.prepare(specs)
        self.apply()
        self.commit()

    def recover(self) -> None:
        """Idempotently roll back an interrupted prepared/applying generation."""
        if not self.pending.exists():
            self._restore_release_selector()
            self._remove_orphan_generations()
            return
        manifest = self._pending_manifest()
        transaction = manifest.parent
        journal = json.loads(self.pending.read_text(encoding="utf-8"))
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
            self.pending.unlink()
            self.pending_release.unlink(missing_ok=True)
            # The interrupted commit's service snapshot is spent: leaving it
            # at the fixed path lets a LATER recover() apply it against a
            # different generation (review on 0f4d77e).
            (self.state_dir / "attempt-units.json").unlink(missing_ok=True)
            self._remove_orphan_generations()
            _fsync_dir(self.state_dir)
            return
        self.restore(manifest, clear_pending=False)
        self._restore_release_selector()
        restore_service_snapshot(self.state_dir / "attempt-units.json")
        self._clear_pending(manifest)
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
        if not self.pending.exists():
            return
        pending = json.loads(self.pending.read_text(encoding="utf-8"))
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
            target = self._target(record.target)
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

    def uninstall_all(self) -> None:
        """Unwind every installed generation to the original pre-install state."""
        self.recover()
        while self.current.exists():
            value = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(value["manifest"]).resolve(strict=True)
            transaction = manifest.parent
            self.restore(manifest)
            shutil.rmtree(transaction)
            _fsync_dir(self.state_dir)
        self._remove_orphan_generations()


RELEASE_MANIFEST_VERSION = 1
RELEASE_ID_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_TREE_ENTRY_RE = re.compile(
    rb"([0-7]{6}) (blob|commit) ([0-9a-f]{40})\t(.+)", re.DOTALL
)


class ReleaseRefused(RuntimeError):
    """A staged or pre-existing immutable release could not be proven."""


def _git_blob_oid(payload: bytes) -> str:
    return hashlib.sha1(  # noqa: S324 - Git object identity, not a security digest
        f"blob {len(payload)}\0".encode("ascii") + payload
    ).hexdigest()


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
    state = _read_regular(path)
    if state.uid != trusted_uid or state.gid != trusted_gid:
        raise ReleaseRefused(f"release file ownership changed while read: {relative}")
    return {
        "path": relative,
        "kind": "file",
        "mode": state.mode,
        "sha256": state.sha256,
        "size": len(state.payload),
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
            "-c",
            "core.fsmonitor=false",
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
) -> list[dict[str, object]]:
    """Write the root-owned content manifest for a freshly staged release.

    Recorded before the staging tree is renamed into place, so a release
    directory never exists without the manifest that authorises its reuse.
    """
    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ReleaseRefused("release id must be exactly 40 lowercase hex characters")
    entries = release_entries(
        release_tree, trusted_uid=trusted_uid, trusted_gid=trusted_gid
    )
    _atomic_bytes(
        manifest,
        _manifest_document(release_id, entries),
        0o600,
        trusted_uid,
        trusted_gid,
    )
    return entries


def verify_release_manifest(
    release_dir: Path,
    manifest: Path,
    release_id: str,
    *,
    repo_root: Path | None = None,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> list[dict[str, object]]:
    """Prove a pre-existing release directory before it may be selected.

    Refuses a missing manifest outright: an unattested `/opt/aicc/releases/<sha>`
    is exactly the case this gate exists for, and rebuilding trust from the
    directory itself would only re-record whatever an attacker left there.
    """
    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise ReleaseRefused("release id must be exactly 40 lowercase hex characters")
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


def default_specs(
    repo_root: Path,
    *,
    authority_env: Path,
    claude_auth: Path,
    codex_auth: Path,
    resolve_identities: bool = True,
) -> tuple[FileSpec, ...]:
    root_uid, root_gid = 0, 0
    agent_gid = grp.getgrnam("aicc-agent").gr_gid if resolve_identities else 0
    publisher_gid = grp.getgrnam("aicc-publisher").gr_gid if resolve_identities else 0
    return (
        # The generator is intentionally the first destination mutation. Once
        # its atomic rename lands, every later mutation can recover on boot
        # from the self-contained generation copy referenced by pending.json.
        FileSpec(
            repo_root / "ops/aicc_principal_recovery_generator.py",
            "/usr/lib/systemd/system-generators/aicc-principal-recovery",
            0o755,
            root_uid,
            root_gid,
        ),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "validate",
            "prepare",
            "apply",
            "commit",
            "install",
            "recover",
            "rollback",
            "uninstall",
            "release-record",
            "release-verify",
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
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--verify-against-git", action="store_true")
    args = parser.parse_args()
    if args.action in {"release-record", "release-verify"}:
        if (
            args.release_tree is None
            or args.manifest is None
            or args.release_id is None
        ):
            parser.error(
                "--release-tree, --manifest and --release-id are required for "
                f"{args.action}"
            )
        if args.action == "release-record":
            record_release_manifest(args.release_tree, args.manifest, args.release_id)
            print(f"AICC_RELEASE_MANIFEST_RECORDED {args.release_id}")
        else:
            verify_release_manifest(
                args.release_tree,
                args.manifest,
                args.release_id,
                repo_root=args.repo_root if args.verify_against_git else None,
            )
            print(f"AICC_RELEASE_MANIFEST_VERIFIED {args.release_id}")
        return 0
    transaction = FileTransaction(args.root, args.state_dir)
    if args.action in {"validate", "prepare", "install"}:
        specs = default_specs(
            args.repo_root,
            authority_env=args.authority_env,
            claude_auth=args.claude_auth,
            codex_auth=args.codex_auth,
            resolve_identities=args.action != "validate",
        )
    if args.action == "validate":
        transaction.validate_sources(specs)
    elif args.action == "prepare":
        transaction.prepare(specs)
    elif args.action == "apply":
        transaction.apply()
    elif args.action == "commit":
        transaction.commit()
    elif args.action == "install":
        transaction.install(specs)
    elif args.action in {"recover", "rollback"}:
        transaction.recover()
    else:
        transaction.uninstall_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
