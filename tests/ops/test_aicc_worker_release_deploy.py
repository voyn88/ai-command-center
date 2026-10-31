"""Worker release deploy: merged main -> immutable root-owned /opt/aicc/current.

Symlink/`fcntl`/root-owned publication primitives are Linux-only (same as
`test_aicc_release_manifest` / `test_aicc_install_transaction`). These tests
drive `deploy_once` through injectable seams so the orchestration contract
runs on Windows too.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("host_shaped_fixture_roots")


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_release_deploy.py"
    spec = importlib.util.spec_from_file_location("aicc_worker_release_deploy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Avoid pulling aicc_install_transaction (fcntl) unless a test asks for it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SHA_A = "a" * 40
SHA_B = "b" * 40
LANE_1 = "voyn-aicc-worker@1.service"
LANE_2 = "voyn-aicc-worker@2.service"
LANE_3 = "voyn-aicc-worker@3.service"


@pytest.mark.skipif(sys.platform == "win32", reason="installed helpers require fcntl")
@pytest.mark.parametrize("installed", [False, True])
def test_helper_loaders_source_and_installed_layout(tmp_path, monkeypatch, installed):
    module = _module()
    source_ops = Path(module.__file__).parent
    for name in ("aicc_install_transaction", "aicc_staged_worker_rollout"):
        filename = name.replace("_", "-") if installed else name + ".py"
        (tmp_path / filename).write_bytes((source_ops / (name + ".py")).read_bytes())
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(module, "_OPS_DIR", tmp_path)
    try:
        # Real deploy acquires the rollout lock before asking for the transaction.
        rollout = module._load_staged_rollout()
        tx = module._load_install_transaction()
        assert callable(tx.verify_release_manifest)
        assert Path(tx.__file__).parent == tmp_path
        assert Path(rollout.__file__).parent == tmp_path
        assert module._load_install_transaction() is tx
        assert module._load_staged_rollout() is rollout
    finally:
        for name in ("aicc_install_transaction", "aicc_staged_worker_rollout"):
            sys.modules.pop(name, None)


def _cfg(module, tmp_path, **overrides):
    kwargs = {
        "bare_mirror": tmp_path / "mirror.git",
        "aicc_root": tmp_path / "opt" / "aicc",
        "release_root": tmp_path / "opt" / "aicc" / "releases",
        "state_dir": tmp_path / "state",
        "runtime_venv": tmp_path / "runtime-venv",
        "libexec_dir": tmp_path / "usr" / "libexec",
        "lane_registry": tmp_path / "lanes.conf",
        "rollout_lock_path": tmp_path / "aicc-staged-rollout.lock",
        "readiness_timeout": 1.0,
        "poll_interval": 0.01,
        "trusted_uid": 0,
        "trusted_gid": 0,
    }
    kwargs.update(overrides)
    cfg = module.DeployConfig(**kwargs)
    cfg.aicc_root.mkdir(parents=True, exist_ok=True)
    cfg.release_root.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.libexec_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _seed_helpers(cfg, release_sha: str, *, release_bodies: dict[str, bytes], installed: dict[str, bytes]):
    """Place release-tree sources and currently-installed libexec helpers."""
    release_ops = cfg.release_root / release_sha / "ops"
    release_ops.mkdir(parents=True, exist_ok=True)
    for relative, body in release_bodies.items():
        path = cfg.release_root / release_sha / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    for name, body in installed.items():
        target = cfg.libexec_dir / name
        target.write_bytes(body)
        target.chmod(0o644)


def _deploy(module, cfg, *, previous: str | None = SHA_A, **kwargs):
    (cfg.release_root / (previous or SHA_A)).mkdir(exist_ok=True)
    defaults = {
        "install_tx": object(),  # never load fcntl-bound installer on Windows
        "ensure_mirror": lambda _cfg: None,
        "resolve_sha": lambda _cfg: SHA_B if previous != SHA_B else SHA_A,
        "current_sha": lambda _cfg: previous,
        "dependency_gate": lambda *_a, **_k: None,
        "write_lock": lambda: None,
        "remove_lock": lambda: None,
        "hold_timers": lambda: None,
        "release_timers": lambda: None,
        "clear_pending": lambda: None,
    }
    defaults.update(kwargs)
    return module.deploy_once(cfg, **defaults)


def test_noop_when_current_already_at_target(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    (cfg.release_root / SHA_A).mkdir()

    report = _deploy(
        module,
        cfg,
        previous=SHA_A,
        resolve_sha=lambda _cfg: SHA_A,
        stage=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no stage")),
        select=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no select")),
        rollout=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no rollout")),
    )

    assert report.outcome == "NOOP"
    assert report.line() == f"NOOP {SHA_A} already_at_target"


def test_deploy_calls_publish_verify_select_rollout_in_order(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    order: list[str] = []

    def stage(_cfg, sha):
        order.append(f"publish:{sha}")
        (cfg.release_root / sha).mkdir(exist_ok=True)

    def select(sha):
        order.append(f"select:{sha}")
        return f"releases/{sha}"

    def rollout(units):
        order.append(f"rollout:{','.join(units)}")
        return True, "ready:1"

    report = _deploy(
        module,
        cfg,
        stage=stage,
        select=select,
        load_lanes=lambda *_a, **_k: (LANE_1,),
        show=lambda unit, prop: {
            "ActiveState": "active",
            "SubState": "running",
            "StatusText": "aicc-ready",
            "MainPID": "42",
        }.get(prop, ""),
        rollout=rollout,
        clear_pending=lambda: order.append("clear_pending"),
        write_lock=lambda: order.append("lock"),
        remove_lock=lambda: order.append("unlock"),
        hold_timers=lambda: order.append("hold"),
        release_timers=lambda: order.append("release"),
    )

    assert report.outcome == "DEPLOYED"
    assert report.steps[:4] == ["publish", "verify", "select", "rollout"]
    assert order == [
        "lock",
        f"publish:{SHA_B}",
        "hold",
        f"select:{SHA_B}",
        "rollout:voyn-aicc-worker@1.service",
        "release",
        "clear_pending",
        "unlock",
    ]


def test_stopped_registered_lane_is_untouched(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    touched: list[str] = []

    def show(unit: str, prop: str) -> str:
        if prop == "ActiveState":
            return "inactive" if unit == LANE_3 else "active"
        return {
            "SubState": "running",
            "StatusText": "aicc-ready",
            "MainPID": "7",
        }.get(prop, "")

    def rollout(units):
        touched.extend(units)
        return True, f"ready:{len(units)}"

    report = _deploy(
        module,
        cfg,
        stage=lambda _c, sha: (cfg.release_root / sha).mkdir(exist_ok=True),
        select=lambda sha: f"releases/{sha}",
        load_lanes=lambda *_a, **_k: (LANE_1, LANE_2, LANE_3),
        show=show,
        rollout=rollout,
    )

    assert report.outcome == "DEPLOYED"
    assert touched == [LANE_1, LANE_2]
    assert f"untouched:{LANE_3}" in report.steps


def test_rollout_lock_present_refuses(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    cfg.rollout_lock_path.write_text("1\n", encoding="utf-8")

    report = module.deploy_once(
        cfg,
        install_tx=object(),
        ensure_mirror=lambda _cfg: (_ for _ in ()).throw(AssertionError("no fetch")),
    )

    assert report.outcome == "REFUSED"
    assert report.detail == "staged_rollout_in_progress"
    assert report.line().startswith("REFUSED")


def test_no_lane_ready_rolls_back_to_previous_release(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    rolled_back = {"done": False}

    report = _deploy(
        module,
        cfg,
        stage=lambda _c, sha: (cfg.release_root / sha).mkdir(exist_ok=True),
        select=lambda sha: f"releases/{sha}",
        load_lanes=lambda *_a, **_k: (LANE_1,),
        show=lambda unit, prop: "active" if prop == "ActiveState" else "",
        rollout=lambda units: (False, "not_ready:voyn-aicc-worker@1.service"),
        restore_previous=lambda: rolled_back.__setitem__("done", True),
    )

    assert report.outcome == "ROLLED_BACK"
    assert "not_ready" in report.detail
    assert rolled_back["done"] is True
    assert "rollback" in report.steps


def test_active_registered_lanes_skips_inactive():
    module = _module()
    states = {LANE_1: "active", LANE_2: "inactive", LANE_3: "activating"}

    def show(unit, prop):
        assert prop == "ActiveState"
        return states[unit]

    assert module.active_registered_lanes(
        (LANE_1, LANE_2, LANE_3), show=show
    ) == (LANE_1, LANE_3)


def test_committed_unit_files_are_root_hardened():
    root = Path(__file__).parents[2]
    service = (
        root / "deploy/systemd/voyn-aicc-worker-release-deploy.service"
    ).read_text(encoding="utf-8")
    assert "User=root" in service
    assert "ProtectSystem=strict" in service
    assert "/usr/libexec/aicc-worker-release-deploy" in service
    assert "ReadWritePaths=/opt/aicc/releases" in service
    assert "/usr/libexec" in service
    assert "/run/aicc-staged-rollout.lock" in service


def test_grok_driver_is_refreshed_from_selected_release(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    body = b"#!/usr/bin/python3\n# bounded Grok driver\n"
    _seed_helpers(cfg, SHA_A, release_bodies={"ops/aicc_grok_agent.py": body},
                  installed={"aicc-grok-agent": b"old-driver"})
    report = _deploy(module, cfg, resolve_sha=lambda _: SHA_A)
    assert report.outcome == "NOOP"
    target = cfg.libexec_dir / "aicc-grok-agent"
    assert target.read_bytes() == body
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_changed_helper_is_replaced_with_mode_0755(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    new_launcher = b"#!/usr/bin/env python3\n# fixed launcher\n"
    _seed_helpers(
        cfg,
        SHA_B,
        release_bodies={
            "ops/aicc_agent_launcher.py": new_launcher,
            "ops/aicc_staged_worker_rollout.py": b"rollout-same\n",
            "ops/aicc_release_deploy.py": b"deploy-same\n",
        },
        installed={
            "aicc-agent-launcher": b"#!/usr/bin/env python3\n# stale launcher\n",
            "aicc-staged-worker-rollout": b"rollout-same\n",
            "aicc-worker-release-deploy": b"deploy-same\n",
        },
    )

    report = _deploy(
        module,
        cfg,
        stage=lambda _c, sha: (cfg.release_root / sha).mkdir(exist_ok=True),
        select=lambda sha: f"releases/{sha}",
        load_lanes=lambda *_a, **_k: (LANE_1,),
        show=lambda unit, prop: {
            "ActiveState": "active",
            "SubState": "running",
            "StatusText": "aicc-ready",
            "MainPID": "9",
        }.get(prop, ""),
        rollout=lambda units: (True, "ready:1"),
    )

    installed = cfg.libexec_dir / "aicc-agent-launcher"
    assert report.outcome == "DEPLOYED"
    assert "helpers:aicc-agent-launcher" in report.detail
    assert "launcher_refreshed_next_connection" in report.steps
    assert installed.read_bytes() == new_launcher
    # Unix mode is the production contract; Windows does not store 0755.
    if sys.platform != "win32":
        assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    # Unchanged siblings left alone.
    assert (cfg.libexec_dir / "aicc-staged-worker-rollout").read_bytes() == b"rollout-same\n"
    assert (cfg.libexec_dir / "aicc-worker-release-deploy").read_bytes() == b"deploy-same\n"


def test_unchanged_helpers_are_left_alone(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    bodies = {
        "ops/aicc_agent_launcher.py": b"launcher-v1\n",
        "ops/aicc_staged_worker_rollout.py": b"rollout-v1\n",
        "ops/aicc_release_deploy.py": b"deploy-v1\n",
    }
    installed = {
        "aicc-agent-launcher": b"launcher-v1\n",
        "aicc-staged-worker-rollout": b"rollout-v1\n",
        "aicc-worker-release-deploy": b"deploy-v1\n",
    }
    _seed_helpers(cfg, SHA_B, release_bodies=bodies, installed=installed)
    before = {
        name: (cfg.libexec_dir / name).stat().st_mtime_ns for name in installed
    }

    report = _deploy(
        module,
        cfg,
        stage=lambda _c, sha: (cfg.release_root / sha).mkdir(exist_ok=True),
        select=lambda sha: f"releases/{sha}",
        load_lanes=lambda *_a, **_k: (LANE_1,),
        show=lambda unit, prop: {
            "ActiveState": "active",
            "SubState": "running",
            "StatusText": "aicc-ready",
            "MainPID": "9",
        }.get(prop, ""),
        rollout=lambda units: (True, "ready:1"),
    )

    assert report.outcome == "DEPLOYED"
    assert "helpers:" not in report.detail
    for name, body in installed.items():
        path = cfg.libexec_dir / name
        assert path.read_bytes() == body
        assert path.stat().st_mtime_ns == before[name]


def test_rollback_leaves_helpers_untouched(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    stale = b"stale-launcher-on-host\n"
    _seed_helpers(
        cfg,
        SHA_B,
        release_bodies={"ops/aicc_agent_launcher.py": b"new-launcher-from-release\n"},
        installed={"aicc-agent-launcher": stale},
    )

    report = _deploy(
        module,
        cfg,
        stage=lambda _c, sha: (cfg.release_root / sha).mkdir(exist_ok=True),
        select=lambda sha: f"releases/{sha}",
        load_lanes=lambda *_a, **_k: (LANE_1,),
        show=lambda unit, prop: "active" if prop == "ActiveState" else "",
        rollout=lambda units: (False, "not_ready:voyn-aicc-worker@1.service"),
        restore_previous=lambda: None,
    )

    assert report.outcome == "ROLLED_BACK"
    assert (cfg.libexec_dir / "aicc-agent-launcher").read_bytes() == stale
    assert "helpers:" not in report.detail
    assert "helpers:" not in "".join(report.steps)


def test_release_remote_never_comes_from_the_operator_checkout(monkeypatch):
    """The URL root deploys from is the unit's, not a writable checkout's."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "ops" / "aicc_release_deploy.py"
    spec = importlib.util.spec_from_file_location("_wrd_remote", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_wrd_remote"] = module
    spec.loader.exec_module(module)

    cfg = module.DeployConfig()
    assert module._discover_remote_url(cfg) == module.DEFAULT_REMOTE_URL
    custom = module.DeployConfig(remote_url="https://example.invalid/r.git")
    assert module._discover_remote_url(custom) == "https://example.invalid/r.git"
    bad = module.DeployConfig(remote_url="file:///home/voynadmin/evil")
    try:
        module._discover_remote_url(bad)
    except RuntimeError:
        pass
    else:
        raise AssertionError("non-https remote must be refused")


def test_noop_retries_failed_helper_refresh(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    _seed_helpers(cfg, SHA_A,
        release_bodies={"ops/aicc_agent_launcher.py": b"new"},
        installed={"aicc-agent-launcher": b"old"})
    target = cfg.libexec_dir / "aicc-agent-launcher"
    inode = target.stat().st_ino
    report = _deploy(module, cfg, resolve_sha=lambda _: SHA_A)
    assert report.outcome == "NOOP"
    assert target.read_bytes() == b"new"
    if sys.platform != "win32":
        assert target.stat().st_ino != inode
        assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_dependency_refusal_never_selects(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    def forbidden(*a, **k):
        raise AssertionError("must refuse before publication or selection")
    report = _deploy(module, cfg, dependency_gate=lambda *a: "dependency_changed",
        stage=forbidden, select=forbidden)
    assert report.outcome == "REFUSED"
    assert report.detail == "dependency_changed"


def test_contended_lock_does_not_flip_selector(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    def locked():
        raise FileExistsError("another rollout")
    def forbidden(*a, **k):
        raise AssertionError("selector and timers must remain untouched")
    report = _deploy(module, cfg,
        stage=lambda c, sha: (c.release_root / sha).mkdir(),
        load_lanes=lambda *a, **k: (), write_lock=locked,
        select=forbidden, release_timers=forbidden)
    assert report.outcome == "REFUSED"
    assert "select" not in report.steps


@pytest.mark.parametrize("noop", [False, True])
def test_lock_covers_mirror_publication_and_helper_refresh(tmp_path, monkeypatch, noop):
    module = _module()
    cfg = _cfg(module, tmp_path)
    held = []
    observed = []

    def check(phase):
        assert held == [True]
        observed.append(phase)

    def refresh(*args, **kwargs):
        check("refresh")
        return []

    monkeypatch.setattr(module, "refresh_root_helpers", refresh)
    report = _deploy(
        module, cfg, previous=SHA_A,
        resolve_sha=lambda c: SHA_A if noop else SHA_B,
        write_lock=lambda: held.append(True), remove_lock=lambda: held.clear(),
        ensure_mirror=lambda c: check("mirror"),
        stage=lambda *args: check("publish"),
        load_lanes=lambda *args, **kwargs: (), select=lambda sha: check("select"),
        rollout=lambda units: (True, "ready:0"),
    )
    assert report.outcome == ("NOOP" if noop else "DEPLOYED")
    assert observed == (["mirror", "refresh"] if noop else ["mirror", "publish", "select", "refresh"])
    assert held == []


def test_mirror_failure_releases_early_lock(tmp_path):
    module = _module()
    cfg = _cfg(module, tmp_path)
    held = []

    def fail(c):
        assert held == [True]
        raise OSError("mirror unavailable")

    report = _deploy(module, cfg, write_lock=lambda: held.append(True),
                     remove_lock=lambda: held.clear(), ensure_mirror=fail)
    assert report.outcome == "REFUSED"
    assert held == []


@pytest.mark.parametrize("override", [False, True])
def test_cli_passes_real_paths_to_deploy(tmp_path, monkeypatch, capsys, override):
    """The installed service invokes main with no path flags, not deploy_once directly."""
    module = _module()
    fields = {
        "bare-mirror": "bare_mirror", "aicc-root": "aicc_root",
        "release-root": "release_root", "state-dir": "state_dir",
        "runtime-venv": "runtime_venv", "lane-registry": "lane_registry",
        "rollout-lock": "rollout_lock_path",
    }
    argv = []
    if override:
        for flag in fields:
            argv += [f"--{flag}", str(tmp_path / flag)]
    seen = []
    def deploy(cfg):
        seen.append(cfg)
        for flag, field in fields.items():
            value = getattr(cfg, field)
            assert isinstance(value, Path), (field, value)
            expected = tmp_path / flag if override else getattr(module.DeployConfig(), field)
            assert value == expected
        # Exercise the operation that crashed the live service before lock acquisition.
        cfg.rollout_lock_path.exists()
        return module.DeployReport(outcome="NOOP", target_sha=SHA_A, detail="already_current")
    monkeypatch.setattr(module, "deploy_once", deploy)
    assert module.main(argv) == 0
    assert len(seen) == 1
    assert capsys.readouterr().out == f"NOOP {SHA_A} already_current\n"


def test_registry_uses_installed_release_runtime(tmp_path, monkeypatch):
    import subprocess
    module = _module()
    cfg = _cfg(module, tmp_path)
    monkeypatch.setattr(module, "_current_release_sha", lambda _: SHA_A)
    def run(args, **kwargs):
        assert args[0] == str(cfg.runtime_venv / "bin/python")
        assert args[-3:] == [str(cfg.lane_registry), str(cfg.trusted_uid), str(cfg.trusted_gid)]
        assert kwargs["cwd"] == str(cfg.release_root / SHA_A)
        assert kwargs["env"]["PYTHONPATH"] == str(cfg.release_root / SHA_A)
        assert kwargs["timeout"] == 60
        return subprocess.CompletedProcess(args, 0, '["voyn-aicc-worker@1.service"]', '')
    monkeypatch.setattr(module, "_run", run)
    assert module.load_installed_lane_registry(cfg) == (LANE_1,)


@pytest.mark.parametrize("payload", ['[]', '["sshd.service"]', '[null]',
                                      '["voyn-aicc-worker@1.service", "voyn-aicc-worker@1.service"]'])
def test_registry_refuses_invalid_runtime_output(tmp_path, monkeypatch, payload):
    import subprocess
    module = _module()
    monkeypatch.setattr(module, "_current_release_sha", lambda _: SHA_A)
    monkeypatch.setattr(module, "_run", lambda args, **kw: subprocess.CompletedProcess(args, 0, payload, ''))
    with pytest.raises(RuntimeError, match="invalid lane registry"):
        module.load_installed_lane_registry(_cfg(module, tmp_path))


def test_registry_refuses_absent_installed_release(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_current_release_sha", lambda _: None)
    monkeypatch.setattr(module, "_run", lambda *a, **kw: pytest.fail("must not execute candidate"))
    with pytest.raises(RuntimeError, match="no installed release"):
        module.load_installed_lane_registry(_cfg(module, tmp_path))



def test_registry_import_does_not_pollute_published_release(tmp_path, monkeypatch):
    import subprocess

    module = _module()
    cfg = _cfg(module, tmp_path)
    release = cfg.release_root / SHA_A
    package = release / "command_center" / "ops"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "credential_rotation.py").write_text(
        "def load_lane_registry(path, **kwargs):\n"
        "    return ['voyn-aicc-worker@1.service']\n"
    )
    before = sorted(str(p.relative_to(release)) for p in release.rglob("*"))
    monkeypatch.setattr(module, "_current_release_sha", lambda _: SHA_A)
    # Do not let the parent test environment mask the production failure.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)

    def run(args, **kwargs):
        return subprocess.run(
            [sys.executable, *args[1:]], capture_output=True, text=True, check=False,
            **kwargs,
        )

    monkeypatch.setattr(module, "_run", run)
    assert module.load_installed_lane_registry(cfg) == (LANE_1,)
    assert sorted(str(p.relative_to(release)) for p in release.rglob("*")) == before
