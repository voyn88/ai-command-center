"""Root-owned control release deploy: policy, staging and rollback on a REAL
git mirror; the installer's publication primitives are replaced by a small
fake that keeps the same contract (stage -> publish -> select -> rollback)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location(
    "aicc_control_release_deploy", ROOT / "ops/aicc_control_release_deploy.py"
)
assert _spec and _spec.loader
deploy = importlib.util.module_from_spec(_spec)
sys.modules["aicc_control_release_deploy"] = deploy
_spec.loader.exec_module(deploy)

UID = os.geteuid()
GID = os.getegid()


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo, message, filename="tracked.txt", content=None) -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else message)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class FakeTx:
    """The slice of aicc_install_transaction the deploy uses."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.events: list[str] = []
        self.fail_select = False
        outer = self

        class FileTransaction:
            def __init__(self, root, state_dir):
                self.pending_release = state_dir / "pending-release"

            def select_release(self, sha, repo):
                if outer.fail_select:
                    raise RuntimeError("select boom")
                link = outer.cfg.aicc_root / "current"
                previous = os.readlink(link)
                self.pending_release.write_text(previous)
                tmp = link.with_name(".sel")
                tmp.symlink_to(f"releases/{sha}")
                os.replace(tmp, link)
                outer.events.append(f"select:{sha}")

            def _restore_release_selector(self, clear_pending=False):
                link = outer.cfg.aicc_root / "current"
                previous = self.pending_release.read_text()
                tmp = link.with_name(".sel")
                tmp.symlink_to(previous)
                os.replace(tmp, link)
                if clear_pending:
                    self.pending_release.unlink()
                outer.events.append("restore")

        self.FileTransaction = FileTransaction

    def reconcile_release_publication(self, *a, **k):
        self.events.append("reconcile")

    def verify_release_manifest(self, *a, **k):
        self.events.append("verify")

    def record_release_manifest(self, staging, manifest, sha, **k):
        manifest.write_text("m")
        self.events.append("record")

    def publish_release_tree(self, staging, release_root, manifest, sha, **k):
        target = release_root / sha
        os.rename(staging, target)
        self.events.append(f"publish:{sha}")
        return target


@pytest.fixture
def host(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(origin))
    first = _commit(origin, "base")
    aicc = tmp_path / "opt-aicc"
    releases = aicc / "releases"
    releases.mkdir(parents=True)
    # the "current" release: a real, tight venv inside it
    current_dir = releases / first
    (current_dir / ".venv" / "bin").mkdir(parents=True)
    (current_dir / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (current_dir / ".venv").chmod(0o755)
    (aicc / "current").symlink_to(f"releases/{first}")
    state = tmp_path / "state"
    (state / "releases").mkdir(parents=True)
    mirror = state / "mirror.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(origin), str(mirror)],
        capture_output=True, text=True, check=True,
    )
    cfg = deploy.ControlConfig(
        mirror=mirror,
        aicc_root=aicc,
        release_root=releases,
        state_dir=state,
        lock_path=tmp_path / "lock",
        rollout_lock=tmp_path / "rollout.lock",
        remote_url="https://example.invalid/x.git",
        trusted_uid=UID,
        trusted_gid=GID,
    )
    return origin, mirror, cfg, first


def _advance(origin, mirror, message="advance", **kw) -> str:
    sha = _commit(origin, message, **kw)
    subprocess.run(
        ["git", "--git-dir", str(mirror), "fetch", "-q", str(origin), "main:main"],
        capture_output=True, text=True, check=True,
    )
    return sha


def _run(cfg, tx, **kw):
    return deploy.deploy_once(
        cfg,
        install_tx=tx,
        ensure=lambda c: None,
        health=kw.pop("health", lambda c, s: (True, "")),
        **kw,
    )


def test_a_plain_change_is_published_selected_and_health_checked(host):
    origin, mirror, cfg, first = host
    new = _advance(origin, mirror, "advance")
    tx = FakeTx(cfg)
    report = _run(cfg, tx)
    assert (report.outcome, report.detail) == ("DEPLOYED", "selected")
    assert (cfg.aicc_root / "current").readlink() == Path(f"releases/{new}")
    release = cfg.release_root / new
    assert (release / "tracked.txt").read_text() == "advance"
    assert (release / ".venv" / "bin" / "python").exists()  # copy of the current venv
    assert stat_mode(release) == 0o555
    assert tx.events.index(f"publish:{new}") < tx.events.index(f"select:{new}")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_noop_when_current_is_the_target(host):
    origin, mirror, cfg, first = host
    report = _run(cfg, FakeTx(cfg))
    assert (report.outcome, report.detail) == ("NOOP", "already_at_target")


