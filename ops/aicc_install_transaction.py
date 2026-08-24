#!/usr/bin/env python3
"""Atomic, reversible installation of the principal-isolation file set."""

from __future__ import annotations

import argparse
import grp
import json
import os
import secrets
import stat
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


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
    mode: int | None
    uid: int | None
    gid: int | None


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.aicc-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
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


class FileTransaction:
    """Install a complete file set or restore its exact pre-install state."""

    def __init__(self, root: Path, state_dir: Path):
        self.root = root.resolve()
        self.state_dir = state_dir.resolve()
        self.current = self.state_dir / "current.json"

    def _target(self, absolute: str) -> Path:
        if not absolute.startswith("/") or ".." in Path(absolute).parts:
            raise ValueError(f"unsafe installation target: {absolute}")
        return self.root / absolute.removeprefix("/")

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
            info = spec.source.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError(f"source is not a regular file: {spec.source}")
            if spec.target in targets:
                raise ValueError(f"duplicate installation target: {spec.target}")
            targets.add(spec.target)
        return validated

    def install(self, specs: Iterable[FileSpec]) -> None:
        validated = self.validate_sources(specs)
        source_payloads = {spec.target: spec.source.read_bytes() for spec in validated}
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
        transaction = self.state_dir / f"transaction-{secrets.token_hex(8)}"
        backups = transaction / "backups"
        backups.mkdir(parents=True, mode=0o700)
        records: list[BackupRecord] = []

        # Snapshot every target before the first mutation.
        for index, spec in enumerate(validated):
            target = self._target(spec.target)
            if spec.if_missing and target.exists():
                continue
            try:
                info = target.lstat()
            except FileNotFoundError:
                records.append(BackupRecord(spec.target, False, None, None, None, None))
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError(f"existing target is not a regular file: {target}")
            backup = backups / f"{index:03d}.bin"
            backup.write_bytes(target.read_bytes())
            backup.chmod(0o600)
            records.append(
                BackupRecord(
                    spec.target,
                    True,
                    str(backup),
                    stat.S_IMODE(info.st_mode),
                    info.st_uid,
                    info.st_gid,
                )
            )

        manifest = transaction / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "records": [asdict(record) for record in records],
                    "previous_current": previous_current,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        _fsync_dir(transaction)

        try:
            record_targets = {record.target for record in records}
            for spec in validated:
                if spec.target not in record_targets:
                    continue
                _atomic_bytes(
                    self._target(spec.target),
                    source_payloads[spec.target],
                    spec.mode,
                    spec.uid,
                    spec.gid,
                )
            _atomic_bytes(
                self.current,
                json.dumps({"manifest": str(manifest)}, sort_keys=True).encode(),
                0o600,
                os.geteuid(),
                os.getegid(),
            )
        except BaseException:
            self.restore(manifest)
            raise

    def restore(self, manifest: Path | None = None) -> None:
        if manifest is None:
            current = json.loads(self.current.read_text(encoding="utf-8"))
            manifest = Path(current["manifest"])
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        records = [BackupRecord(**value) for value in payload["records"]]
        for record in reversed(records):
            target = self._target(record.target)
            if record.existed:
                assert record.backup is not None
                assert record.mode is not None
                assert record.uid is not None
                assert record.gid is not None
                _atomic_bytes(
                    target,
                    Path(record.backup).read_bytes(),
                    record.mode,
                    record.uid,
                    record.gid,
                )
            else:
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError(f"refusing to remove replaced target: {target}")
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
        "action", choices=("validate", "install", "rollback", "uninstall")
    )
    parser.add_argument("--repo-root", type=Path, required=True)
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
    specs = default_specs(
        args.repo_root,
        authority_env=args.authority_env,
        claude_auth=args.claude_auth,
        codex_auth=args.codex_auth,
        resolve_identities=args.action != "validate",
    )
    if args.action == "validate":
        transaction.validate_sources(specs)
    elif args.action == "install":
        transaction.install(specs)
    else:
        transaction.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
