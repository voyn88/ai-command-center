"""Per-host self-deploy: merged -> deployed without a human (VOYN-W0-AICC-
DEPLOY-AUTOMATION).

Merged is not deployed: the loop's ticks and workers execute whatever their
host's checkout happens to hold, and until now moving it was a manual
`git reset` + service restart on every host after every merge -- so the loop
kept running OLD versions of itself indefinitely (live 2026-08-26: the
freshly merged auto-acceptance pipeline was inert until a hand deploy).

Architecture: each host deploys ITSELF from a periodic oneshot tick --
control-01 cannot reach worker-01 over ssh (verified live), and per-host
ownership needs no cross-host credentials at all. Control-plane ticks are
oneshot units that re-exec from the checkout, so for them a deploy is just
the checkout move plus migrations; worker daemons are long-running, so the
worker host also restarts its services (SIGTERM is already a graceful
drain: the daemon finishes the item in hand and claims no more).

Fail-closed by construction; every refusal is data in the report:

- fast-forward only -- a diverged checkout is an incident, never resolved
  by discarding whatever diverged it;
- a dirty tree refuses (someone is editing where only the deployer should
  write);
- a change to dependency manifests (uv.lock / pyproject / requirements)
  refuses: hosts run pinned runtime venvs with no installer on purpose, so
  a dependency change is a provisioning act, not a code move;
- migrations (control only) run BEFORE services would pick the new code
  up, expand-contract discipline being the migrations' own contract;
- a failed restart or failed import smoke ROLLS BACK to the previous sha
  and restarts again -- the host must never be left half-deployed;
- the deployed sha + outcome are recorded in a host-local provenance file
  and journald (the tick's stdout), the evidence the DEPLOY task's
  acceptance reads.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SelfDeployConfig", "SelfDeployReport", "self_deploy_once"]

_DEPENDENCY_MANIFESTS = re.compile(
    r"(^|/)(uv\.lock|pyproject\.toml|requirements[^/]*\.txt|package(-lock)?\.json)$"
)


@dataclass(frozen=True, slots=True)
class SelfDeployConfig:
    remote: str = "origin"
    branch: str = "main"
    #: Services this host must restart to pick the new code up. Empty for a
    #: control host whose ticks are oneshot units (they re-exec from the
    #: checkout on their next firing anyway).
    services: tuple[str, ...] = ()
    #: Run `python -m command_center.db upgrade` after moving the checkout.
    #: Control host only -- the worker role holds no DDL privilege.
    migrate: bool = False
    #: Host-local provenance record (sha, outcome, timestamp per line).
    provenance_path: str = "~/.aicc-self-deploy-provenance.jsonl"
    command_timeout: int = 300
    #: Marker file the staged worker rollout (ops/aicc_staged_worker_rollout.py)
    #: holds for its entire duration. A restart issued by this tick while the
    #: rollout is mid-drain is exactly what raced the rollout's own `stop` of
    #: the canary lane on worker-01 (live 2026-09-08); refusing while the
    #: marker exists means the next tick (5 minutes later, after the rollout
    #: has finished and removed it) picks the deploy back up instead.
    rollout_lock_path: str = "/run/aicc-staged-rollout.lock"
    #: Optional immutable release root. When set, self-deploy stages tracked
    #: files into `<release_root>/releases/<sha>` and atomically points
    #: `<release_root>/current` at that release instead of asking systemd units
    #: to execute directly from the mutable source clone.
    release_root: str | None = None
    #: Runtime venv to expose inside each immutable release. The venv itself is
    #: still provisioned by the installer; release staging only binds it into
    #: the immutable source tree so units can keep using
    #: `/opt/aicc/current/.venv/bin/python`.
    release_venv: str | None = None


@dataclass(slots=True)
class SelfDeployReport:
    outcome: str = ""  # noop | deployed | refused | rolled_back | failed
    detail: str = ""
    previous_sha: str = ""
    target_sha: str = ""
    steps: list[str] = field(default_factory=list)


def _run_bounded(
    args: list[str], timeout: int, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """subprocess.run that converts TimeoutExpired into an ordinary failed
    result (rc 124, the shell `timeout` convention) instead of raising --
    review of f794b3e: a raise AFTER `reset --hard` (during migrations,
    smoke, or restart) would bypass rollback and provenance entirely,
    leaving the host half-deployed -- exactly what this module promises
    never to happen."""
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 124, "", f"timed out after {timeout}s"
        )


def _git(repo_path: str, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return _run_bounded(["git", *args], timeout, cwd=repo_path)


def _systemctl(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Passwordless-sudo systemctl -- the exact grant the hosts already carry
    (`sudo -n`: never prompt; a missing grant is a refusal, not a hang)."""
    return _run_bounded(["sudo", "-n", "systemctl", *args], timeout)