@pytest.mark.parametrize(
    ("filename", "marker"),
    [
        ("ops/aicc_release_deploy.py", "deploy_mechanism_changed"),
        ("ops/aicc_control_release_deploy.py", "deploy_mechanism_changed"),
        ("ops/install-ops-autonomy.sh", "deploy_mechanism_changed"),
        ("deploy/systemd/voyn-aicc-x.service", "deploy_mechanism_changed"),
        ("deploy/sudoers.d/x", "deploy_mechanism_changed"),
        ("command_center/ops/disk_guard.py", "deploy_mechanism_changed"),
        ("uv.lock", "dependencies_changed"),
        ("deploy/agent-toolchain/package-lock.json", "deploy_mechanism_changed"),
        ("command_center/db/sql/0099_x.up.sql", "migrations_changed"),
    ],
)
def test_changes_that_need_a_human_stop_before_anything_is_written(host, filename, marker):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "touch", filename=filename, content="x")
    tx = FakeTx(cfg)
    report = _run(cfg, tx)
    assert report.outcome == "MANUAL" and marker in report.detail
    assert tx.events == []  # not even a reconcile
    assert (cfg.aicc_root / "current").readlink() == Path(f"releases/{first}")


def test_a_target_that_does_not_descend_from_current_is_refused(host):
    origin, mirror, cfg, first = host
    _git(origin, "checkout", "-q", "-b", "side")
    side = _commit(origin, "side", filename="side.txt")
    _git(origin, "checkout", "-q", "main")
    _advance(origin, mirror, "main moves on")
    subprocess.run(
        ["git", "--git-dir", str(mirror), "fetch", "-q", str(origin), "+side:side"],
        capture_output=True, text=True, check=True,
    )
    (cfg.release_root / side / ".venv").mkdir(parents=True)
    (cfg.aicc_root / ".c").symlink_to(f"releases/{side}")
    os.replace(cfg.aicc_root / ".c", cfg.aicc_root / "current")
    tx = FakeTx(cfg)
    report = _run(cfg, tx)
    assert report.outcome == "REFUSED"
    assert report.detail == "target_does_not_descend_from_current"
    assert tx.events == []


