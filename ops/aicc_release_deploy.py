#!/usr/bin/env python3
"""Root-owned worker release deploy: merged main -> immutable /opt/aicc/current.

VOYN-W0-AICC-WORKER-SELF-DEPLOY-MUST-STAGE-IMMUTABLE-RELEASE.

The voynadmin self-deploy tick cannot write root-owned releases under
`/opt/aicc/releases` (live worker-01: timer enabled, never successful). This
oneshot runs as root, fetches `origin/main` into a root-owned bare mirror
(never executing the voynadmin-writable checkout), stages the exact SHA via
the installer publication primitives (`record_release_manifest` /
`publish_release_tree` / `verify_release_manifest` / `select_release`), then
drain-restarts only the currently-active lanes from
`/etc/voyn/aicc-worker-lanes.conf` and waits for `STATUS=aicc-ready`.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_OPS_DIR = Path(__file__).resolve().parent
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DEPENDENCY_MANIFESTS = re.compile(
    r"(^|/)(uv\.lock|pyproject\.toml|requirements[^/]*\.txt|package(-lock)?\.json)$"
)
_READY = "aicc-ready"

#: Root helpers installed by the worker isolation profile (FileSpec list in
#: aicc_install_transaction). Worker release deploy publishes a new tree under
#: /opt/aicc/releases/<sha> but must also refresh these so merged launcher
#: fixes reach the socket-activated binary on the host.
_ROOT_HELPERS: tuple[tuple[str, str], ...] = (
    ("ops/aicc_grok_agent.py", "aicc-grok-agent"),
    ("ops/aicc_agent_launcher.py", "aicc-agent-launcher"),
    ("ops/aicc_staged_worker_rollout.py", "aicc-staged-worker-rollout"),
    ("ops/aicc_release_deploy.py", "aicc-worker-release-deploy"),
)


def _load_install_transaction():
    path = _OPS_DIR / "aicc_install_transaction.py"
    if not path.is_file():
        path = _OPS_DIR / "aicc-install-transaction"
    name = "aicc_install_transaction"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, path, loader=importlib.machinery.SourceFileLoader(name, str(path))
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load aicc_install_transaction")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_staged_rollout():
    # The installed rollout imports its hyphenated sibling by Python module name.
    # Load that trusted sibling before executing rollout, including the first lock.
    _load_install_transaction()
    path = _OPS_DIR / "aicc_staged_worker_rollout.py"
    if not path.is_file():
        path = _OPS_DIR / "aicc-staged-worker-rollout"
    name = "aicc_staged_worker_rollout"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, path, loader=importlib.machinery.SourceFileLoader(name, str(path))
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load aicc_staged_worker_rollout")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class DeployConfig:
    bare_mirror: Path = Path("/var/lib/aicc-principal-isolation/mirror.git")
    aicc_root: Path = Path("/opt/aicc")
    release_root: Path = Path("/opt/aicc/releases")
    state_dir: Path = Path("/var/lib/aicc-principal-isolation")
    runtime_venv: Path = Path("/opt/aicc/runtime-venv")
    libexec_dir: Path = Path("/usr/libexec")
    lane_registry: Path = Path("/etc/voyn/aicc-worker-lanes.conf")
    remote: str = "origin"
    branch: str = "main"
    remote_url: str | None = None
    rollout_lock_path: Path = Path("/run/aicc-staged-rollout.lock")
    readiness_timeout: float = 120.0
    drain_timeout: float = 3700.0
    poll_interval: float = 0.5
    git_timeout: int = 300
    trusted_uid: int = 0
    trusted_gid: int = 0


@dataclass(slots=True)
class DeployReport:
    outcome: str = ""  # NOOP | DEPLOYED | REFUSED | ROLLED_BACK
    detail: str = ""
    target_sha: str = ""
    previous_sha: str = ""
    steps: list[str] = field(default_factory=list)

    def line(self) -> str:
        sha = self.target_sha or "-"
        detail = self.detail or "-"
        return f"{self.outcome} {sha} {detail}"


def _run(
    args: list[str], *, timeout: int, cwd: str | None = None, env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout}s")


def _git_mirror(
    mirror: Path, args: list[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", "--git-dir", str(mirror), *args],
        timeout=timeout,
    )


def _current_release_sha(aicc_root: Path) -> str | None:
    current = aicc_root / "current"
    try:
        info = current.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("release selector is not a symlink")
    selector = os.readlink(current)
    match = re.fullmatch(r"releases/([0-9a-f]{40})", selector)
    if match is None:
        if os.path.isabs(selector):
            return None
        raise RuntimeError("release selector is invalid")
    return match.group(1)


#: The release source. Configured in the root-owned unit
#: (AICC_RELEASE_REMOTE_URL) and never read from the voynadmin-writable
#: checkout: whoever controls that checkout's git config must not be able to
#: repoint what root deploys.
DEFAULT_REMOTE_URL = "https://github.com/voyn88/ai-command-center.git"


def _discover_remote_url(cfg: DeployConfig) -> str:
    url = (cfg.remote_url or DEFAULT_REMOTE_URL).strip()
    if not url.startswith("https://"):
        raise RuntimeError("release remote must be an https URL")
    return url


def ensure_bare_mirror(cfg: DeployConfig) -> None:
    """Create/fetch a root-owned bare mirror of origin (trusted object store)."""
    mirror = cfg.bare_mirror
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        init = _run(["git", "init", "--bare", str(mirror)], timeout=cfg.git_timeout)
        if init.returncode != 0:
            raise RuntimeError(
                f"bare mirror init failed: {(init.stderr or init.stdout).strip()[:120]}"
            )
        url = _discover_remote_url(cfg)
        added = _git_mirror(
            mirror,
            ["remote", "add", cfg.remote, url],
            timeout=cfg.git_timeout,
        )
        if added.returncode != 0:
            raise RuntimeError(
                f"bare mirror remote add failed: "
                f"{(added.stderr or added.stdout).strip()[:120]}"
            )
    # Re-assert the configured URL every tick: a mirror created earlier (or
    # edited by hand) must not keep fetching from somewhere else.
    reset = _git_mirror(
        mirror,
        ["remote", "set-url", cfg.remote, _discover_remote_url(cfg)],
        timeout=cfg.git_timeout,
    )
    if reset.returncode != 0:
        raise RuntimeError(
            f"bare mirror set-url failed: {(reset.stderr or reset.stdout).strip()[:120]}"
        )
    fetched = _git_mirror(
        mirror,
        ["fetch", "--prune", cfg.remote, f"+refs/heads/{cfg.branch}:refs/heads/{cfg.branch}"],
        timeout=cfg.git_timeout,
    )
    if fetched.returncode != 0:
        raise RuntimeError(
            f"bare mirror fetch failed: {(fetched.stderr or fetched.stdout).strip()[:160]}"
        )


def resolve_target_sha(cfg: DeployConfig) -> str:
    parsed = _git_mirror(
        cfg.bare_mirror,
        ["rev-parse", f"refs/heads/{cfg.branch}"],
        timeout=cfg.git_timeout,
    )
    if parsed.returncode != 0:
        # Fall back to remote-tracking style if the mirror stores it that way.
        parsed = _git_mirror(
            cfg.bare_mirror,
            ["rev-parse", f"{cfg.remote}/{cfg.branch}"],
            timeout=cfg.git_timeout,
        )
    if parsed.returncode != 0:
        raise RuntimeError("cannot resolve target sha from bare mirror")
    sha = parsed.stdout.strip()
    if not _RELEASE_SHA_RE.fullmatch(sha):
        raise RuntimeError("target sha is not a 40-hex commit")
    return sha


def _dependency_change_refuses(
    cfg: DeployConfig, previous_sha: str | None, target_sha: str
) -> str | None:
    if previous_sha is None or previous_sha == target_sha:
        return None
    changed = _git_mirror(
        cfg.bare_mirror,
        ["diff", "--name-only", f"{previous_sha}..{target_sha}"],
        timeout=cfg.git_timeout,
    )
    if changed.returncode != 0:
        return "diff_failed"
    manifests = [
        name
        for name in changed.stdout.splitlines()
        if _DEPENDENCY_MANIFESTS.search(name.strip())
    ]
    if manifests:
        return f"dependency_change_requires_installer: {manifests[:3]}"
    return None


def _freeze_tree(path: Path, *, uid: int, gid: int) -> None:
    """chown/chmod a staging tree without following symlinks into runtime-venv."""
    for root, dirs, files in os.walk(path, followlinks=False, topdown=False):
        for name in dirs + files:
            target = Path(root) / name
            try:
                os.lchown(target, uid, gid)
            except AttributeError:
                # Non-Unix test hosts: ownership seams are injected.
                pass
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            mode = stat.S_IMODE(info.st_mode) & ~0o222
            if stat.S_ISDIR(info.st_mode):
                mode = (mode | 0o555) & ~0o222
            os.chmod(target, mode)
    try:
        os.lchown(path, uid, gid)
    except AttributeError:
        pass
    # Release dirs are dr-xr-xr-x like every existing release: lanes (aicc-worker)
    # must read/execute them and nobody may write them.
    os.chmod(path, 0o555)  # nosec B103


def _verify_runtime_venv(cfg: DeployConfig) -> None:
    venv = cfg.runtime_venv
    if not venv.is_dir():
        raise RuntimeError(f"runtime venv missing: {venv}")
    info = venv.lstat()
    if info.st_uid != cfg.trusted_uid or info.st_gid != cfg.trusted_gid:
        raise RuntimeError("runtime venv is not trusted-owned")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError("runtime venv is group/other writable")
    python = venv / "bin" / "python"
    if not python.exists():
        raise RuntimeError("runtime venv has no bin/python")


def stage_release_tree(
    cfg: DeployConfig,
    release_id: str,
    *,
    install_tx=None,
) -> Path:
    """Archive the exact SHA, bind the runtime venv, publish via installer path."""
    install_tx = install_tx or _load_install_transaction()
    release_dir = cfg.release_root / release_id
    manifest = cfg.state_dir / "releases" / f"{release_id}.json"
    cfg.release_root.mkdir(parents=True, mode=0o755, exist_ok=True)
    try:
        os.chown(cfg.release_root, cfg.trusted_uid, cfg.trusted_gid)
    except (AttributeError, OSError, PermissionError):
        pass

    install_tx.reconcile_release_publication(
        cfg.release_root,
        manifest,
        release_id,
        state_dir=cfg.state_dir,
        trusted_uid=cfg.trusted_uid,
        trusted_gid=cfg.trusted_gid,
    )
    if release_dir.is_dir():
        install_tx.verify_release_manifest(
            release_dir,
            manifest,
            release_id,
            repo_root=cfg.bare_mirror,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        return release_dir

    _verify_runtime_venv(cfg)
    staging = cfg.release_root / f".stage-{release_id}.{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o755)

    try:
        # Two sequential bounded commands through a private tarball (no
        # process pipeline): the archive comes only from the root mirror.
        tarball = cfg.release_root / f".stage-{release_id}.{os.getpid()}.tar"
        archive = _git_mirror(
            cfg.bare_mirror,
            ["archive", "--format=tar", "-o", str(tarball), release_id],
            timeout=cfg.git_timeout,
        )
        if archive.returncode != 0:
            tarball.unlink(missing_ok=True)
            raise RuntimeError(f"git archive failed with rc={archive.returncode}")
        extract = _run(
            ["tar", "-xf", str(tarball), "-C", str(staging)],
            timeout=cfg.git_timeout,
        )
        tarball.unlink(missing_ok=True)
        if extract.returncode != 0:
            raise RuntimeError(
                "tar extract failed: "
                f"{(extract.stderr or extract.stdout)[:160]}"
            )

        venv_link = staging / ".venv"
        if venv_link.exists() or venv_link.is_symlink():
            raise RuntimeError("archive unexpectedly contains .venv")
        venv_link.symlink_to(cfg.runtime_venv)

        # AIOS wheels live in the root-owned runtime venv (installer-provisioned).
        # Prove the release+venv can import what lanes need before publication.
        preflight = _run(
            [
                str(cfg.runtime_venv / "bin" / "python"),
                "-B",
                "-c",
                (
                    "import aios_db, aios_sdk; from command_center import worker; "
                    "assert worker is not None"
                ),
            ],
            timeout=60,
            env={
                **os.environ,
                "PYTHONPATH": str(staging),
            },
        )
        if preflight.returncode != 0:
            raise RuntimeError(
                "runtime venv preflight failed: "
                f"{(preflight.stderr or preflight.stdout).strip()[:160]}"
            )

        _freeze_tree(staging, uid=cfg.trusted_uid, gid=cfg.trusted_gid)
        manifest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chown(manifest.parent, cfg.trusted_uid, cfg.trusted_gid)
        except (AttributeError, OSError, PermissionError):
            pass
        install_tx.record_release_manifest(
            staging,
            manifest,
            release_id,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        published = install_tx.publish_release_tree(
            staging,
            cfg.release_root,
            manifest,
            release_id,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        staging = None  # published; do not remove the working directory
    except BaseException:
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    install_tx.verify_release_manifest(
        published,
        manifest,
        release_id,
        repo_root=cfg.bare_mirror,
        trusted_uid=cfg.trusted_uid,
        trusted_gid=cfg.trusted_gid,
    )
    return published


def _systemctl_show(unit: str, prop: str, *, timeout: int = 30) -> str:
    result = _run(
        ["systemctl", "show", unit, f"--property={prop}", "--value"],
        timeout=timeout,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def active_registered_lanes(
    lanes: tuple[str, ...],
    *,
    show: Callable[[str, str], str] | None = None,
) -> tuple[str, ...]:
    """Registered lanes whose ActiveState is not inactive (never start stopped)."""
    show = show or (lambda unit, prop: _systemctl_show(unit, prop))
    active: list[str] = []
    for unit in lanes:
        state = show(unit, "ActiveState")
        if state != "inactive":
            active.append(unit)
    return tuple(active)


def _wait_lane_ready(
    unit: str,
    *,
    timeout: float,
    poll_interval: float,
    show: Callable[[str, str], str],
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        active = show(unit, "ActiveState")
        sub = show(unit, "SubState")
        status = show(unit, "StatusText")
        main_pid = show(unit, "MainPID")
        try:
            pid = int(main_pid or "0")
        except ValueError:
            pid = 0
        if (
            active == "active"
            and sub == "running"
            and pid > 0
            and status.startswith(_READY)
        ):
            return True
        sleeper(poll_interval)
    return False


def _file_sha256(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _install_helper_bytes(
    target: Path, payload: bytes, *, mode: int, uid: int, gid: int
) -> None:
    """Atomically install a root helper (temp in same dir, fsync, rename)."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{target.name}.aicc-{os.getpid()}-{time.time_ns()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # Windows: avoid CRLF translation on helper payloads
    fd = os.open(str(temporary), flags, mode)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        if hasattr(os, "fchown"):
            try:
                os.fchown(fd, uid, gid)
            except (AttributeError, OSError, PermissionError):
                pass
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(temporary, mode)  # nosec B103 - root helper mode is intentional
        if hasattr(os, "chown"):
            try:
                os.chown(temporary, uid, gid)
            except (AttributeError, OSError, PermissionError):
                pass
        os.replace(str(temporary), str(target))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def refresh_root_helpers(
    cfg: DeployConfig,
    release_id: str,
    *,
    install_tx=None,
) -> list[str]:
    """Replace libexec helpers that differ from the newly published release.

    Returns the installed basenames that were rewritten. Does not restart
    socket-activated `aicc-agent-launcher@*.service` instances — the next
    accepted connection loads the new binary.
    """
    release_dir = cfg.release_root / release_id
    refreshed: list[str] = []
    for relative, name in _ROOT_HELPERS:
        source = release_dir / relative
        if not source.is_file():
            continue
        target = cfg.libexec_dir / name
        source_digest = _file_sha256(source)
        if source_digest is None:
            continue
        if source_digest == _file_sha256(target):
            continue
        payload = source.read_bytes()
        if install_tx is not None and hasattr(install_tx, "_atomic_bytes"):
            install_tx._atomic_bytes(
                target, payload, 0o755, cfg.trusted_uid, cfg.trusted_gid
            )
        else:
            _install_helper_bytes(
                target,
                payload,
                mode=0o755,
                uid=cfg.trusted_uid,
                gid=cfg.trusted_gid,
            )
        refreshed.append(name)
    return refreshed


