"""Per-host self-deploy (VOYN-W0-AICC-DEPLOY-AUTOMATION) on a REAL git
origin+clone pair; systemctl and migrations are faked at the module seam --
the git behavior (fast-forward, divergence, dirty tree, rollback) is the
substance under test and runs for real."""

from __future__ import annotations

import json
import subprocess

import pytest

from command_center.orchestrator import self_deploy
from command_center.orchestrator.self_deploy import (
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
    recorded = {"systemctl": [], "migrate": 0, "smoke_rc": 0, "systemctl_rc": {}}

    def fake_systemctl(args, timeout):
        recorded["systemctl"].append(args)
        rc = recorded["systemctl_rc"].get(tuple(args[:1]), 0)
        out = "active" if args[0] == "is-active" and rc == 0 else ""
        return subprocess.CompletedProcess(args, rc, out, "" if rc == 0 else "boom")

    def fake_migrations(repo_path, timeout):
        recorded["migrate"] += 1
        recorded["migrate_cwd"] = repo_path
        return subprocess.CompletedProcess([], 0, "", "")

    def fake_smoke(repo_path, timeout):
        return subprocess.CompletedProcess([], recorded["smoke_rc"], "", "import boom")

    monkeypatch.setattr(self_deploy, "_systemctl", fake_systemctl)
    monkeypatch.setattr(self_deploy, "_run_migrations", fake_migrations)
    monkeypatch.setattr(self_deploy, "_import_smoke", fake_smoke)
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
    assert report.steps.index("import_smoke_passed") < report.steps.index("migrations_applied")
    assert ["restart", "voyn-aicc-worker.service"] in calls["systemctl"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "provenance.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome"] == "deployed" and rows[-1]["target_sha"] == new


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