def test_a_failed_health_check_restores_the_previous_release(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    tx = FakeTx(cfg)
    report = _run(cfg, tx, health=lambda c, s: (False, "import boom"))
    assert report.outcome == "ROLLED_BACK" and "health_failed" in report.detail
    assert (cfg.aicc_root / "current").readlink() == Path(f"releases/{first}")
    assert "restore" in tx.events


def test_a_failed_select_leaves_current_alone(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    tx = FakeTx(cfg)
    tx.fail_select = True
    report = _run(cfg, tx)
    assert report.outcome == "REFUSED" and "select_failed" in report.detail
    assert (cfg.aicc_root / "current").readlink() == Path(f"releases/{first}")


def test_a_current_venv_that_is_a_link_stops_the_deploy(host, tmp_path):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    venv = cfg.release_root / first / ".venv"
    for item in sorted(venv.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    venv.rmdir()
    elsewhere = tmp_path / "somebody-elses-venv"
    elsewhere.mkdir()
    venv.symlink_to(elsewhere)
    report = _run(cfg, FakeTx(cfg))
    assert report.outcome == "REFUSED" and "publish_failed" in report.detail
    assert not list(cfg.release_root.glob(".stage-*"))  # staging cleaned up


def test_a_group_writable_current_venv_stops_the_deploy(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    (cfg.release_root / first / ".venv").chmod(0o775)
    report = _run(cfg, FakeTx(cfg))
    assert report.outcome == "REFUSED" and "group/other writable" in report.detail


def test_a_symlink_that_leaves_the_tree_or_a_setuid_file_is_refused(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "ok").write_text("x")
    (tree / "inside").symlink_to("ok")
    deploy._check_tree(tree)  # fine
    (tree / "out").symlink_to("/etc/passwd")
    with pytest.raises(RuntimeError, match="leaves the release tree"):
        deploy._check_tree(tree)
    (tree / "out").unlink()
    (tree / "suid").write_text("x")
    (tree / "suid").chmod(0o4755)
    with pytest.raises(RuntimeError, match="setuid"):
        deploy._check_tree(tree)


def test_nothing_is_taken_from_the_caller(host, monkeypatch, capsys):
    origin, mirror, cfg, first = host
    assert deploy.main(["--target", "abc"]) == 2
    assert "takes no arguments" in capsys.readouterr().err
    monkeypatch.setenv("AICC_RELEASE_REMOTE_URL", "https://evil.invalid/x.git")
    assert deploy.ControlConfig().remote_url == deploy.REMOTE_URL


def test_a_staged_rollout_or_a_second_run_refuses(host):
    origin, mirror, cfg, first = host
    cfg.rollout_lock.write_text("1")
    assert _run(cfg, FakeTx(cfg)).detail == "staged_rollout_in_progress"
    cfg.rollout_lock.unlink()
    import fcntl

    with open(cfg.lock_path, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = _run(cfg, FakeTx(cfg))
    assert report.detail == "another_deploy_running"


def test_the_mirror_fetch_is_hardened(host, monkeypatch):
    origin, mirror, cfg, first = host
    seen: list[list[str]] = []
    real = deploy._run

    def spy(argv, **kw):
        seen.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(deploy, "_run", spy)
    cfg2 = deploy.ControlConfig(**{**cfg.__dict__, "remote_url": "https://example.invalid/x.git"}) if False else cfg
    try:
        deploy.ensure_mirror(cfg2)
    except RuntimeError:
        pass  # the invalid host is unreachable; only the argv matters
    fetch = [a for a in seen if "fetch" in a]
    assert fetch
    joined = " ".join(fetch[0])
    for flag in ("core.hooksPath=/dev/null", "transfer.fsckObjects=true", "protocol.allow=never"):
        assert flag in joined


def test_the_unit_takes_no_environment_or_arguments_and_is_sandboxed():
    text = (ROOT / "deploy/systemd/voyn-aicc-control-release-deploy.service").read_text()
    lines = [x for x in text.splitlines() if x and not x.startswith("#")]
    assert not any(x.startswith(("EnvironmentFile", "Environment=")) for x in lines)
    exec_start = [x for x in lines if x.startswith("ExecStart=")]
    assert exec_start == [
        "ExecStart=/usr/bin/python3 -I -B "
        "/opt/aicc/current/ops/aicc_control_release_deploy.py"
    ]
    for needed in (
        "User=root",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "RuntimeMaxSec=1200",
        "MemoryDenyWriteExecute=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    ):
        assert needed in lines, needed
    caps = [x for x in lines if x.startswith("CapabilityBoundingSet=")][0]
    assert "CAP_SYS_ADMIN" not in caps and "CAP_NET_ADMIN" not in caps
    timer = (ROOT / "deploy/systemd/voyn-aicc-control-release-deploy.timer").read_text()
    assert "Not enabled by any installer" in timer


@pytest.mark.parametrize(
    "path",
    [
        "json.py",
        "re.py",
        "typing.py",
        "sitecustomize.py",
        "usercustomize.py",
        "evil.pth",
        "json/__init__.py",
        "app.py",
        "clients/__init__.py",
        "command_center/__init__.py",
        "command_center/ops/__init__.py",
        "command_center/ops/anything_new.py",
        "command_center/ops/infra_monitor.py",
        "command_center/deployment/self_deploy.py",
        "ops/anything.py",
        "deploy/anything",
        "deploy/systemd/voyn-infra-monitor.service",
        "deploy/systemd/voyn-queue-monitor.service",
        ".github/workflows/ci.yml",
    ],
)
def test_anything_that_can_run_as_root_is_manual(path):
    assert deploy.classify_change([path]) is not None


@pytest.mark.parametrize(
    "path",
    [
        "requirements-ci-linux.lock",
        "constraints-ci.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        ".python-version",
        "deploy/agent-toolchain/package.json",
    ],
)
def test_every_dependency_pinning_file_is_manual(path):
    assert deploy.classify_change([path]) is not None


def test_ordinary_application_files_are_deployed_unattended():
    for path in (
        "command_center/orchestrator/review_merge.py",
        "command_center/worker/handlers.py",
        "tests/x.py",
        "docs/x.md",
    ):
        assert deploy.classify_change([path]) is None, path


def test_every_root_launch_path_in_the_shipped_units_is_covered_by_the_policy():
    """Whatever a root unit starts from the release must be MANUAL when it
    changes: derived from the shipped units, so a new root unit cannot slip by."""
    import re as _re

    checked = 0
    for unit in sorted((ROOT / "deploy/systemd").glob("*.service")):
        text = unit.read_text()
        if "User=root" not in text and "\nUser=" in text:
            continue  # a non-root unit
        for match in _re.finditer(
            r"ExecStart=\S*python\S*(?:\s+-\S+)*\s+-m\s+(command_center[\w.]*)", text
        ):
            module = match.group(1)
            parts = module.split(".")
            for depth in range(1, len(parts) + 1):
                rel = "/".join(parts[:depth])
                candidates = [f"{rel}.py", f"{rel}/__init__.py"]
                for candidate in candidates:
                    if (ROOT / candidate).exists():
                        assert deploy.classify_change([candidate]) is not None, (
                            unit.name,
                            candidate,
                        )
                        checked += 1
        for match in _re.finditer(r"/opt/aicc/current/([\w./-]+\.py)", text):
            assert deploy.classify_change([match.group(1)]) is not None, (
                unit.name,
                match.group(1),
            )
            checked += 1
    assert checked >= 6


def test_a_copied_venv_that_points_at_another_release_is_refused(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    (cfg.release_root / first / ".venv").chmod(0o755)
    pth = cfg.release_root / first / ".venv" / "lib" / "site-packages"
    pth.mkdir(parents=True)
    (pth / "x.pth").write_text(f"{cfg.release_root}/{first}\n")
    report = _run(cfg, FakeTx(cfg))
    assert report.outcome == "REFUSED" and "points at another release" in report.detail
    assert not list(cfg.release_root.glob(".stage-*"))


def test_the_health_check_requires_the_new_release_to_run_from_itself(host, monkeypatch):
    origin, mirror, cfg, first = host
    release = cfg.release_root / first
    base = str(release.resolve())

    def fake(outputs):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, outputs, "")

        return run

    monkeypatch.setattr(deploy, "_run", fake(f"{base}/command_center/__init__.py\n{base}/.venv\n"))
    assert deploy.health_check(cfg, first) == (True, "")
    monkeypatch.setattr(deploy, "_run", fake(f"/elsewhere/command_center/__init__.py\n{base}/.venv\n"))
    ok, why = deploy.health_check(cfg, first)
    assert not ok and "does not run from itself" in why
    monkeypatch.setattr(deploy, "_run", fake(f"{base}/command_center/__init__.py\n/usr\n"))
    assert deploy.health_check(cfg, first)[0] is False


def test_a_copied_venv_pointing_at_an_editable_install_of_another_release_is_refused(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    site = cfg.release_root / first / ".venv" / "lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "__editable__.command_center.py").write_text(
        f"MAPPING = {{'command_center': '{cfg.release_root}/{first}/command_center'}}\n"
    )
    report = _run(cfg, FakeTx(cfg))
    assert report.outcome == "REFUSED" and "points at another release" in report.detail


def test_the_creation_command_line_in_pyvenv_cfg_is_not_a_pointer(host):
    origin, mirror, cfg, first = host
    new = _advance(origin, mirror, "advance")
    venv = cfg.release_root / first / ".venv"
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\n"
        f"command = /usr/bin/python3 -m venv {cfg.release_root}/{first}/.venv\n"
    )
    report = _run(cfg, FakeTx(cfg))
    assert (report.outcome, report.detail) == ("DEPLOYED", "selected")
    assert (cfg.release_root / new).is_dir()


def test_a_pyvenv_home_inside_a_release_is_refused(host):
    origin, mirror, cfg, first = host
    _advance(origin, mirror, "advance")
    venv = cfg.release_root / first / ".venv"
    (venv / "pyvenv.cfg").write_text(f"home = {cfg.release_root}/{first}/.venv/bin\n")
    report = _run(cfg, FakeTx(cfg))
    assert report.outcome == "REFUSED"


def test_without_stdlib_module_names_top_level_python_files_fail_closed(monkeypatch):
    monkeypatch.delattr(deploy.sys, "stdlib_module_names", raising=False)
    assert deploy.classify_change(["something.py"]) is not None
    assert deploy.classify_change(["docs/x.md"]) is None