def load_installed_lane_registry(cfg: DeployConfig) -> tuple[str, ...]:
    """Use the installed validator without mutating its attested release tree.

    Root can write bytecode even inside a frozen release. Explicit -B prevents
    new cache directories from invalidating the manifest needed for rollback.
    """
    installed_sha = _current_release_sha(cfg.aicc_root)
    if installed_sha is None:
        raise RuntimeError("no installed release for lane registry validation")
    release = cfg.release_root / installed_sha
    result = _run(
        [str(cfg.runtime_venv / "bin" / "python"), "-B", "-c",
         ("import json, sys; from pathlib import Path; "
         "from command_center.ops.credential_rotation import load_lane_registry; "
         "print(json.dumps(load_lane_registry(Path(sys.argv[1]), "
         "expected_uid=int(sys.argv[2]), expected_gid=int(sys.argv[3]))))"),
         str(cfg.lane_registry), str(cfg.trusted_uid), str(cfg.trusted_gid)],
        timeout=60,
        cwd=str(release),
        env={**os.environ, "PYTHONPATH": str(release)},
    )
    if result.returncode != 0:
        raise RuntimeError("release lane registry validation failed")
    lanes = json.loads(result.stdout)
    if (not isinstance(lanes, list) or not lanes
            or any(not isinstance(unit, str) or not re.fullmatch(
                r"voyn-aicc-worker@[0-9]{1,4}\.service", unit) for unit in lanes)
            or len(set(lanes)) != len(lanes)):
        raise RuntimeError("release returned invalid lane registry")
    return tuple(lanes)


