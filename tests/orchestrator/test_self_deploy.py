"""Per-host self-deploy (VOYN-W0-AICC-DEPLOY-AUTOMATION) on a REAL git
origin+clone pair; systemctl and migrations are faked at the module seam --
the git behavior (fast-forward, divergence, dirty tree, rollback) is the
substance under test and runs for real."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from command_center.deployment import self_deploy
from command_center.deployment.self_deploy import (
    SelfDeployConfig,
    self_deploy_once,
)


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo, message, filename="tracked.txt", content=None) -> str:
    (repo / filename).write_text(content if content is not None else message)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pair(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(origin))
    first = _commit(origin, "base")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    return origin, clone, first


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Fake the two privileged seams; git stays real."""
    recorded = {"systemctl": [], "migrate": 0, "smoke_rc": 0, "systemctl_rc": {}, "order": []}

    def fake_systemctl(args, timeout):
        recorded["systemctl"].append(args)
        recorded["order"].append(f"systemctl:{args[0]}")
        rc = recorded["systemctl_rc"].get(tuple(args[:1]), 0)
        out = "active" if args[0] == "is-active" and rc == 0 else ""
        return subprocess.CompletedProcess(args, rc, out, "" if rc == 0 else "boom")

    def fake_migrations(repo_path, timeout):
        recorded["migrate"] += 1
        recorded["order"].append("migrate")
        recorded["migrate_cwd"] = repo_path
        return subprocess.CompletedProcess([], 0, "", "")

    def fake_smoke(repo_path, timeout):
        recorded["smoke_cwd"] = repo_path
        return subprocess.CompletedProcess([], recorded["smoke_rc"], "", "import boom")

    def fake_dispatch_smoke(repo_path, timeout):
        recorded["dispatch_smoke"] = recorded.get("dispatch_smoke", 0) + 1
        recorded["order"].append("dispatch_smoke")
        return subprocess.CompletedProcess(
            [], recorded.get("dispatch_smoke_rc", 0), "", "permission denied for view"
        )

    monkeypatch.setattr(self_deploy, "_systemctl", fake_systemctl)
    monkeypatch.setattr(self_deploy, "_run_migrations", fake_migrations)
    monkeypatch.setattr(self_deploy, "_import_smoke", fake_smoke)
    monkeypatch.setattr(self_deploy, "_dispatch_smoke", fake_dispatch_smoke)
    return recorded


def _cfg(tmp_path, **overrides):
    values = {"provenance_path": str(tmp_path / "provenance.jsonl")}
    values.update(overrides)
    return SelfDeployConfig(**values)


def test_noop_when_already_at_origin(pair, calls, tmp_path):
    _origin, clone, first = pair
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert report.outcome == "noop"
    assert report.target_sha == first
    assert calls["systemctl"] == [] and calls["migrate"] == 0
    assert not (tmp_path / "provenance.jsonl").exists()  # noops leave no rows


