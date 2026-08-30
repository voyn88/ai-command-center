#!/usr/bin/python3
"""Install the content-addressed provider toolchain from its pinned artifact.

Production used to obtain the provider CLIs by running `npm install --global`
as root: it resolved npm online and executed third-party package lifecycle
scripts with root privileges. This module replaces that entirely. It downloads
one immutable release asset, proves it against the sha256 the reviewed lock
pins, extracts it under a root-owned tree, records the same content manifest
the principal-isolation release path uses, and selects it atomically. Nothing
here resolves a package, and no code from the artifact is executed.

The artifact carries the native binaries the packages ship for linux-x64, so
the installed toolchain needs no Node runtime -- which also removes production's
dependency on the host Node, currently v18 on one host and v22 on the other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aicc_install_transaction import (  # noqa: E402
    ARTIFACT_ID_RE,
    ReleaseRefused,
    publish_release_tree,
    record_release_manifest,
    reconcile_release_publication,
    verify_release_manifest,
)

TOOLCHAIN_ROOT = Path("/opt/aicc/toolchains")
STATE_DIR = Path("/var/lib/aicc-toolchain")
LOCK_PATH = Path("deploy/agent-toolchain.lock.json")
RELEASE_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REPOSITORY = "voyn88/ai-command-center"
# The repository is public, so the asset is fetched without a credential. A
# privileged installer that needs no token is one less secret on the host.
ASSET_URL = "https://github.com/{repository}/releases/download/{tag}/{name}"
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024


class ToolchainRefused(RuntimeError):
    """The toolchain artifact could not be proven."""


def load_lock(path: Path) -> dict[str, object]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolchainRefused(f"toolchain lock is unreadable: {path}") from exc
    digest = lock.get("artifact_sha256")
    tag = lock.get("release_tag")
    if not isinstance(digest, str) or ARTIFACT_ID_RE.fullmatch(digest) is None:
        raise ToolchainRefused(
            "toolchain lock does not pin a sha256 -- run the build workflow and "
            "record its digest in the lock as a reviewed change"
        )
    if not isinstance(tag, str) or RELEASE_TAG_RE.fullmatch(tag) is None:
        raise ToolchainRefused("toolchain lock does not pin a usable release tag")
    executables = lock.get("executables")
    if not isinstance(executables, dict) or not executables:
        raise ToolchainRefused("toolchain lock declares no executables")
    for name, relative in executables.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
            raise ToolchainRefused(f"unsafe executable name in lock: {name!r}")
        candidate = Path(str(relative))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ToolchainRefused(f"unsafe executable path in lock: {relative!r}")
    return lock


def fetch_artifact(lock: dict[str, object], platform: str) -> bytes:
    """Download the asset named by its own digest and prove that digest."""
    digest = str(lock["artifact_sha256"])
    name = f"agent-toolchain-{platform}-{digest}.tar.gz"
    url = ASSET_URL.format(
        repository=REPOSITORY, tag=str(lock["release_tag"]), name=name
    )
    request = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream"}
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ToolchainRefused(
            f"cannot download the toolchain artifact: {exc}"
        ) from exc
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ToolchainRefused("toolchain artifact exceeds its size bound")
    observed = hashlib.sha256(payload).hexdigest()
    if observed != digest:
        raise ToolchainRefused(
            f"toolchain artifact digest mismatch: expected {digest}, got {observed}"
        )
    return payload


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Every member, refused unless it is plainly safe to extract as root.

    `tarfile`'s own data filter already rejects absolute paths, traversal and
    device nodes. This adds what matters for a privileged extraction: no
    hardlinks (an alias into an already-verified inode), no setuid/setgid bits,
    and symlinks only when their target stays inside the tree.
    """
    members = []
    for member in archive.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ToolchainRefused(f"unsafe path in artifact: {member.name}")
        if member.islnk():
            raise ToolchainRefused(f"artifact contains a hardlink: {member.name}")
        if member.issym():
            target = Path(member.linkname)
            if target.is_absolute():
                raise ToolchainRefused(
                    f"artifact symlink escapes the tree: {member.name}"
                )
            # Normalise rather than string-match: `bin/claude -> ../pkg/claude`
            # is the artifact's own shape and contains "/../" while staying
            # firmly inside the tree. What matters is where it lands.
            resolved = os.path.normpath(os.path.join(name.parent.as_posix(), target))
            if resolved.startswith("..") or os.path.isabs(resolved):
                raise ToolchainRefused(
                    f"artifact symlink escapes the tree: {member.name}"
                )
        elif not (member.isfile() or member.isdir()):
            raise ToolchainRefused(f"unsupported artifact entry: {member.name}")
        if member.mode & (stat.S_ISUID | stat.S_ISGID):
            raise ToolchainRefused(f"artifact entry is setuid/setgid: {member.name}")
        members.append(member)
    if not members:
        raise ToolchainRefused("toolchain artifact is empty")
    return members