def rollout_active_lanes(
    units: tuple[str, ...],
    *,
    readiness_timeout: float,
    poll_interval: float,
    drain_timeout: float = 3700.0,
    show: Callable[[str, str], str] | None = None,
    systemctl: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Drain-restart each active lane; require STATUS=aicc-ready. Returns (ok, detail)."""
    if not units:
        return True, "no_active_lanes"
    show = show or (lambda unit, prop: _systemctl_show(unit, prop))

    def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        if systemctl is not None:
            return systemctl(args)
        # Draining an in-flight agent follows worker TimeoutStopSec=3660s,
        # not the much shorter post-start readiness deadline.
        timeout = drain_timeout if args[0] == "stop" else readiness_timeout + 30
        return _run(["systemctl", *args], timeout=max(60, int(timeout)))

    ready_count = 0
    for unit in units:
        stopped = _systemctl(["stop", unit])
        if stopped.returncode != 0:
            return False, f"stop_failed:{unit}"
        if show(unit, "ActiveState") != "inactive":
            return False, f"drain_failed:{unit}"
        started = _systemctl(["start", unit])
        if started.returncode != 0:
            return False, f"start_failed:{unit}"
        if not _wait_lane_ready(
            unit,
            timeout=readiness_timeout,
            poll_interval=poll_interval,
            show=show,
            clock=clock,
            sleeper=sleeper,
        ):
            return False, f"not_ready:{unit}"
        ready_count += 1
    return True, f"ready:{ready_count}"


def deploy_once(
    cfg: DeployConfig | None = None,
    *,
    install_tx=None,
    load_lanes: Callable[..., tuple[str, ...]] | None = None,
    stage: Callable[..., Path] | None = None,
    select: Callable[..., str] | None = None,
    restore_previous: Callable[[], None] | None = None,
    clear_pending: Callable[[], None] | None = None,
    rollout: Callable[..., tuple[bool, str]] | None = None,
    show: Callable[[str, str], str] | None = None,
    hold_timers: Callable[[], None] | None = None,
    release_timers: Callable[[], None] | None = None,
    write_lock: Callable[[], None] | None = None,
    remove_lock: Callable[[], None] | None = None,
    ensure_mirror: Callable[[DeployConfig], None] | None = None,
    resolve_sha: Callable[[DeployConfig], str] | None = None,
    dependency_gate: Callable[[DeployConfig, str | None, str], str | None] | None = None,
    current_sha: Callable[[DeployConfig], str | None] | None = None,
) -> DeployReport:
    """One deploy tick. Prints nothing; caller prints `report.line()`."""
    cfg = cfg or DeployConfig()
    report = DeployReport()
    _install_tx = install_tx

    def _tx():
        nonlocal _install_tx
        if _install_tx is None:
            _install_tx = _load_install_transaction()
        return _install_tx

    def finish(outcome: str, detail: str) -> DeployReport:
        report.outcome = outcome
        report.detail = detail
        return report

    if cfg.rollout_lock_path.exists():
        return finish("REFUSED", "staged_rollout_in_progress")

    rollout_mod = None
    lock_written = False
    try:
        if write_lock is not None:
            write_lock()
        else:
            rollout_mod = _load_staged_rollout()
            rollout_mod.write_rollout_lock()
        lock_written = True
    except Exception as exc:  # noqa: BLE001 - fail closed before any mutation
        return finish("REFUSED", f"rollout_lock_failed:{exc}")

    # Serialize mirror, publication, selector, rollback and helper refresh,
    # including NOOP retries, against the installer and other deploys.
    try:
        pending = cfg.state_dir / "pending-release"
        if pending.exists() or pending.is_symlink():
            return finish("REFUSED", "pending_release_recovery_required")
        try:
            (ensure_mirror or ensure_bare_mirror)(cfg)
            target = (resolve_sha or resolve_target_sha)(cfg)
        except (OSError, RuntimeError) as exc:
            return finish("REFUSED", f"mirror_failed:{exc}")
        report.target_sha = target

        try:
            previous = (
                current_sha(cfg)
                if current_sha is not None
                else _current_release_sha(cfg.aicc_root)
            )
        except RuntimeError as exc:
            return finish("REFUSED", f"current_selector_failed:{exc}")
        report.previous_sha = previous or ""

        if previous == target and (cfg.release_root / target).is_dir():
            # A selector match alone does not prove a completed deployment.
            # Revalidate the attested tree and live lanes before reporting NOOP
            # or refreshing privileged helpers from the selected release.
            try:
                _tx().verify_release_manifest(
                    cfg.release_root / target,
                    cfg.state_dir / "releases" / f"{target}.json",
                    target,
                    repo_root=cfg.bare_mirror,
                    trusted_uid=cfg.trusted_uid,
                    trusted_gid=cfg.trusted_gid,
                )
                lanes = (
                    load_installed_lane_registry(cfg) if load_lanes is None else
                    load_lanes(cfg.lane_registry, expected_uid=cfg.trusted_uid,
                               expected_gid=cfg.trusted_gid)
                )
                for unit in active_registered_lanes(lanes, show=show):
                    if not _wait_lane_ready(
                        unit, timeout=cfg.readiness_timeout,
                        poll_interval=cfg.poll_interval,
                        show=show or _systemctl_show,
                    ):
                        return finish("REFUSED", f"current_lane_not_ready:{unit}")
            except Exception as exc:  # noqa: BLE001 - NOOP must fail closed
                return finish("REFUSED", f"current_release_unverified:{exc}")
            try:
                refreshed = refresh_root_helpers(cfg, target, install_tx=_install_tx)
            except OSError as exc:
                return finish("REFUSED", f"helper_refresh_failed:{exc}")
            if refreshed:
                report.steps.append("helpers:" + ",".join(refreshed))
            return finish("NOOP", "already_at_target")

        gate = dependency_gate or _dependency_change_refuses
        refused = gate(cfg, previous, target)
        if refused:
            return finish("REFUSED", refused)

        # Publish -> verify -> select -> rollout (order is the acceptance contract).
        try:
            if stage is not None:
                stage(cfg, target)
                report.steps.append("publish")
            else:
                stage_release_tree(cfg, target, install_tx=_tx())
                report.steps.append("publish")
            # stage_release_tree already verifies; record the explicit step for tests.
            report.steps.append("verify")

        except Exception as exc:  # noqa: BLE001 - surface as REFUSED, never half-selected
            return finish("REFUSED", f"publish_select_failed:{exc}")

        try:
            lanes = (
                load_installed_lane_registry(cfg) if load_lanes is None else
                load_lanes(cfg.lane_registry, expected_uid=cfg.trusted_uid,
                           expected_gid=cfg.trusted_gid)
            )
        except Exception as exc:  # noqa: BLE001
            return finish("REFUSED", f"lane_registry_failed:{exc}")

        to_restart = active_registered_lanes(lanes, show=show)
        stopped = [unit for unit in lanes if unit not in to_restart]
        for unit in stopped:
            report.steps.append(f"untouched:{unit}")

        try:
            if hold_timers is not None:
                hold_timers()
            else:
                if rollout_mod is None:
                    rollout_mod = _load_staged_rollout()
                rollout_mod.hold_lane_mutating_timers(rollout_mod.Systemd())

            if select is not None:
                select(target)
            else:
                transaction = _tx().FileTransaction(Path("/"), cfg.state_dir)
                transaction.select_release(target, cfg.bare_mirror)
            report.steps.append("select")

            if rollout is not None:
                ok, detail = rollout(to_restart)
            else:
                ok, detail = rollout_active_lanes(
                    to_restart,
                    readiness_timeout=cfg.readiness_timeout,
                    poll_interval=cfg.poll_interval,
                    drain_timeout=cfg.drain_timeout,
                    show=show,
                )
            report.steps.append("rollout")
            if not ok:
                try:
                    _rollback_selector(_tx(), cfg, restore_previous=restore_previous)
                except Exception as exc:  # noqa: BLE001 - preserve pending recovery
                    return finish("REFUSED", f"rollback_failed:{detail}:{exc}")
                report.steps.append("rollback")
                return finish("ROLLED_BACK", detail)
        except Exception as exc:  # noqa: BLE001
            if "select" in report.steps:
                try:
                    _rollback_selector(_tx(), cfg, restore_previous=restore_previous)
                except Exception as rollback_exc:  # noqa: BLE001
                    return finish("REFUSED", f"rollback_failed:{exc}:{rollback_exc}")
                return finish("ROLLED_BACK", f"rollout_failed:{exc}")
            return finish("REFUSED", f"rollout_lock_or_select_failed:{exc}")
        finally:
            if lock_written and release_timers is not None:
                release_timers()
            elif lock_written and rollout_mod is not None:
                rollout_mod.release_lane_mutating_timers(rollout_mod.Systemd())

        if clear_pending is not None:
            clear_pending()
        else:
            _tx().FileTransaction(Path("/"), cfg.state_dir).pending_release.unlink(
                missing_ok=True
            )

        # Only after DEPLOYED (lanes ready). ROLLED_BACK returns above and must
        # leave the previously installed root helpers alone.
        helper_tx = None
        candidate = _install_tx
        if candidate is None:
            try:
                candidate = _tx()
            except Exception:  # noqa: BLE001 - fall back to portable install
                candidate = None
        if candidate is not None and hasattr(candidate, "_atomic_bytes"):
            helper_tx = candidate
        refreshed = refresh_root_helpers(cfg, target, install_tx=helper_tx)
        base = detail if to_restart else "selected_no_active_lanes"
        if refreshed:
            report.steps.append("helpers:" + ",".join(refreshed))
            if "aicc-agent-launcher" in refreshed:
                # Socket-activated instances keep the old binary until the next
                # connection; do not kill in-flight agent runs.
                report.steps.append("launcher_refreshed_next_connection")
            base = f"{base} helpers:{','.join(refreshed)}"
        return finish("DEPLOYED", base)
    finally:
        if remove_lock is not None:
            remove_lock()
        elif rollout_mod is not None:
            rollout_mod.remove_rollout_lock()


def _rollback_selector(
    install_tx,
    cfg: DeployConfig,
    *,
    restore_previous: Callable[[], None] | None,
) -> None:
    if restore_previous is not None:
        restore_previous()
        return
    transaction = install_tx.FileTransaction(Path("/"), cfg.state_dir)
    transaction._restore_release_selector(clear_pending=True)


def main(argv: list[str] | None = None) -> int:
    defaults = DeployConfig()
    parser = argparse.ArgumentParser(
        description="Stage an immutable root-owned worker release for origin/main"
    )
    parser.add_argument("--bare-mirror", type=Path, default=defaults.bare_mirror)
    parser.add_argument("--aicc-root", type=Path, default=defaults.aicc_root)
    parser.add_argument("--release-root", type=Path, default=defaults.release_root)
    parser.add_argument("--state-dir", type=Path, default=defaults.state_dir)
    parser.add_argument("--runtime-venv", type=Path, default=defaults.runtime_venv)
    parser.add_argument("--lane-registry", type=Path, default=defaults.lane_registry)
    parser.add_argument(
        "--remote-url", default=os.environ.get("AICC_RELEASE_REMOTE_URL") or None
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--rollout-lock",
        type=Path,
        default=defaults.rollout_lock_path,
    )
    parser.add_argument("--readiness-timeout", type=float, default=120.0)
    parser.add_argument("--drain-timeout", type=float, default=defaults.drain_timeout)
    args = parser.parse_args(argv)
    cfg = DeployConfig(
        bare_mirror=args.bare_mirror,
        aicc_root=args.aicc_root,
        release_root=args.release_root,
        state_dir=args.state_dir,
        runtime_venv=args.runtime_venv,
        lane_registry=args.lane_registry,
        remote_url=args.remote_url,
        remote=args.remote,
        branch=args.branch,
        rollout_lock_path=args.rollout_lock,
        readiness_timeout=args.readiness_timeout,
        drain_timeout=args.drain_timeout,
        trusted_uid=os.geteuid() if hasattr(os, "geteuid") else 0,
        trusted_gid=os.getegid() if hasattr(os, "getegid") else 0,
    )
    report = deploy_once(cfg)
    print(report.line())
    return 0 if report.outcome in {"NOOP", "DEPLOYED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