def test_refuses_while_a_staged_rollout_holds_the_lock(pair, calls, tmp_path):
    """A self-deploy tick that fires while the staged rollout holds its lock
    must refuse before touching git or systemctl -- a restart issued mid-drain
    is exactly what raced the rollout's own `stop` of the canary lane on
    worker-01 (live 2026-09-08). The next tick, five minutes later once the
    rollout has finished and removed the lock, picks the deploy back up."""
    origin, clone, first = pair
    _commit(origin, "advance")  # a real fast-forward is available and skipped
    lock = tmp_path / "aicc-staged-rollout.lock"
    lock.write_text("12345\n", encoding="utf-8")
    cfg = _cfg(
        tmp_path,
        services=("voyn-aicc-worker.service",),
        rollout_lock_path=str(lock),
    )

    report = self_deploy_once(str(clone), cfg)

    assert (report.outcome, report.detail) == ("refused", "staged_rollout_in_progress")
    assert calls["systemctl"] == [] and calls["migrate"] == 0
    assert _git(clone, "rev-parse", "HEAD") == first  # untouched
    rows = [
        json.loads(line)
        for line in (tmp_path / "provenance.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome"] == "refused"
    assert rows[-1]["detail"] == "staged_rollout_in_progress"


def test_deploys_normally_once_the_rollout_lock_is_absent(pair, calls, tmp_path):
    origin, clone, _first = pair
    new = _commit(origin, "advance")
    cfg = _cfg(
        tmp_path,
        services=("voyn-aicc-worker.service",),
        rollout_lock_path=str(tmp_path / "no-such-lock"),
    )

    report = self_deploy_once(str(clone), cfg)

    assert (report.outcome, report.detail) == ("deployed", new)
    assert ["restart", "voyn-aicc-worker.service"] in calls["systemctl"]


def test_fast_forward_deploys_migrates_restarts_and_records(pair, calls, tmp_path):
    origin, clone, first = pair
    new = _commit(origin, "advance")
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",), migrate=True)
    report = self_deploy_once(str(clone), cfg)
    assert (report.outcome, report.detail) == ("deployed", new)
    assert report.previous_sha == first
    assert _git(clone, "rev-parse", "HEAD") == new
    assert calls["migrate"] == 1
    assert calls["migrate_cwd"] == str(clone)  # the NEW tree, review of f794b3e
    assert (
        report.steps.index("import_smoke_passed")
        < report.steps.index("database_upgrade_ran_not_rolled_back")
    )
    assert ["restart", "voyn-aicc-worker.service"] in calls["systemctl"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "provenance.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome"] == "deployed" and rows[-1]["target_sha"] == new


def test_immutable_release_is_staged_and_selected(pair, calls, tmp_path):
    origin, clone, _first = pair
    new = _commit(origin, "advance")
    release_root = tmp_path / "opt-aicc"
    runtime_venv = tmp_path / "runtime-venv"
    runtime_venv.mkdir()

    report = self_deploy_once(
        str(clone),
        _cfg(
            tmp_path,
            migrate=True,
            release_root=str(release_root),
            release_venv=str(runtime_venv),
        ),
    )

    release = release_root / "releases" / new
    assert (report.outcome, report.detail) == ("deployed", new)
    assert release.is_dir()
    assert (release / "tracked.txt").read_text() == "advance"
    assert (release / ".aicc-release-sha").read_text() == new + "\n"
    assert (release / ".venv").is_symlink()
    assert (release / ".venv").readlink() == runtime_venv
    assert (release_root / "current").readlink() == Path(f"releases/{new}")
    assert calls["smoke_cwd"] == str(release)
    assert calls["migrate_cwd"] == str(release)
    assert "release_selected:" + new in report.steps


def test_failed_restart_restores_previous_release_selector(pair, calls, tmp_path):
    origin, clone, first = pair
    release_root = tmp_path / "opt-aicc"
    old_release = release_root / "releases" / first
    old_release.mkdir(parents=True)
    (release_root / "current").symlink_to(f"releases/{first}")
    _commit(origin, "advance")
    calls["systemctl_rc"][("restart",)] = 1

    report = self_deploy_once(
        str(clone),
        _cfg(
            tmp_path,
            services=("voyn-aicc-worker.service",),
            release_root=str(release_root),
            release_venv=str(tmp_path / "missing-venv"),
        ),
    )

    assert report.outcome == "failed"
    assert "rollback_incomplete_services" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first
    assert (release_root / "current").readlink() == Path(f"releases/{first}")


def test_committed_control_unit_deploys_an_immutable_release():
    root = Path(__file__).parents[2]
    service = (root / "deploy/systemd/voyn-aicc-self-deploy.service").read_text()

    assert "WorkingDirectory=/opt/aicc/current" in service
    assert "--release-root /opt/aicc" in service
    assert "--release-venv ${AICC_RUNTIME_VENV}" in service
    assert "--repo-path ${AICC_SOURCE_REPO}" in service
    assert "exec /opt/aicc/current/.venv/bin/python" in service


def test_noop_looking_migration_still_records_unrolled_back_write(
    pair, calls, tmp_path, monkeypatch
):
    """Review of 5eb6f62 (`migrations_applied` on any zero exit, even a
    no-op) and review of 15774a77 (a stdout-parsed `schema_mutated` flag
    that missed `db upgrade` unconditionally re-asserting table grants):
    the step recorded for a successful `db upgrade` must neither claim a
    confirmed mutation nor claim a confirmed no-op -- it must say a database
    write happened that will not be undone, regardless of what the command's
    stdout says and regardless of whether a later restart fails."""
    origin, clone, first = pair
    _commit(origin, "advance, no pending schema change")
    monkeypatch.setattr(
        self_deploy, "_run_migrations",
        lambda repo_path, timeout: subprocess.CompletedProcess(
            [], 0, "already up to date\nre-asserted 0 table grants\n", ""
        ),
    )
    fails = {"left": 1}

    def one_shot_systemctl(args, timeout):
        calls["systemctl"].append(args)
        if args[0] == "restart" and fails["left"] > 0:
            fails["left"] -= 1
            return subprocess.CompletedProcess(args, 1, "", "boom")
        out = "active" if args[0] == "is-active" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(self_deploy, "_systemctl", one_shot_systemctl)
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",), migrate=True)
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "rolled_back"
    assert "restart_failed" in report.detail
    # The checkout and services were restored, but the database write from
    # `db upgrade` is not -- and the report must still say so, not omit it
    # just because the command's own output looked like a no-op.
    assert "database_upgrade_ran_not_rolled_back" in report.steps
    assert _git(clone, "rev-parse", "HEAD") == first


def test_diverged_and_dirty_checkouts_refuse(pair, calls, tmp_path):
    origin, clone, _first = pair
    _commit(origin, "advance")
    _commit(clone, "local divergence", filename="local.txt")
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert (report.outcome, report.detail) == (
        "refused", "non_fast_forward_checkout_diverged"
    )

    _git(clone, "reset", "-q", "--hard", "origin/main")
    _commit(origin, "advance again")
    (clone / "tracked.txt").write_text("uncommitted edit")
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert (report.outcome, report.detail) == ("refused", "checkout_dirty")
    assert calls["systemctl"] == []


def test_dependency_manifest_changes_refuse(pair, calls, tmp_path):
    origin, clone, first = pair
    _commit(origin, "bump deps", filename="uv.lock", content="lock v2")
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert report.outcome == "refused"
    assert "dependency_change_requires_manual_deploy" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first  # checkout untouched


def test_failed_smoke_rolls_back_before_anything_else_ran(pair, calls, tmp_path):
    """Smoke runs FIRST (review of cff672a): a broken tree is discovered
    while the database is untouched and no service was restarted -- the
    rollback has nothing to unwind but the checkout itself."""
    origin, clone, first = pair
    _commit(origin, "broken advance")
    calls["smoke_rc"] = 1
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",), migrate=True)
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "rolled_back"
    assert "import_smoke_failed" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first
    assert calls["migrate"] == 0  # smoke precedes migrations
    assert calls["systemctl"] == []  # and precedes any restart
    rows = [
        json.loads(line)
        for line in (tmp_path / "provenance.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome"] == "rolled_back"


def test_failed_restart_rolls_back(pair, calls, tmp_path, monkeypatch):
    """First restart (onto the new code) fails; the restore restart succeeds
    -- a VERIFIED rollback reports rolled_back."""
    origin, clone, first = pair
    _commit(origin, "advance")
    fails = {"left": 1}

    def one_shot_systemctl(args, timeout):
        calls["systemctl"].append(args)
        if args[0] == "restart" and fails["left"] > 0:
            fails["left"] -= 1
            return subprocess.CompletedProcess(args, 1, "", "boom")
        out = "active" if args[0] == "is-active" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(self_deploy, "_systemctl", one_shot_systemctl)
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",))
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "rolled_back"
    assert "restart_failed" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first


def test_failed_migrations_roll_back_before_services_restart(
    pair, calls, tmp_path, monkeypatch
):
    origin, clone, first = pair
    _commit(origin, "advance with migration")
    monkeypatch.setattr(
        self_deploy, "_run_migrations",
        lambda repo_path, timeout: subprocess.CompletedProcess([], 1, "", "DDL boom"),
    )
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",), migrate=True)
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "rolled_back"
    assert "migrations_failed" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first
    # Services were never restarted onto the failed deploy.
    assert ["restart", "voyn-aicc-worker.service"] not in calls["systemctl"]


def test_a_timeout_after_reset_still_rolls_back(pair, calls, tmp_path, monkeypatch):
    """Review of f794b3e: subprocess timeouts must become ordinary failures,
    not raises -- a raise after `reset --hard` would bypass rollback and
    leave the host half-deployed. `_run_bounded` converts TimeoutExpired to
    rc 124, and the ordinary rollback path handles that result."""
    origin, clone, first = pair
    _commit(origin, "advance")

    # The smoke seam yields exactly what _run_bounded yields on a timeout.
    monkeypatch.setattr(
        self_deploy, "_import_smoke",
        lambda repo_path, timeout: subprocess.CompletedProcess(
            ["python"], 124, "", "timed out after 1s"
        ),
    )
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert report.outcome == "rolled_back"
    assert "import_smoke_failed" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first

    # And the wrapper itself, against a genuinely overrunning command:
    bounded = self_deploy._run_bounded(["sleep", "5"], 1)
    assert bounded.returncode == 124
    assert "timed out" in bounded.stderr


def test_branch_is_configurable(pair, calls, tmp_path):
    """--branch plumbs through: a repository whose default branch is not
    `main` deploys from its own branch (review of f794b3e)."""
    origin, clone, _first = pair
    _git(origin, "checkout", "-q", "-b", "trunk")
    new = _commit(origin, "trunk advance")
    report = self_deploy_once(
        str(clone), _cfg(tmp_path, branch="trunk")
    )
    assert (report.outcome, report.detail) == ("deployed", new)
    assert _git(clone, "rev-parse", "HEAD") == new


def test_a_failed_rollback_is_reported_failed_not_rolled_back(
    pair, calls, tmp_path, monkeypatch
):
    """Review of 8d1f967 (High): the rollback's own `reset --hard` result
    was ignored -- a host left on NEW code must never carry provenance
    claiming a successful rollback. A rollback that cannot restore the
    checkout is `failed`/`rollback_incomplete`, an operator incident."""
    origin, clone, first = pair
    _commit(origin, "advance")
    calls["smoke_rc"] = 1

    real_git = self_deploy._git

    def sabotaged_git(repo_path, args, timeout):
        if args[:2] == ["reset", "--hard"] and args[2] == first:
            return subprocess.CompletedProcess(args, 1, "", "disk full")
        return real_git(repo_path, args, timeout)

    monkeypatch.setattr(self_deploy, "_git", sabotaged_git)
    report = self_deploy_once(str(clone), _cfg(tmp_path))
    assert report.outcome == "failed"
    assert report.detail.startswith("rollback_incomplete_checkout")
    rows = [
        json.loads(line)
        for line in (tmp_path / "provenance.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome"] == "failed"


def test_a_failed_service_restore_is_reported_failed(pair, calls, tmp_path):
    """The restore restart after a restart failure is verified too: dead
    services on the old code are `failed`, not `rolled_back`."""
    origin, clone, first = pair
    _commit(origin, "advance")
    calls["systemctl_rc"][("restart",)] = 1  # every restart fails
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",))
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "failed"
    assert "rollback_incomplete_services" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first  # checkout DID restore


def test_migration_failure_provenance_names_the_partial_database(
    pair, calls, tmp_path, monkeypatch
):
    """Review of 8d1f967 (Medium): earlier pending migrations may have
    committed before the failing one -- provenance must say so instead of
    implying a pristine database."""
    origin, clone, first = pair
    _commit(origin, "advance with migration")
    monkeypatch.setattr(
        self_deploy, "_run_migrations",
        lambda repo_path, timeout: subprocess.CompletedProcess([], 1, "", "DDL boom"),
    )
    report = self_deploy_once(str(clone), _cfg(tmp_path, migrate=True))
    assert report.outcome == "rolled_back"
    assert "database_may_hold_partial_migrations" in report.detail
    assert _git(clone, "rev-parse", "HEAD") == first


def test_failed_dispatch_smoke_after_migration_rolls_back_before_restart(pair, calls, tmp_path):
    """0019 on control-01 (2026-09-08): the migration applied, import-smoke was
    green, services restarted, and every planner tick died on a view whose
    owner the migration had changed. The dispatch smoke runs with exactly
    dispatch's privileges AFTER the migration and BEFORE any restart; a
    refusal rolls the checkout back with no service touched."""
    origin, clone, first = pair
    _commit(origin, "advance with a migration that breaks dispatch")
    calls["dispatch_smoke_rc"] = 1
    cfg = _cfg(tmp_path, services=("voyn-aicc-worker.service",), migrate=True)
    report = self_deploy_once(str(clone), cfg)
    assert report.outcome == "rolled_back"
    assert "dispatch_smoke_failed_after_migration" in report.detail
    assert calls["migrate"] == 1
    assert calls["dispatch_smoke"] == 1
    # The safety guarantee is an ORDER: migration, then the dispatch smoke,
    # then rollback -- and no service start/restart at all (review of
    # fc167cf7: counts alone passed with the smoke before the migration or
    # after a restart).
    assert calls["order"].index("migrate") < calls["order"].index("dispatch_smoke")
    assert not any(
        event.startswith("systemctl:") and event.split(":", 1)[1] in {"restart", "start", "reload"}
        for event in calls["order"]
    ), calls["order"]
    assert _git(clone, "rev-parse", "HEAD") == first
    assert "database_upgrade_ran_not_rolled_back" in report.steps
    assert "dispatch_smoke_passed" not in report.steps