def _run_migrations(repo_path: str, timeout: int) -> subprocess.CompletedProcess[str]:
    # cwd=repo_path is load-bearing (review of f794b3e): the runtime package
    # is imported from the checkout, not installed into the venv -- without
    # it a fresh interpreter could execute the OLD tree's migration code.
    return _run_bounded(
        [sys.executable, "-m", "command_center.db", "upgrade"],
        timeout,
        cwd=repo_path,
    )


def _import_smoke(repo_path: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """The cheapest deploy smoke that still catches a broken checkout: the
    modules every tick and worker imports must import from the NEW tree."""
    return _run_bounded(
        [
            sys.executable,
            "-c",
            (
                "import command_center.orchestrator.review_merge, "
                "command_center.orchestrator.planner, "
                "command_center.worker.handlers"
            ),
        ],
        timeout,
        cwd=repo_path,
    )


def _dispatch_smoke(repo_path: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """`backlog_dispatch_smoke()` (0021) through the deployed code and the
    control plane's own role: a read of `backlog_eligible` and the wave
    candidate under the same SECURITY DEFINER the planner's dispatch uses."""
    return _run_bounded(
        [sys.executable, "-m", "command_center.db", "backlog-plan", "--smoke"],
        timeout,
        cwd=repo_path,
    )


def _selector_for_sha(sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("release sha is invalid")
    return f"releases/{sha}"


def _current_selector(current_link: Path) -> str:
    try:
        info = current_link.lstat()
    except FileNotFoundError:
        return "ABSENT"
    if not stat.S_ISLNK(info.st_mode):
        raise RuntimeError("release selector is not a symlink")
    selector = os.readlink(current_link)
    if not re.fullmatch(r"releases/[0-9a-f]{40}", selector):
        if os.path.isabs(selector):
            return selector
        raise RuntimeError("release selector is invalid")
    return selector


def _point_current_at(current_link: Path, selector: str) -> None:
    if (
        selector != "ABSENT"
        and not os.path.isabs(selector)
        and not re.fullmatch(r"releases/[0-9a-f]{40}", selector)
    ):
        raise RuntimeError("release selector is invalid")
    tmp = current_link.with_name(f".{current_link.name}.next")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    if selector == "ABSENT":
        try:
            current_link.unlink()
        except FileNotFoundError:
            pass
        return
    tmp.symlink_to(selector)
    os.replace(tmp, current_link)


def _tracked_files(repo_path: str, timeout: int) -> list[str]:
    listed = _git(repo_path, ["ls-files", "-z"], timeout)
    if listed.returncode != 0:
        raise RuntimeError("release file listing failed")
    return [name for name in listed.stdout.split("\0") if name]


def _stage_immutable_release(
    repo_path: str, cfg: SelfDeployConfig, sha: str
) -> tuple[Path, str]:
    if cfg.release_root is None:
        return Path(repo_path), "ABSENT"
    release_root = Path(cfg.release_root)
    selector = _selector_for_sha(sha)
    release_dir = release_root / selector
    current_link = release_root / "current"
    previous_selector = _current_selector(current_link)
    if release_dir.exists():
        return release_dir, previous_selector

    staging = release_root / "releases" / f".{sha}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o755)
    try:
        for name in _tracked_files(repo_path, cfg.command_timeout):
            source = Path(repo_path) / name
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        if cfg.release_venv is not None:
            venv = Path(cfg.release_venv)
        else:
            venv = Path(repo_path) / ".venv"
        if venv.exists():
            (staging / ".venv").symlink_to(venv)
        (staging / ".aicc-release-sha").write_text(sha + "\n", encoding="ascii")
        os.rename(staging, release_dir)
    except (OSError, RuntimeError, shutil.Error):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return release_dir, previous_selector


def _record_provenance(cfg: SelfDeployConfig, report: SelfDeployReport) -> None:
    try:
        path = Path(cfg.provenance_path).expanduser()
        row = {
            "at": _datetime.datetime.now(_datetime.UTC).isoformat(),
            "outcome": report.outcome,
            "detail": report.detail,
            "previous_sha": report.previous_sha,
            "target_sha": report.target_sha,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # Provenance is evidence, not a gate: journald (the tick's own
        # stdout) still carries the report even when the file cannot.
        pass


def _restart_services(
    cfg: SelfDeployConfig, report: SelfDeployReport
) -> str | None:
    for service in cfg.services:
        restarted = _systemctl(["restart", service], cfg.command_timeout)
        if restarted.returncode != 0:
            return f"restart_failed:{service}:{restarted.stderr.strip()[:80]}"
        active = _systemctl(["is-active", service], cfg.command_timeout)
        if active.returncode != 0 or active.stdout.strip() != "active":
            return f"service_not_active_after_restart:{service}"
        report.steps.append(f"restarted:{service}")
    return None


def self_deploy_once(
    repo_path: str, cfg: SelfDeployConfig | None = None
) -> SelfDeployReport:
    cfg = cfg or SelfDeployConfig()
    report = SelfDeployReport()
    timeout = cfg.command_timeout

    def finish(outcome: str, detail: str) -> SelfDeployReport:
        report.outcome, report.detail = outcome, detail
        if outcome != "noop":
            _record_provenance(cfg, report)
        return report

    if cfg.rollout_lock_path and Path(cfg.rollout_lock_path).expanduser().exists():
        return finish("refused", "staged_rollout_in_progress")

    fetched = _git(repo_path, ["fetch", cfg.remote, cfg.branch], timeout)
    if fetched.returncode != 0:
        return finish("failed", f"fetch_failed: {fetched.stderr.strip()[:100]}")
    current = _git(repo_path, ["rev-parse", "HEAD"], timeout)
    target = _git(repo_path, ["rev-parse", f"{cfg.remote}/{cfg.branch}"], timeout)
    if current.returncode != 0 or target.returncode != 0:
        return finish("failed", "rev_parse_failed")
    report.previous_sha = current.stdout.strip()
    report.target_sha = target.stdout.strip()
    same_sha = report.previous_sha == report.target_sha
    if same_sha and cfg.release_root is None:
        return finish("noop", report.target_sha)

    if same_sha and cfg.release_root is not None:
        release_root = Path(cfg.release_root)
        try:
            current_selector = _current_selector(release_root / "current")
        except (OSError, RuntimeError) as exc:
            return finish("failed", f"release_selector_read_failed: {exc}")
        release_selector = _selector_for_sha(report.target_sha)
        if (
            current_selector == release_selector
            and (release_root / release_selector).is_dir()
        ):
            return finish("noop", report.target_sha)
    if not same_sha:
        ff = _git(
            repo_path,
            ["merge-base", "--is-ancestor", "HEAD", report.target_sha],
            timeout,
        )
        if ff.returncode != 0:
            return finish("refused", "non_fast_forward_checkout_diverged")
    dirty = _git(repo_path, ["status", "--porcelain"], timeout)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return finish("refused", "checkout_dirty")
    changed = _git(
        repo_path,
        ["diff", "--name-only", f"HEAD..{report.target_sha}"],
        timeout,
    )
    if changed.returncode != 0:
        return finish("failed", "diff_failed")
    manifests = [
        name for name in changed.stdout.splitlines()
        if _DEPENDENCY_MANIFESTS.search(name.strip())
    ]
    if manifests:
        return finish(
            "refused", f"dependency_change_requires_manual_deploy: {manifests[:3]}"
        )

    if not same_sha:
        moved = _git(repo_path, ["reset", "--hard", report.target_sha], timeout)
        if moved.returncode != 0:
            return finish("failed", f"reset_failed: {moved.stderr.strip()[:100]}")
        report.steps.append(f"checkout_moved:{report.target_sha}")
    else:
        report.steps.append(f"checkout_already_current:{report.target_sha}")

    try:
        runtime_path, previous_selector = _stage_immutable_release(
            repo_path, cfg, report.target_sha
        )
    except (OSError, RuntimeError, shutil.Error) as exc:
        _git(repo_path, ["reset", "--hard", report.previous_sha], timeout)
        return finish("failed", f"release_stage_failed: {exc}")
    selector_flipped = False
    if cfg.release_root is not None:
        report.steps.append(f"release_staged:{runtime_path}")

    def rollback(reason: str, *, services_touched: bool) -> SelfDeployReport:
        """VERIFIED restoration (review of 8d1f967: an unchecked
        `reset --hard` could itself fail or time out, leaving the host on
        new code while provenance claimed a successful rollback). The
        outcome is `rolled_back` only when the checkout provably sits at
        the previous sha again and the touched services are active on it;
        anything less is `failed` with `rollback_incomplete` -- an
        operator incident, never a false all-clear."""
        _git(repo_path, ["reset", "--hard", report.previous_sha], timeout)
        at = _git(repo_path, ["rev-parse", "HEAD"], timeout)
        if at.returncode != 0 or at.stdout.strip() != report.previous_sha:
            return finish("failed", f"rollback_incomplete_checkout: after {reason}")
        if selector_flipped and cfg.release_root is not None:
            try:
                _point_current_at(Path(cfg.release_root) / "current", previous_selector)
            except (OSError, RuntimeError) as exc:
                return finish(
                    "failed",
                    f"rollback_incomplete_release_selector: {exc}; after {reason}",
                )
        if services_touched:
            restore_failure = _restart_services(cfg, report)
            if restore_failure is not None:
                return finish(
                    "failed",
                    f"rollback_incomplete_services: {restore_failure}; after {reason}",
                )
        return finish("rolled_back", reason)

    # Smoke BEFORE migrations (review of cff672a): a broken tree must be
    # discovered while the database is still untouched -- the cheapest
    # failure order is the one with nothing to unwind.
    smoke = _import_smoke(str(runtime_path), timeout)
    if smoke.returncode != 0:
        return rollback(
            f"import_smoke_failed: {smoke.stderr.strip()[:150]}",
            services_touched=False,
        )
    report.steps.append("import_smoke_passed")

    if cfg.migrate:
        migrated = _run_migrations(str(runtime_path), timeout)
        if migrated.returncode != 0:
            # The checkout is restored, but earlier PENDING migrations may
            # have committed before the failing one (each migration is its
            # own transaction) -- provenance says so explicitly rather than
            # implying a pristine database (review of 8d1f967, medium).
            return rollback(
                "migrations_failed_database_may_hold_partial_migrations: "
                f"{(migrated.stderr or migrated.stdout).strip()[:130]}",
                services_touched=False,
            )
        # `db upgrade` runs the pending migrations AND unconditionally
        # re-asserts the grant matrix (roles.apply_table_grants) even when
        # no migration was pending -- so a clean exit is never provably a
        # no-op (review of 5eb6f62: "ran once" isn't "mutated"; review of
        # 15774a77: an "already up to date" schema can still see its grants
        # change). Rather than guess from exit code or stdout, every
        # successful invocation is recorded as a write that will NOT be
        # rolled back, which is true whether or not anything actually
        # changed.
        report.steps.append("database_upgrade_ran_not_rolled_back")
        # A migration can leave the schema importable yet unusable by the
        # control plane (0019 recreated a view under the wrong owner; every
        # planner tick died for 18 minutes while import-smoke was green).
        # Exercise exactly the privileges dispatch needs before any service
        # restarts; a refusal rolls the checkout and services back.
        dispatch_smoke = _dispatch_smoke(str(runtime_path), timeout)
        if dispatch_smoke.returncode != 0:
            return rollback(
                "dispatch_smoke_failed_after_migration: "
                f"{(dispatch_smoke.stderr or dispatch_smoke.stdout).strip()[:150]}",
                services_touched=False,
            )
        report.steps.append("dispatch_smoke_passed")

    if cfg.release_root is not None:
        try:
            _point_current_at(
                Path(cfg.release_root) / "current", _selector_for_sha(report.target_sha)
            )
        except (OSError, RuntimeError) as exc:
            return rollback(f"release_selector_failed: {exc}", services_touched=False)
        selector_flipped = True
        report.steps.append(f"release_selected:{report.target_sha}")

    # A completed `db upgrade` is deliberately NOT rolled back on a later
    # restart failure: this codebase's migration policy is expand-contract
    # (each migration is backward-compatible with the previous code, and the
    # legacy path is removed only by a separately accepted change), so the
    # PREVIOUS code running against the upgraded database (schema and
    # grants) is the supported state by design. An automatic `downgrade`
    # here would be the opposite of safety: it is the one operation that can
    # DROP data, which is why the CLI gates it behind
    # --yes-i-understand-this-drops-data.
    failure = _restart_services(cfg, report)
    if failure is not None:
        # Half old, half new is the state rollback exists to prevent -- so
        # the restored services are re-verified inside rollback() too.
        return rollback(failure, services_touched=True)

    return finish("deployed", report.target_sha)
