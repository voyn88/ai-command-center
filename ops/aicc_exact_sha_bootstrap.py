#!/usr/bin/python3
"""Bootstrap AICC principal isolation from one trusted, exact Git commit.

This is the only privileged entry point allowed to cross from a remote Git
commit into the root-owned principal-isolation installer.  It deliberately
does not consume an operator checkout: all executable bytes are fetched into
and verified inside a private root-owned attempt directory first.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

TRUSTED_REMOTE = "https://github.com/voyn88/ai-command-center.git"
DEFAULT_STATE_ROOT = Path("/var/lib/aicc-exact-sha-bootstrap")
DEFAULT_AUTHORITY_ENV = Path("/etc/aicc/workspace-authority.env")
GIT = "/usr/bin/git"
# Every repository-config knob that turns a plain Git read into code execution
# by the invoking (root) principal. `GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL`
# in `_safe_environment` neutralise system and user config, but NOT the
# per-repository `.git/config` of the tree being verified -- which, on the
# attestation-reuse path, is exactly the file an attacker would target. These
# are therefore forced on the command line, where repository config cannot win.
# Found live: `core.fsmonitor` executed during `git status` in verification.
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


def _git_argv(*arguments: str) -> list[str]:
    """The only way this module builds a Git command line."""
    return [GIT, *GIT_CONFIG_FREE, *arguments]


SHA_RE = re.compile(r"[0-9a-f]{40}")
TREE_ENTRY_RE = re.compile(rb"([0-7]{6}) (blob|commit) ([0-9a-f]{40})\t(.+)", re.DOTALL)
REQUIRED_ENTRYPOINTS = (
    "deploy/install-agent-toolchain.sh",
    "deploy/install-agent-principal-isolation.sh",
    "ops/aicc_install_transaction.py",
    "ops/aicc_staged_worker_rollout.py",
    "ops/verify-agent-principal-boundary.sh",
    "command_center/workspace_authority.py",
)


class BootstrapRefused(RuntimeError):
    """The privileged trust boundary could not be proven."""


@dataclass(frozen=True, slots=True)
class TreeAttestation:
    expected_sha: str
    remote_main_sha: str
    tree_manifest_sha256: str
    file_count: int
    repository: str
    attempt_id: str


def _run(
    argv: list[str], *, cwd: Path | None, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BootstrapRefused(detail or f"command failed: {argv[0]}")
    return result


def _safe_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _require_private_root_directory(path: Path, *, create: bool) -> None:
    if not path.is_absolute():
        raise BootstrapRefused("bootstrap state root must be absolute")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BootstrapRefused("bootstrap state root must be root:root mode 0700")
    current = path
    while current != Path("/"):
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise BootstrapRefused(f"untrusted bootstrap path component: {current}")
        if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise BootstrapRefused(
                f"rename-writable bootstrap path component: {current}"
            )
        current = current.parent


def _require_root_owned_directory_chain(path: Path, *, create: bool) -> None:
    if not path.is_absolute():
        raise BootstrapRefused("privileged directory must be absolute")
    if create:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
    current = path
    while True:
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise BootstrapRefused(f"untrusted privileged directory: {current}")
        if current == Path("/"):
            break
        current = current.parent


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _git_blob_oid(payload: bytes) -> str:
    """The blob's Git object name.

    SHA-1 is not a choice here and carries no security claim of its own: it is
    the identity function of the repository format, and the value is compared
    against an oid Git itself produced. `usedforsecurity=False` states that,
    so neither a reader nor a scanner mistakes it for hashing a secret. The
    trust in this payload comes from the exact commit SHA the bootstrap pins,
    not from the strength of this digest.
    """
    prefix = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.new("sha1", prefix + payload, usedforsecurity=False).hexdigest()


def _verify_owned_tree(root: Path, *, trusted_uid: int, trusted_gid: int) -> None:
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_info = directory_path.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != trusted_uid
            or directory_info.st_gid != trusted_gid
            or stat.S_IMODE(directory_info.st_mode) & 0o022
        ):
            raise BootstrapRefused(f"untrusted checkout directory: {directory_path}")
        for name in [*names, *files]:
            path = directory_path / name
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or info.st_uid != trusted_uid
                or info.st_gid != trusted_gid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise BootstrapRefused(f"untrusted checkout path: {path}")


def _read_trusted_blob(
    path: Path,
    *,
    expected_mode: int,
    trusted_uid: int,
    trusted_gid: int,
    max_bytes: int = 128 * 1024 * 1024,
) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or initial.st_uid != trusted_uid
            or initial.st_gid != trusted_gid
            or stat.S_IMODE(initial.st_mode) != expected_mode
            or initial.st_size > max_bytes
        ):
            raise BootstrapRefused(f"checkout file shape is unsafe: {path}")
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise BootstrapRefused(f"checkout file was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise BootstrapRefused(f"checkout file grew while read: {path}")
        final = os.fstat(descriptor)
        if (
            final.st_dev != initial.st_dev
            or final.st_ino != initial.st_ino
            or final.st_size != initial.st_size
            or final.st_mtime_ns != initial.st_mtime_ns
            or final.st_ctime_ns != initial.st_ctime_ns
            or final.st_uid != initial.st_uid
            or final.st_gid != initial.st_gid
            or stat.S_IMODE(final.st_mode) != stat.S_IMODE(initial.st_mode)
        ):
            raise BootstrapRefused(f"checkout file changed while read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_tree_manifest(repo: Path, env: dict[str, str], sha: str) -> bytes:
    return _run(
        _git_argv("ls-tree", "-rz", "--full-tree", sha),
        cwd=repo,
        env=env,
    ).stdout


def _verify_checkout(
    repo: Path,
    env: dict[str, str],
    expected_sha: str,
    attempt_id: str,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> TreeAttestation:
    _verify_owned_tree(repo, trusted_uid=trusted_uid, trusted_gid=trusted_gid)
    head = _run(
        _git_argv("rev-parse", "HEAD^{commit}"), cwd=repo, env=env
    ).stdout.strip()
    remote_main = _run(
        _git_argv("rev-parse", "refs/remotes/origin/main^{commit}"), cwd=repo, env=env
    ).stdout.strip()
    expected = expected_sha.encode("ascii")
    if head != expected or remote_main != expected:
        raise BootstrapRefused("checked out SHA is not the exact trusted remote main")
    if _run(
        _git_argv("status", "--porcelain=v1", "--untracked-files=all"),
        cwd=repo,
        env=env,
    ).stdout:
        raise BootstrapRefused("trusted checkout is not clean")

    raw_manifest = _read_tree_manifest(repo, env, expected_sha)
    entries = raw_manifest.rstrip(b"\0").split(b"\0") if raw_manifest else []
    observed: set[str] = set()
    for raw in entries:
        match = TREE_ENTRY_RE.fullmatch(raw)
        if match is None:
            raise BootstrapRefused("unparseable Git tree entry")
        mode_raw, kind, oid_raw, path_raw = match.groups()
        try:
            relative = path_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BootstrapRefused("non-UTF-8 path in trusted tree") from exc
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\x00" in relative:
            raise BootstrapRefused("unsafe path in trusted tree")
        if kind != b"blob" or mode_raw not in {b"100644", b"100755"}:
            raise BootstrapRefused(f"unsupported tree entry: {relative}")
        target = repo / path
        expected_mode = 0o755 if mode_raw == b"100755" else 0o644
        payload = _read_trusted_blob(
            target,
            expected_mode=expected_mode,
            trusted_uid=trusted_uid,
            trusted_gid=trusted_gid,
        )
        if _git_blob_oid(payload) != oid_raw.decode("ascii"):
            raise BootstrapRefused(
                f"checkout content does not match Git blob: {relative}"
            )
        observed.add(relative)

    missing = sorted(set(REQUIRED_ENTRYPOINTS) - observed)
    if missing:
        raise BootstrapRefused(f"bootstrap payload is incomplete: {missing}")
    return TreeAttestation(
        expected_sha=expected_sha,
        remote_main_sha=remote_main.decode("ascii"),
        tree_manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        file_count=len(entries),
        repository=TRUSTED_REMOTE,
        attempt_id=attempt_id,
    )


def _authority_file_is_safe(info: os.stat_result, allowed_gids: set[int]) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == 0
        and info.st_gid in allowed_gids
        and stat.S_IMODE(info.st_mode) == 0o640
    )


def _prepare_authority_file(path: Path) -> None:
    _require_root_owned_directory_chain(path.parent, create=True)
    if path.exists():
        info = path.lstat()
        allowed_gids = {0}
        try:
            allowed_gids.add(grp.getgrnam("aicc-publisher").gr_gid)
        except KeyError:
            pass
        if not _authority_file_is_safe(info, allowed_gids):
            raise BootstrapRefused("existing workspace authority file is unsafe")
        return
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    key = secrets.token_bytes(48).hex()
    _atomic_write(path, f"AICC_WORKSPACE_AUTHORITY_KEY=hex:{key}\n".encode(), 0o640)


def _fetch_exact_checkout(
    attempt: Path, env: dict[str, str], expected_sha: str
) -> Path:
    repo = attempt / "repo"
    _run(
        _git_argv("init", "--initial-branch=bootstrap", str(repo)), cwd=attempt, env=env
    )
    _run(_git_argv("remote", "add", "origin", TRUSTED_REMOTE), cwd=repo, env=env)
    _run(
        _git_argv(
            "fetch",
            "--no-tags",
            "--force",
            "origin",
            "refs/heads/main:refs/remotes/origin/main",
        ),
        cwd=repo,
        env=env,
    )
    remote_main = (
        _run(
            _git_argv("rev-parse", "refs/remotes/origin/main^{commit}"),
            cwd=repo,
            env=env,
        )
        .stdout.decode("ascii")
        .strip()
    )
    if remote_main != expected_sha:
        raise BootstrapRefused(
            "trusted remote main moved or does not match expected SHA"
        )
    _run(
        _git_argv("checkout", "--detach", expected_sha),
        cwd=repo,
        env=env,
    )
    return repo


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", nargs="?", choices=("install", "uninstall"), default="install"
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--authority-env", type=Path, default=DEFAULT_AUTHORITY_ENV)
    parser.add_argument("--verify-attestation", type=Path)
    parser.add_argument("--repo-root", type=Path)
    return parser.parse_args(argv)


def _verify_existing_attestation(
    path: Path,
    repo_root: Path,
    expected_sha: str,
    *,
    trusted_uid: int = 0,
    trusted_gid: int = 0,
) -> TreeAttestation:
    _require_private_root_directory(path.parent, create=False)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != trusted_uid
        or info.st_gid != trusted_gid
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise BootstrapRefused("bootstrap attestation is not root:root mode 0600")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        attestation = TreeAttestation(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapRefused("bootstrap attestation is malformed") from exc
    if (
        attestation.expected_sha != expected_sha
        or attestation.repository != TRUSTED_REMOTE
    ):
        raise BootstrapRefused("bootstrap attestation identity mismatch")
    expected_repo = (path.parent / "repo").resolve(strict=True)
    if repo_root.resolve(strict=True) != expected_repo:
        raise BootstrapRefused("installer is not running from the attested checkout")
    observed = _verify_checkout(
        repo_root,
        _safe_environment(path.parent),
        expected_sha,
        attestation.attempt_id,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
    )
    if observed != attestation:
        raise BootstrapRefused("bootstrap checkout no longer matches its attestation")
    return observed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.geteuid() != 0:
        raise BootstrapRefused("exact-SHA bootstrap must run as root")
    if SHA_RE.fullmatch(args.expected_sha) is None:
        raise BootstrapRefused(
            "expected SHA must be exactly 40 lowercase hex characters"
        )
    if args.verify_attestation is not None:
        if args.repo_root is None:
            raise BootstrapRefused(
                "--repo-root is required for attestation verification"
            )
        _verify_existing_attestation(
            args.verify_attestation, args.repo_root, args.expected_sha
        )
        print(f"AICC_EXACT_SHA_BOOTSTRAP_ATTESTED {args.expected_sha}")
        return 0
    if args.repo_root is not None:
        raise BootstrapRefused("--repo-root is only valid with --verify-attestation")
    _require_private_root_directory(args.state_root, create=True)
    attempt_id = f"{int(time.time())}-{secrets.token_hex(12)}"
    attempt = args.state_root / "attempts" / attempt_id
    attempt.parent.mkdir(mode=0o700, exist_ok=True)
    attempt.mkdir(mode=0o700)
    os.chown(attempt.parent, 0, 0)
    os.chown(attempt, 0, 0)
    env = _safe_environment(attempt)
    repo = _fetch_exact_checkout(attempt, env, args.expected_sha)
    attestation = _verify_checkout(repo, env, args.expected_sha, attempt_id)
    attestation_path = attempt / "attestation.json"
    _atomic_write(
        attestation_path,
        (json.dumps(asdict(attestation), sort_keys=True) + "\n").encode(),
    )
    installer_env = {
        **env,
        "AICC_BOOTSTRAP_ATTESTATION": str(attestation_path),
        "AICC_EXPECTED_RELEASE_SHA": args.expected_sha,
    }
    if args.action == "install":
        _prepare_authority_file(args.authority_env)
    installer_argv = [str(repo / "deploy/install-agent-principal-isolation.sh")]
    if args.action == "uninstall":
        installer_argv.append("--uninstall")
    _run(
        installer_argv,
        cwd=repo,
        env=installer_env,
    )
    completed = {**asdict(attestation), "completed_at": int(time.time())}
    _atomic_write(
        attempt / "completed.json",
        (json.dumps(completed, sort_keys=True) + "\n").encode(),
    )
    print(f"AICC_EXACT_SHA_BOOTSTRAP_{args.action.upper()}ED {args.expected_sha}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapRefused as exc:
        print(f"AICC_EXACT_SHA_BOOTSTRAP_REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
