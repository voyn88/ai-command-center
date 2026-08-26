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
import re
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
            sys.executable, "-c",
            "import command_center.orchestrator.review_merge, "
            "command_center.orchestrator.planner, "
            "command_center.worker.handlers",
        ],
        timeout,
        cwd=repo_path,
    )


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

    fetched = _git(repo_path, ["fetch", cfg.remote, cfg.branch], timeout)
    if fetched.returncode != 0:
        return finish("failed", f"fetch_failed: {fetched.stderr.strip()[:100]}")
    current = _git(repo_path, ["rev-parse", "HEAD"], timeout)
    target = _git(repo_path, ["rev-parse", f"{cfg.remote}/{cfg.branch}"], timeout)
    if current.returncode != 0 or target.returncode != 0:
        return finish("failed", "rev_parse_failed")
    report.previous_sha = current.stdout.strip()
    report.target_sha = target.stdout.strip()
    if report.previous_sha == report.target_sha:
        return finish("noop", report.target_sha)

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

    moved = _git(repo_path, ["reset", "--hard", report.target_sha], timeout)
    if moved.returncode != 0:
        return finish("failed", f"reset_failed: {moved.stderr.strip()[:100]}")
    report.steps.append(f"checkout_moved:{report.target_sha}")

    # Smoke BEFORE migrations (review of cff672a): a broken tree must be
    # discovered while the database is still untouched -- the cheapest
    # failure order is the one with nothing to unwind.
    smoke = _import_smoke(repo_path, timeout)
    if smoke.returncode != 0:
        _git(repo_path, ["reset", "--hard", report.previous_sha], timeout)
        return finish(
            "rolled_back", f"import_smoke_failed: {smoke.stderr.strip()[:150]}"
        )
    report.steps.append("import_smoke_passed")

    if cfg.migrate:
        migrated = _run_migrations(repo_path, timeout)
        if migrated.returncode != 0:
            _git(repo_path, ["reset", "--hard", report.previous_sha], timeout)
            return finish(
                "rolled_back",
                f"migrations_failed: {(migrated.stderr or migrated.stdout).strip()[:150]}",
            )
        report.steps.append("migrations_applied")

    # Applied migrations are deliberately NOT rolled back on a later restart
    # failure: this codebase's migration policy is expand-contract (each
    # migration is backward-compatible with the previous code, and the
    # legacy path is removed only by a separately accepted change), so the
    # PREVIOUS code running against the migrated schema is the supported
    # state by design. An automatic `downgrade` here would be the opposite
    # of safety: it is the one operation that can DROP data, which is why
    # the CLI gates it behind --yes-i-understand-this-drops-data.
    failure = _restart_services(cfg, report)
    if failure is not None:
        _git(repo_path, ["reset", "--hard", report.previous_sha], timeout)
        # Best effort: put the services back on the previous code too --
        # half old, half new is the state this rollback exists to prevent.
        _restart_services(cfg, report)
        return finish("rolled_back", failure)

    return finish("deployed", report.target_sha)