def extract_artifact(
    payload: bytes,
    staging: Path,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> None:
    """Extract the proven archive into a trusted-owned staging tree.

    Ownership is forced rather than taken from the archive: a tar carries its
    own uid/gid, and honouring them would let the artifact decide who owns the
    files root just wrote. Modes are normalised to non-writable-by-others for
    the same reason.
    """
    import io

    staging.mkdir(mode=0o755, parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = _safe_members(archive)
        archive.extractall(staging, members=members, filter="data")
    for path in sorted(staging.rglob("*"), reverse=True):
        if path.is_symlink():
            if os.chown in os.supports_follow_symlinks:
                os.chown(path, trusted_uid, trusted_gid, follow_symlinks=False)
            continue
        os.chown(path, trusted_uid, trusted_gid)
        os.chmod(path, 0o755 if (path.is_dir() or os.access(path, os.X_OK)) else 0o644)
    os.chown(staging, trusted_uid, trusted_gid)
    os.chmod(staging, 0o755)


def select_toolchain(digest: str, *, root: Path = TOOLCHAIN_ROOT) -> None:
    """Point `current` at a proven release, atomically."""
    current = root / "current"
    temporary = root / f".current.{os.getpid()}"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(Path("releases") / digest)
    os.replace(temporary, current)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(
    lock_path: Path,
    *,
    root: Path = TOOLCHAIN_ROOT,
    state_dir: Path = STATE_DIR,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> Path:
    lock = load_lock(lock_path)
    digest = str(lock["artifact_sha256"])
    platform = str(lock.get("platform", "linux-x64"))
    release_root = root / "releases"
    manifest = state_dir / "releases" / f"{digest}.json"
    release_dir = release_root / digest

    # The release root must exist before reconciliation looks inside it:
    # `reconcile_release_publication` opens it with `create=False`, so on a
    # first installation -- the case that matters most -- it would fail before
    # anything else ran (independent review on 58b50b9).
    # Every level is created and then given its mode explicitly. `parents=True`
    # alone applies the caller's umask to the intermediate directories, so on a
    # host with umask 002 the toolchain root comes out group-writable and the
    # publication guard rightly refuses it -- as a Linux run of the end-to-end
    # test showed. Modes are set after creation because `mkdir(mode=...)` is
    # itself masked by the umask.
    for directory, mode in (
        (root, 0o755),
        (release_root, 0o755),
        (state_dir, 0o700),
        (state_dir / "releases", 0o700),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, trusted_uid, trusted_gid)
        os.chmod(directory, mode)

    resumed = reconcile_release_publication(
        release_root,
        manifest,
        digest,
        state_dir=state_dir,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        id_pattern=ARTIFACT_ID_RE,
    )
    if release_dir.is_dir():
        # Already present: prove it rather than trust the name, exactly as the
        # principal-isolation release path does. A same-name/wrong-tree
        # toolchain would otherwise be selected on its directory name alone.
        verify_release_manifest(
            release_dir,
            manifest,
            digest,
            trusted_uid=trusted_uid,
            trusted_gid=trusted_gid,
            id_pattern=ARTIFACT_ID_RE,
        )
    else:
        if resumed is None:
            payload = fetch_artifact(lock, platform)
            staging = release_root / f".stage-{digest}.{os.getpid()}"
            extract_artifact(
                payload, staging, trusted_uid=trusted_uid, trusted_gid=trusted_gid
            )
            # Record the manifest from the staging tree BEFORE publication:
            # `publish_release_tree` verifies against it and does not create it,
            # exactly as the shell installer records one before renaming a Git
            # release into place. Missing this is what made the first live
            # bootstrap refuse with "release manifest is missing or unsafe" --
            # the artifact was downloaded and extracted correctly, and then had
            # nothing authorising its publication.
            record_release_manifest(
                staging,
                manifest,
                digest,
                trusted_uid=trusted_uid,
                trusted_gid=trusted_gid,
                id_pattern=ARTIFACT_ID_RE,
            )
            publish_release_tree(
                staging,
                release_root,
                manifest,
                digest,
                trusted_uid=trusted_uid,
                trusted_gid=trusted_gid,
                id_pattern=ARTIFACT_ID_RE,
            )

    _require_declared_executables(release_dir, lock, trusted_uid=trusted_uid)
    select_toolchain(digest, root=root)
    return release_dir


def _require_declared_executables(
    release_dir: Path,
    lock: dict[str, object],
    *,
    trusted_uid: int = 0,
) -> None:
    """Every executable the lock declares must exist, be trusted-owned and be
    executable -- checked without running any of them."""
    for name, relative in sorted(dict(lock["executables"]).items()):  # type: ignore[arg-type]
        target = release_dir / str(relative)
        try:
            info = target.stat()
        except OSError as exc:
            raise ToolchainRefused(
                f"toolchain executable is missing: {name} -> {relative}"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise ToolchainRefused(f"toolchain executable is not a file: {name}")
        if info.st_uid != trusted_uid:
            raise ToolchainRefused(f"toolchain executable is not root-owned: {name}")
        if not info.st_mode & stat.S_IXUSR:
            raise ToolchainRefused(f"toolchain executable is not executable: {name}")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ToolchainRefused(
                f"toolchain executable is group/world writable: {name}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--root", type=Path, default=TOOLCHAIN_ROOT)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise ToolchainRefused("toolchain installation must run as root")
    release = install(args.lock, root=args.root, state_dir=args.state_dir)
    print(f"AICC_TOOLCHAIN_SELECTED {release.name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ToolchainRefused, ReleaseRefused) as exc:
        print(f"AICC_TOOLCHAIN_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
