#!/usr/bin/env python3
"""Root-owned control-host release deploy: merged main -> immutable release.

Why this exists: the voynadmin self-deploy tick cannot write the root-owned
``/opt/aicc`` (and must not be able to), so a control host could only be
updated by hand.  This oneshot runs as root and does ONLY what is safe to do
unattended.  Anything else stops with ``MANUAL`` and waits for a human.

Rules (security review ``control-self-deploy-A-criteria``):

* no arguments and no environment are read: the source, paths and policy are
  constants below; the only inputs are the root-owned mirror and the current
  selector;
* the target is the tip of ``main`` in the root-owned bare mirror (fetched
  over anonymous https from a public repository, fsck-checked, no hooks, no
  credentials); it must descend from the release that is current now;
* a diff that touches the deploy mechanism itself, systemd units, sudoers, the
  installer, dependency manifests or database migrations is NOT deployed
  automatically (``MANUAL``): otherwise any merged change would run as root on
  the next tick;
* the tree is exported with ``git archive`` (no checkout, no filters, no
  hooks), made root-owned and not writable, free of setuid files and of
  symlinks that leave the tree, and published through the installer's own
  atomic primitives;
* the ``.venv`` of the new release is a copy of the current release's
  root-owned venv (dependencies are unchanged by rule, so nothing is
  installed); a venv that is a link, or owned by anyone else, stops the deploy;
* root never executes the new release's code before the switch.  A health
  check runs it after the switch as an unprivileged account with an empty
  environment; a failure restores the previous selector;
* migrations are a separate step (voynadmin, later PR): until it exists,
  a release with migrations is ``MANUAL``.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REMOTE_URL = "https://github.com/voyn88/ai-command-center.git"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SELECTOR = re.compile(r"^releases/([0-9a-f]{40})$")

#: Paths whose change must never be deployed unattended (see module docstring).
_MECHANISM = re.compile(
    "|".join(
        (
            r"^ops/aicc_control_release_deploy\.py$",
            r"^ops/aicc_release_deploy\.py$",
            r"^ops/aicc_install_transaction\.py$",
            r"^ops/aicc_exact_sha_bootstrap\.py$",
            r"^ops/aicc_staged_worker_rollout\.py$",
            r"^ops/[^/]*\.sh$",
            r"^deploy/systemd/",
            r"^deploy/sudoers\.d/",
            r"^deploy/sysusers\.d/",
            r"^deploy/tmpfiles\.d/",
            r"^deploy/install-[^/]*\.sh$",
            r"^deploy/aicc/",
            r"^command_center/ops/(disk_guard|self_heal|status_beacon|host_care_common)\.py$",
        )
    )
)
_DEPENDENCY = re.compile(
    r"(^|/)(uv\.lock|pyproject\.toml|requirements[^/]*\.txt|package(-lock)?\.json)$"
)
_MIGRATION = re.compile(r"^command_center/db/(sql/|migrations\.py$)")

_GIT_FLAGS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "transfer.fsckObjects=true",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "credential.helper=",
)
_CLEAN_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}
_OPS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class ControlConfig:
    mirror: Path = Path("/var/lib/aicc-principal-isolation/mirror.git")
    aicc_root: Path = Path("/opt/aicc")
    release_root: Path = Path("/opt/aicc/releases")
    state_dir: Path = Path("/var/lib/aicc-principal-isolation")
    lock_path: Path = Path("/run/aicc-control-release.lock")
    rollout_lock: Path = Path("/run/aicc-staged-rollout.lock")
    remote_url: str = REMOTE_URL
    branch: str = "main"
    health_user: str = "voynadmin"
    git_timeout: int = 300
    trusted_uid: int = 0
    trusted_gid: int = 0


@dataclass(slots=True)
class Report:
    outcome: str = ""  # NOOP | DEPLOYED | MANUAL | REFUSED | ROLLED_BACK
    detail: str = ""
    target_sha: str = ""
    previous_sha: str = ""
    steps: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"{self.outcome} {self.target_sha or '-'} {self.detail or '-'}"


# --------------------------------------------------------------------------
# small helpers


def _run(
    argv: list[str],
    *,
    timeout: int,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_CLEAN_ENV if env is None else env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")


def _git(cfg: ControlConfig, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", *_GIT_FLAGS, "--git-dir", str(cfg.mirror), *args],
        timeout=cfg.git_timeout,
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


def current_release_sha(cfg: ControlConfig) -> str | None:
    """The sha ``<aicc_root>/current`` selects, or None when it is absent.
    Anything that is not exactly ``releases/<40-hex>`` is an error."""
    link = cfg.aicc_root / "current"
    try:
        info = link.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("release selector is not a symlink")
    match = _SELECTOR.fullmatch(os.readlink(link))
    if match is None:
        raise RuntimeError("release selector is not an immutable release")
    return match.group(1)


def ensure_mirror(cfg: ControlConfig) -> None:
    """Create/refresh the root-owned bare mirror (public repo, no credentials)."""
    if not cfg.remote_url.startswith("https://"):
        raise RuntimeError("release remote must be an https URL")
    if not cfg.mirror.exists():
        cfg.mirror.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        made = _run(
            ["git", "init", "--bare", str(cfg.mirror)], timeout=cfg.git_timeout
        )
        if made.returncode != 0:
            raise RuntimeError("bare mirror init failed")
        added = _git(cfg, ["remote", "add", "origin", cfg.remote_url])
        if added.returncode != 0:
            raise RuntimeError("bare mirror remote add failed")
    # re-assert the one allowed source every tick
    reset = _git(cfg, ["remote", "set-url", "origin", cfg.remote_url])
    if reset.returncode != 0:
        raise RuntimeError("bare mirror set-url failed")
    fetched = _git(
        cfg,
        [
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            f"+refs/heads/{cfg.branch}:refs/heads/{cfg.branch}",
        ],
    )
    if fetched.returncode != 0:
        raise RuntimeError(
            f"bare mirror fetch failed: {(fetched.stderr or '').strip()[:120]}"
        )


def resolve_target(cfg: ControlConfig) -> str:
    parsed = _git(cfg, ["rev-parse", f"refs/heads/{cfg.branch}"])
    sha = parsed.stdout.strip() if parsed.returncode == 0 else ""
    if not _SHA.fullmatch(sha):
        raise RuntimeError("cannot resolve a 40-hex target from the mirror")
    return sha


# --------------------------------------------------------------------------
# policy (pure given the changed-file list)


def classify_change(paths: list[str]) -> str | None:
    """None when the change may be deployed unattended, else why not."""
    mechanism = [p for p in paths if _MECHANISM.search(p)]
    if mechanism:
        return f"deploy_mechanism_changed: {mechanism[:3]}"
    deps = [p for p in paths if _DEPENDENCY.search(p)]
    if deps:
        return f"dependencies_changed: {deps[:3]}"
    migrations = [p for p in paths if _MIGRATION.search(p)]
    if migrations:
        return f"migrations_changed: {migrations[:3]}"
    return None


# --------------------------------------------------------------------------
# staging


def _check_tree(root: Path) -> None:
    """The exported tree has no setuid/setgid file and no symlink that leaves it."""
    base = root.resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = (path.parent / os.readlink(path)).resolve()
                if target != base and base not in target.parents:
                    raise RuntimeError(f"symlink leaves the release tree: {name}")
            elif info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                raise RuntimeError(f"setuid/setgid file in the release tree: {name}")


def _freeze(path: Path, *, uid: int, gid: int) -> None:
    for current, dirs, files in os.walk(path, followlinks=False, topdown=False):
        for name in dirs + files:
            item = Path(current) / name
            try:
                os.lchown(item, uid, gid)
            except AttributeError:
                pass
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            mode = stat.S_IMODE(info.st_mode) & ~0o7222
            if stat.S_ISDIR(info.st_mode):
                mode |= 0o555
            os.chmod(item, mode)
    try:
        os.lchown(path, uid, gid)
    except AttributeError:
        pass
    os.chmod(path, 0o555)  # nosec B103  release dirs are dr-xr-xr-x


def _trusted_venv(cfg: ControlConfig, sha: str) -> Path:
    venv = cfg.release_root / sha / ".venv"
    info = venv.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("current release .venv is not a real directory")
    if info.st_uid != cfg.trusted_uid or info.st_gid != cfg.trusted_gid:
        raise RuntimeError("current release .venv is not trusted-owned")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError("current release .venv is group/other writable")
    return venv


def stage_release(cfg: ControlConfig, sha: str, current: str, install_tx) -> Path:
    manifest = cfg.state_dir / "releases" / f"{sha}.json"
    release_dir = cfg.release_root / sha
    install_tx.reconcile_release_publication(
        cfg.release_root,
        manifest,
        sha,
        state_dir=cfg.state_dir,
        trusted_uid=cfg.trusted_uid,
        trusted_gid=cfg.trusted_gid,
    )
    if release_dir.is_dir():
        install_tx.verify_release_manifest(
            release_dir,
            manifest,
            sha,
            repo_root=cfg.mirror,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        return release_dir
    source_venv = _trusted_venv(cfg, current)
    staging = cfg.release_root / f".stage-{sha}.{os.getpid()}"
    tarball = cfg.release_root / f".stage-{sha}.{os.getpid()}.tar"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o755)
    try:
        archived = _git(cfg, ["archive", "--format=tar", "-o", str(tarball), sha])
        if archived.returncode != 0:
            raise RuntimeError(f"git archive failed rc={archived.returncode}")
        extracted = _run(
            [
                "tar",
                "-xf",
                str(tarball),
                "-C",
                str(staging),
                "--no-same-owner",
                "--no-same-permissions",
            ],
            timeout=cfg.git_timeout,
        )
        if extracted.returncode != 0:
            raise RuntimeError("tar extract failed")
        tarball.unlink(missing_ok=True)
        if (staging / ".venv").exists() or (staging / ".venv").is_symlink():
            raise RuntimeError("archive unexpectedly contains .venv")
        _check_tree(staging)
        shutil.copytree(source_venv, staging / ".venv", symlinks=True)
        _freeze(staging, uid=cfg.trusted_uid, gid=cfg.trusted_gid)
        manifest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        install_tx.record_release_manifest(
            staging,
            manifest,
            sha,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        published = install_tx.publish_release_tree(
            staging,
            cfg.release_root,
            manifest,
            sha,
            trusted_uid=cfg.trusted_uid,
            trusted_gid=cfg.trusted_gid,
        )
        staging = None  # type: ignore[assignment]
    except BaseException:
        tarball.unlink(missing_ok=True)
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    install_tx.verify_release_manifest(
        published,
        manifest,
        sha,
        repo_root=cfg.mirror,
        trusted_uid=cfg.trusted_uid,
        trusted_gid=cfg.trusted_gid,
    )
    return published


# --------------------------------------------------------------------------
# health, switch, rollback


def health_check(cfg: ControlConfig, sha: str) -> tuple[bool, str]:
    """Run the NEW code only as an unprivileged account with an empty
    environment (never as root)."""
    release = cfg.release_root / sha
    done = _run(
        [
            "runuser",
            "-u",
            cfg.health_user,
            "--",
            "env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/nonexistent",
            str(release / ".venv" / "bin" / "python"),
            "-B",
            "-c",
            "import command_center, command_center.db",
        ],
        timeout=120,
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(release),
    )
    return done.returncode == 0, (done.stderr or done.stdout).strip()[:160]


def deploy_once(
    cfg: ControlConfig | None = None,
    *,
    install_tx=None,
    ensure: Callable[[ControlConfig], None] | None = None,
    resolve: Callable[[ControlConfig], str] | None = None,
    health: Callable[[ControlConfig, str], tuple[bool, str]] | None = None,
    lock: bool = True,
) -> Report:
    cfg = cfg or ControlConfig()
    report = Report()

    def finish(outcome: str, detail: str) -> Report:
        report.outcome, report.detail = outcome, detail
        return report

    if cfg.rollout_lock.exists():
        return finish("REFUSED", "staged_rollout_in_progress")
    lock_handle = None
    if lock:
        try:
            lock_handle = open(cfg.lock_path, "a+")  # noqa: SIM115
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return finish("REFUSED", "another_deploy_running")
    try:
        try:
            (ensure or ensure_mirror)(cfg)
            target = (resolve or resolve_target)(cfg)
        except (OSError, RuntimeError) as exc:
            return finish("REFUSED", f"mirror_failed:{exc}")
        report.target_sha = target
        try:
            current = current_release_sha(cfg)
        except (OSError, RuntimeError) as exc:
            return finish("REFUSED", f"current_selector_failed:{exc}")
        if current is None:
            return finish("MANUAL", "no_current_release")
        report.previous_sha = current
        if current == target:
            return finish("NOOP", "already_at_target")
        if _git(cfg, ["cat-file", "-e", f"{current}^{{commit}}"]).returncode != 0:
            return finish("MANUAL", "current_release_not_in_mirror")
        if _git(cfg, ["merge-base", "--is-ancestor", current, target]).returncode != 0:
            return finish("REFUSED", "target_does_not_descend_from_current")
        changed = _git(cfg, ["diff", "--name-only", f"{current}..{target}"])
        if changed.returncode != 0:
            return finish("REFUSED", "diff_failed")
        why = classify_change(changed.stdout.splitlines())
        if why:
            return finish("MANUAL", why)

        tx = install_tx or _load_install_transaction()
        try:
            stage_release(cfg, target, current, tx)
            report.steps.append("publish")
        except Exception as exc:  # noqa: BLE001 - never leave a half-selected release
            return finish("REFUSED", f"publish_failed:{exc}")
        try:
            transaction = tx.FileTransaction(Path("/"), cfg.state_dir)
            transaction.select_release(target, cfg.mirror)
            report.steps.append("select")
        except Exception as exc:  # noqa: BLE001
            return finish("REFUSED", f"select_failed:{exc}")
        ok, detail = (health or health_check)(cfg, target)
        if not ok:
            try:
                transaction._restore_release_selector(clear_pending=True)
            except Exception as exc:  # noqa: BLE001 - keep the pending marker
                return finish("REFUSED", f"rollback_failed:{detail}:{exc}")
            report.steps.append("rollback")
            return finish("ROLLED_BACK", f"health_failed:{detail}")
        transaction.pending_release.unlink(missing_ok=True)
        return finish("DEPLOYED", "selected")
    finally:
        if lock_handle is not None:
            lock_handle.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        # nothing is configurable from the caller: not arguments, not paths
        print("REFUSED - this deploy takes no arguments", file=sys.stderr)
        return 2
    report = deploy_once()
    print(report.line())
    return 0 if report.outcome in {"NOOP", "DEPLOYED", "MANUAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
