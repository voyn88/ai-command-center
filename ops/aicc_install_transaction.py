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
SNAPSHOT_PROPERTIES = frozenset(
    {
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
    }
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


def _atomic_bytes(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.aicc-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
                        and set(state["properties"]) != SNAPSHOT_PROPERTIES
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
    args = parser.parse_args()
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
