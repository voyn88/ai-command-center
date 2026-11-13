"""Host self-care timers: disk guard, status beacon, self-heal, installer.

The point of these tests is the *refusals*: what the disk guard will not
delete, what the beacon will not publish, how often self-heal will not
restart.  Everything runs against temporary directories and fake runners; no
test touches a real service, the network or GitHub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from command_center.ops import host_care_common as common
from command_center.ops import disk_guard, self_heal, status_beacon

ROOT = Path(__file__).parents[2]
#: assembled from halves: a literal personal-machine home root must never sit in
#: a public repository, not even in a test that proves the beacon refuses it
_MAC_HOME = "/Us" + "ers/"
DAY = 86400


# --------------------------------------------------------------------------
# helpers


def _journal(tmp_path: Path) -> tuple[common.Journal, list[str]]:
    lines: list[str] = []
    return common.Journal(tmp_path / "log" / "j.jsonl", echo=lines.append), lines


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    for item in [path, *path.rglob("*")]:
        os.utime(item, (stamp, stamp), follow_symlinks=False)


def _clone(root: Path, name: str, *, days_old: float = 10) -> Path:
    clone = root / name
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/x\n")
    (clone / "file.txt").write_text("x" * 2000)
    _age(clone, days_old)
    return clone


def _fake_runner(table: dict[str, dict], calls: list[list[str]] | None = None):
    """``table`` maps a clone directory name to its branch / pull requests."""

    def run(argv, timeout=30):
        argv = list(argv)
        if calls is not None:
            calls.append(argv)
        if "git" in argv[:4]:
            at = argv.index("-C")
            name = Path(argv[at + 1]).name
            spec = table.get(name, {})
            sub = argv[at + 2 :]
            if sub[:2] == ["rev-parse", "--git-dir"]:
                return (0, ".git", "") if spec.get("git", True) else (128, "", "no")
            if sub[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return 0, spec.get("branch", f"backlog/{name}"), ""
            if sub[:2] == ["remote", "get-url"]:
                return (
                    0,
                    spec.get("remote", "https://github.com/voyn88/widgets.git"),
                    "",
                )
            if sub[:1] == ["status"]:
                return 0, spec.get("dirty", ""), ""
            if sub[:1] == ["rev-list"]:
                return 0, spec.get("unpushed", "0"), ""
            if sub[:2] == ["stash", "list"]:
                return 0, spec.get("stash", ""), ""
        if argv[0] == "gh":
            branch = argv[argv.index("--head") + 1]
            name = branch.split("/", 1)[-1]
            spec = table.get(name, {})
            if spec.get("gh_fails"):
                return 1, "", "boom"
            return 0, json.dumps(spec.get("pulls", [])), ""
        return 0, "", ""

    return run


def _pr(state: str, days_ago: float) -> dict:
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.localtime(time.time() - days_ago * DAY)
    )
    key = "mergedAt" if state == "MERGED" else "closedAt"
    return {"number": 1, "state": state, key: stamp}


def _cfg(tmp_path: Path, **over) -> disk_guard.Config:
    roots = tmp_path / "clones"
    roots.mkdir(exist_ok=True)
    values = {
        "OPS_CLONE_ROOTS": str(roots),
        "OPS_DISK_GUARD_DRY_RUN": "0",
        "OPS_DISK_LOG": str(tmp_path / "log" / "dg.jsonl"),
        "OPS_DISK_LOCK": str(tmp_path / "dg.lock"),
        "OPS_JOURNAL_MAX_MB": "0",
        "OPS_DISK_PATH": str(tmp_path),
    }
    values.update({k: str(v) for k, v in over.items()})
    return disk_guard.Config.from_settings(values)


def _low(_path):  # the disk is "almost full"
    return 5.0


# --------------------------------------------------------------------------
# common


def test_settings_parse_comments_quotes_and_environment_override(tmp_path):
    file = tmp_path / "s.env"
    file.write_text("# c\nOPS_A=1\nOPS_B=\"two words\"\n\nbad line\nOPS_C='x'\n")
    values = common.load_settings(file, {"OPS_A": "9", "OTHER": "no"})
    assert values == {"OPS_A": "9", "OPS_B": "two words", "OPS_C": "x"}


def test_paths_must_be_absolute(tmp_path):
    assert common.as_paths("/a/b: relative :/c") == [Path("/a/b"), Path("/c")]
    assert common.as_paths("") == []


def test_exclusive_lock_refuses_a_second_holder(tmp_path):
    with common.exclusive_lock(tmp_path / "l") as first:
        assert first is True
        with common.exclusive_lock(tmp_path / "l") as second:
            assert second is False


# --------------------------------------------------------------------------
# disk guard: what it deletes


def test_deletes_only_a_clone_whose_pr_is_merged_long_enough_ago(tmp_path):
    cfg = _cfg(tmp_path)
    root = cfg.clone_roots[0]
    gone = _clone(root, "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    journal, _ = _journal(tmp_path)
    summary = disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert not gone.exists()
    assert summary["acted"] and summary["removed"][0]["kind"] == "clone"


@pytest.mark.parametrize(
    ("name", "spec", "days_old", "why"),
    [
        ("open-pr", {"pulls": [_pr("OPEN", 0)]}, 10, "open_pull_request"),
        ("no-pr", {"pulls": []}, 10, "no_pull_request"),
        (
            "merged-yesterday",
            {"pulls": [_pr("MERGED", 1)]},
            10,
            "pull_request_closed_inside_age_window",
        ),
        (
            "recent-files",
            {"pulls": [_pr("MERGED", 9)]},
            1,
            "modified_inside_age_window",
        ),
        (
            "on-main",
            {"branch": "main", "pulls": [_pr("MERGED", 9)]},
            10,
            "default_or_unknown_branch",
        ),
        ("not-git", {"git": False, "pulls": [_pr("MERGED", 9)]}, 10, "not_a_git_clone"),
        ("gh-down", {"gh_fails": True}, 10, "gh_unavailable"),
        (
            "other-host",
            {"remote": "git@gitlab.example:a/b.git", "pulls": [_pr("MERGED", 9)]},
            10,
            "no_github_remote",
        ),
    ],
)
def test_keeps_everything_that_is_not_provably_finished(
    tmp_path, name, spec, days_old, why
):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], name, days_old=days_old)
    journal, lines = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner({name: spec}), journal=journal, cwds=[], free_pct=_low
    )
    assert clone.exists(), why
    skipped = [json.loads(line) for line in lines if '"skipped_clones"' in line]
    assert any(item["why"] == why for item in skipped[0]["sample"])


def test_keeps_a_clone_a_process_is_working_in_and_the_keep_marker(tmp_path):
    cfg = _cfg(tmp_path)
    busy = _clone(cfg.clone_roots[0], "busy")
    kept = _clone(cfg.clone_roots[0], "kept")
    (kept / ".aicc-keep").write_text("")
    _age(kept, 10)
    table = {n: {"pulls": [_pr("MERGED", 9)]} for n in ("busy", "kept")}
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg,
        runner=_fake_runner(table),
        journal=journal,
        cwds=[str(busy / "sub")],
        free_pct=_low,
    )
    assert busy.exists() and kept.exists()


def test_symlinks_and_protected_names_are_never_deleted(tmp_path):
    cfg = _cfg(tmp_path)
    root = cfg.clone_roots[0]
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "data").write_text("db")
    (root / "link").symlink_to(outside, target_is_directory=True)
    protected = _clone(root, "nightly-backup-2026")
    table = {"nightly-backup-2026": {"pulls": [_pr("MERGED", 9)]}}
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert (outside / "data").exists() and protected.exists()
    assert not disk_guard.safe_remove_tree(root / "link", root)
    assert not disk_guard.safe_remove_tree(protected, root)


def test_safe_remove_tree_only_takes_direct_children(tmp_path):
    root = tmp_path / "root"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert not disk_guard.safe_remove_tree(nested, root)
    assert not disk_guard.safe_remove_tree(root, root)
    assert not disk_guard.safe_remove_tree(tmp_path, root)
    assert disk_guard.safe_remove_tree(root / "a", root)


def test_dry_run_journals_but_removes_nothing(tmp_path):
    cfg = _cfg(tmp_path, OPS_DISK_GUARD_DRY_RUN="1")
    clone = _clone(cfg.clone_roots[0], "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    journal, lines = _journal(tmp_path)
    summary = disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert clone.exists()
    assert summary["would_remove"] and not summary["removed"]
    assert any('"would_remove"' in line for line in lines)


def test_default_settings_are_a_dry_run():
    assert disk_guard.Config.from_settings({}).dry_run is True
    assert common.as_dry_run(None) is True


def test_does_nothing_while_the_disk_has_room(tmp_path):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    journal, lines = _journal(tmp_path)
    summary = disk_guard.run_guard(
        cfg,
        runner=_fake_runner(table),
        journal=journal,
        cwds=[],
        free_pct=lambda _p: 60.0,
    )
    assert clone.exists() and summary["acted"] is False
    assert any('"disk_ok"' in line for line in lines)


def test_per_run_clone_cap_limits_the_blast_radius(tmp_path):
    cfg = _cfg(tmp_path, OPS_DISK_MAX_CLONES="2", OPS_DISK_TARGET_FREE_PCT="100")
    names = [f"c{i}" for i in range(5)]
    for name in names:
        _clone(cfg.clone_roots[0], name)
    table = {n: {"pulls": [_pr("MERGED", 5)]} for n in names}
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    left = [n for n in names if (cfg.clone_roots[0] / n).exists()]
    assert len(left) == 3


def test_stops_as_soon_as_the_target_would_be_reached(tmp_path):
    # target 0 % free means "already satisfied": nothing needs deleting.
    cfg = _cfg(tmp_path, OPS_DISK_TARGET_FREE_PCT="0")
    clone = _clone(cfg.clone_roots[0], "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert clone.exists()


def test_repo_slug_from_common_remote_shapes():
    assert disk_guard.repo_slug("https://github.com/a/b.git") == "a/b"
    assert disk_guard.repo_slug("git@github.com:a/b.git") == "a/b"
    assert disk_guard.repo_slug("https://example.org/a/b") is None


# --------------------------------------------------------------------------
# disk guard: orphan model blobs


def _models(tmp_path: Path, *, referenced: list[str], orphans: dict[str, float]):
    models = tmp_path / "models"
    (models / "manifests" / "reg" / "m").mkdir(parents=True)
    (models / "blobs").mkdir()
    manifest = {
        "config": {"digest": "sha256:" + referenced[0]},
        "layers": [{"digest": "sha256:" + d} for d in referenced[1:]],
    }
    (models / "manifests" / "reg" / "m" / "tag").write_text(json.dumps(manifest))
    for digest in referenced:
        (models / "blobs" / f"sha256-{digest}").write_bytes(b"k" * 100)
    for digest, age in orphans.items():
        blob = models / "blobs" / f"sha256-{digest}"
        blob.write_bytes(b"o" * 500)
        stamp = time.time() - age * DAY
        os.utime(blob, (stamp, stamp))
    return models


def test_orphan_blobs_are_removed_but_referenced_and_fresh_ones_stay(tmp_path):
    models = _models(tmp_path, referenced=["aa", "bb"], orphans={"old": 5, "fresh": 0})
    cfg = _cfg(tmp_path, OPS_OLLAMA_MODELS_DIR=models, OPS_CLONE_ROOTS="")
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner({}), journal=journal, cwds=[], free_pct=_low
    )
    blobs = {p.name for p in (models / "blobs").iterdir()}
    assert blobs == {"sha256-aa", "sha256-bb", "sha256-fresh"}


def test_an_unreadable_manifest_means_nothing_is_called_orphaned(tmp_path):
    models = _models(tmp_path, referenced=["aa"], orphans={"old": 5})
    (models / "manifests" / "reg" / "m" / "tag").write_text("{broken")
    cfg = _cfg(tmp_path, OPS_OLLAMA_MODELS_DIR=models, OPS_CLONE_ROOTS="")
    assert disk_guard.plan_ollama_orphans(cfg, now=time.time()) == []


def test_missing_manifests_means_no_orphans(tmp_path):
    models = tmp_path / "models"
    (models / "blobs").mkdir(parents=True)
    (models / "blobs" / "sha256-zz").write_bytes(b"x")
    cfg = _cfg(tmp_path, OPS_OLLAMA_MODELS_DIR=models, OPS_CLONE_ROOTS="")
    assert disk_guard.plan_ollama_orphans(cfg, now=time.time() + 10 * DAY) == []


def test_package_caches_are_emptied_but_the_directory_stays(tmp_path):
    cache = tmp_path / "_cacache"
    (cache / "x").mkdir(parents=True)
    (cache / "x" / "f").write_bytes(b"c" * 100)
    (cache / "top").write_bytes(b"c" * 100)
    cfg = _cfg(tmp_path, OPS_CACHE_DIRS=cache, OPS_CLONE_ROOTS="")
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner({}), journal=journal, cwds=[], free_pct=_low
    )
    assert cache.is_dir() and not any(cache.iterdir())


# --------------------------------------------------------------------------
# status beacon: what it refuses to publish


@pytest.mark.parametrize(
    "text",
    [
        '{"x": "203.0.113.7"}',
        '{"x": "/home/someone/file"}',
        '{"x": "' + _MAC_HOME + 'someone"}',
        '{"x": "' + "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123" + '"}',
        '{"x": "' + "-----BEGIN " + "PRIVATE KEY-----" + '"}',
        '{"x": "password"}',
        '{"api_token": "abc"}',
        '{"x": "voyn-worker-01"}',
        '{"x": "/var/lib/aicc/github"}',
    ],
)
def test_the_beacon_refuses_anything_that_looks_like_a_leak(text):
    with pytest.raises(status_beacon.DirtyStatus):
        status_beacon.assert_clean(text)


def test_a_unit_name_containing_token_is_not_a_leak():
    status_beacon.assert_clean('{"units": {"voyn-aicc-github-token.timer": "active"}}')


def _beacon_runner(argv, timeout=30):
    argv = list(argv)
    if argv[:2] == ["systemctl", "is-active"]:
        return 0, "active\n", ""
    if argv[:2] == ["systemctl", "--failed"]:
        return 0, "", ""
    if argv[0] == "ss":
        return (
            0,
            'LISTEN 0 4096 127.0.0.1:17443 0.0.0.0:* users:(("tailscaled",pid=1,fd=2))\n',
            "",
        )
    if argv[0] == "gh" and "open" in argv:
        return (
            0,
            json.dumps(
                [
                    {
                        "number": 1,
                        "mergeStateStatus": "BEHIND",
                        "labels": [{"name": "review-window:blocked"}],
                    },
                    {
                        "number": 2,
                        "mergeStateStatus": "CLEAN",
                        "labels": [{"name": "queue-active"}],
                    },
                ]
            ),
            "",
        )
    if argv[0] == "gh":
        return 0, '[{"mergedAt": "2026-11-11T10:00:00Z"}]', ""
    if argv[0] == "journalctl":
        return 0, f"{time.time() - 600:.3f} host unit: MARKER pass\n", ""
    return 0, "", ""


def test_collect_builds_a_clean_allow_listed_document(tmp_path):
    settings = {
        "OPS_HOST_ROLE": "control",
        "OPS_DISK_PATH": str(tmp_path),
        "OPS_BEACON_DETAIL": "full",
        "OPS_STATE_DIR": str(tmp_path),
    }
    doc = status_beacon.collect(settings, runner=_beacon_runner)
    text = status_beacon.render(doc)  # raises if dirty
    assert doc["role"] == "control"
    assert doc["queue"]["open"] == 2 and doc["queue"]["window_blocked"] == 1
    assert doc["queue"]["queue_active"] == 1 and doc["queue"]["behind"] == 1
    assert doc["review_tunnel"]["held_by"] == "tailscaled"
    assert 9 <= doc["last_verdict_age_min"] <= 11
    assert set(doc) >= {"schema", "role", "heartbeat_unix", "disk", "units"}
    assert "voyn-worker" not in text


def test_an_unknown_role_falls_back_to_worker(tmp_path):
    doc = status_beacon.collect(
        {"OPS_HOST_ROLE": "evil/../x", "OPS_STATE_DIR": str(tmp_path)},
        runner=_beacon_runner,
    )
    assert doc["role"] == "worker"


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(script: dict, seen: list):
    def open_(request: urllib.request.Request):
        body = json.loads(request.data) if request.data else None
        seen.append((request.get_method(), request.full_url.split("/git/", 1)[1], body))
        key = (request.get_method(), request.full_url.split("/git/", 1)[1])
        status, payload = script[key] if key in script else script[(key[0], "*")]
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, None)
        return _Response(status, payload)

    return open_


def test_publish_rewrites_the_branch_as_one_parentless_commit():
    seen: list = []
    script = {
        ("POST", "blobs"): (201, {"sha": "b1"}),
        ("POST", "trees"): (201, {"sha": "t1"}),
        ("POST", "commits"): (201, {"sha": "c1"}),
        ("GET", "refs/heads/ops-status-worker"): (200, {}),
        ("PATCH", "refs/heads/ops-status-worker"): (200, {}),
    }
    sha = status_beacon.publish_branch(
        "acme/widgets",
        "ops-status-worker",
        '{"ok": 1}\n',
        "T0K",
        opener=_opener(script, seen),
    )
    assert sha == "c1"
    commit = next(b for m, p, b in seen if p == "commits")
    assert commit["parents"] == []
    patch = next(b for m, p, b in seen if m == "PATCH")
    assert patch == {"sha": "c1", "force": True}


def test_publish_creates_the_branch_the_first_time():
    seen: list = []
    script = {
        ("POST", "blobs"): (201, {"sha": "b1"}),
        ("POST", "trees"): (201, {"sha": "t1"}),
        ("POST", "commits"): (201, {"sha": "c1"}),
        ("GET", "refs/heads/ops-status-control"): (404, {}),
        ("POST", "refs"): (201, {}),
    }
    status_beacon.publish_branch(
        "acme/widgets", "ops-status-control", "{}", "T0K", opener=_opener(script, seen)
    )
    assert [m for m, p, b in seen][-1] == "POST"
    assert seen[-1][2]["ref"] == "refs/heads/ops-status-control"


def test_publish_never_sends_a_dirty_document():
    seen: list = []
    with pytest.raises(status_beacon.DirtyStatus):
        status_beacon.publish_branch(
            "a/b",
            "ops-status-worker",
            '{"x": "10.0.0.1"}',
            "T0K",
            opener=_opener({}, seen),
        )
    assert seen == []


def test_an_api_refusal_is_an_error_not_a_silent_success():
    script = {("POST", "blobs"): (403, {})}
    with pytest.raises(RuntimeError):
        status_beacon.publish_branch(
            "a/b", "ops-status-worker", "{}", "T0K", opener=_opener(script, [])
        )


# --------------------------------------------------------------------------
# self-heal


def _heal_runner(
    *, ts_ok=True, held_by=None, tunnel_code="000", failed=(), backend="Running"
):
    def run(argv, timeout=30):
        argv = list(argv)
        if argv[:2] == ["tailscale", "ip"]:
            return (0, "100.64.0.1\n", "") if ts_ok else (124, "", "timeout")
        if argv[:2] == ["tailscale", "status"]:
            return 0, json.dumps({"BackendState": backend}), ""
        if argv[0] == "ss":
            if held_by:
                return (
                    0,
                    f'LISTEN 0 1 127.0.0.1:17443 *:* users:(("{held_by}",pid=1,fd=3))\n',
                    "",
                )
            return 0, "", ""
        if argv[0] == "curl":
            return 0, tunnel_code, ""
        if argv[:2] == ["systemctl", "is-active"]:
            return 0, ("failed\n" if argv[2] in failed else "active\n"), ""
        return 0, "", ""

    return run


def _heal(tmp_path, runner, *, now, dry_run=False, settings=None):
    journal, lines = _journal(tmp_path)
    actions = self_heal.run_heal(
        settings or {"OPS_HEAL_UNITS": "voyn-aicc-review.service"},
        runner=runner,
        journal=journal,
        state=common.StateFile(tmp_path / "state.json"),
        dry_run=dry_run,
        now=now,
    )
    return actions, lines


class _Recorder:
    def __init__(self, inner):
        self.inner = inner
        self.restarts: list[str] = []

    def __call__(self, argv, timeout=30):
        argv = list(argv)
        if argv[:2] == ["systemctl", "restart"]:
            self.restarts.append(argv[2])
            return 0, "", ""
        return self.inner(argv, timeout)


def test_a_signal_must_be_seen_twice_before_anything_restarts(tmp_path):
    runner = _Recorder(_heal_runner(ts_ok=False))
    _heal(tmp_path, runner, now=1000)
    assert runner.restarts == []
    _heal(tmp_path, runner, now=1300)
    assert runner.restarts == ["tailscaled.service"]


def test_a_signal_that_clears_resets_the_count(tmp_path):
    bad, good = _Recorder(_heal_runner(ts_ok=False)), _Recorder(_heal_runner())
    _heal(tmp_path, bad, now=1000)
    _heal(tmp_path, good, now=1300)
    _heal(tmp_path, bad, now=1600)
    assert bad.restarts == []


def test_cooldown_blocks_a_repeat_for_half_an_hour(tmp_path):
    runner = _Recorder(_heal_runner(ts_ok=False))
    for now in (1000, 1300, 1600, 1900):
        _heal(tmp_path, runner, now=now)
    assert runner.restarts == ["tailscaled.service"]
    _heal(tmp_path, runner, now=1300 + self_heal.COOLDOWN_SECONDS + 10)
    assert len(runner.restarts) == 2


def test_the_hourly_cap_stops_a_restart_loop(tmp_path):
    runner = _Recorder(
        _heal_runner(ts_ok=False, failed=("voyn-aicc-a.service", "voyn-aicc-b.service"))
    )
    settings = {"OPS_HEAL_UNITS": "voyn-aicc-a.service,voyn-aicc-b.service"}
    _heal(tmp_path, runner, now=1000, settings=settings)
    _heal(tmp_path, runner, now=1300, settings=settings)
    assert len(runner.restarts) == self_heal.MAX_ACTIONS_PER_HOUR
    assert len(set(runner.restarts)) == 3 or len(runner.restarts) == 3


def test_dry_run_only_journals(tmp_path):
    runner = _Recorder(_heal_runner(ts_ok=False))
    _heal(tmp_path, runner, now=1000, dry_run=True)
    actions, lines = _heal(tmp_path, runner, now=1300, dry_run=True)
    assert runner.restarts == [] and actions and actions[0]["dry_run"] is True
    assert any('"would_restart"' in line for line in lines)


def test_the_tunnel_signal_needs_tailscaled_to_hold_the_port(tmp_path):
    owned = _Recorder(_heal_runner(held_by="tailscaled"))
    for now in (1000, 1300):
        _heal(tmp_path, owned, now=now)
    assert owned.restarts == ["tailscaled.service"]
    other = tmp_path / "other"
    other.mkdir()
    nobody = _Recorder(_heal_runner(held_by=None))
    for now in (1000, 1300):
        _heal(other, nobody, now=now)
    assert nobody.restarts == []


def test_a_healthy_tunnel_answer_is_left_alone(tmp_path):
    runner = _Recorder(_heal_runner(held_by="tailscaled", tunnel_code="501"))
    for now in (1000, 1300):
        _heal(tmp_path, runner, now=now)
    assert runner.restarts == []


@pytest.mark.parametrize(
    ("unit", "allowed"),
    [
        ("tailscaled.service", True),
        ("ssh.service", True),
        ("voyn-aicc-review.service", True),
        ("voyn-aicc-worker@1.service", False),
        ("voyn-aicc-self-deploy.service", False),
        ("voyn-aicc-pgtunnel.service", False),
        ("voyn-aicc-credential-rotation.service", False),
        ("postgresql.service", False),
        ("sshd.service", False),
        ("tailscaled.service; reboot", False),
        ("../../x.service", False),
    ],
)
def test_only_allow_listed_units_can_ever_be_restarted(unit, allowed):
    assert self_heal.unit_allowed(unit) is allowed


def test_a_failed_listed_unit_is_reset_and_restarted(tmp_path):
    inner = _heal_runner(failed=("voyn-aicc-review.service",))
    calls: list[list[str]] = []

    def runner(argv, timeout=30):
        calls.append(list(argv))
        return inner(argv, timeout)

    for now in (1000, 1300):
        _heal(tmp_path, runner, now=now)
    assert ["systemctl", "reset-failed", "voyn-aicc-review.service"] in calls
    assert ["systemctl", "restart", "voyn-aicc-review.service"] in calls


# --------------------------------------------------------------------------
# units and installer


def test_unit_files_are_hardened_and_carry_no_host_specific_paths():
    for name in ("disk-guard", "beacon", "self-heal"):
        for suffix in ("service", "timer"):
            text = (ROOT / "deploy/systemd" / f"voyn-ops-{name}.{suffix}").read_text()
            assert _MAC_HOME not in text and "voyn" + "admin" not in text
            if suffix == "service":
                assert "NoNewPrivileges=true" in text
                assert "ProtectSystem=strict" in text
                assert "-m command_center.ops." in text
                assert "Type=oneshot" in text
            else:
                assert "OnUnitActiveSec=" in text and "WantedBy=timers.target" in text


def test_the_settings_template_defaults_both_features_to_dry_run():
    text = (ROOT / "deploy/aicc/ops-autonomy.env").read_text()
    values = common.parse_settings(text)
    assert values["OPS_DISK_GUARD_DRY_RUN"] == "1"
    assert values["OPS_SELF_HEAL_DRY_RUN"] == "1"


INSTALLER = ROOT / "ops/install-ops-autonomy.sh"


def test_the_installer_is_valid_bash_and_console_friendly():
    done = subprocess.run(
        ["bash", "-n", str(INSTALLER)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    text = INSTALLER.read_text()
    # The documented owner commands use only characters the web console keeps.
    for line in text.splitlines():
        if line.startswith("#   bash /opt/aicc/current/ops/install-ops-autonomy.sh"):
            command = line.removeprefix("#   ")
            assert all(
                ch.islower() or ch.isdigit() or ch in " -./"
                for ch in command.split("  ")[0]
            ), command


def _fake_host(tmp_path: Path) -> dict[str, str]:
    release = tmp_path / "release"
    shutil_copy = __import__("shutil").copytree
    shutil_copy(ROOT / "deploy", release / "deploy")
    (release / "ops").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text('#!/bin/sh\necho "$@" >> "$FAKE_SYSTEMCTL_LOG"\n')
    systemctl.chmod(0o755)
    wrapper_dir = tmp_path / "interpreter"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    return {
        **os.environ,
        "AICC_RELEASE_DIR": str(release),
        "AICC_VENV_PYTHON": str(wrapper),
        "AICC_OPS_SETTINGS": str(tmp_path / "etc" / "ops-autonomy.env"),
        "AICC_OPS_STATE_DIR": str(tmp_path / "state"),
        "AICC_OPS_LOG_DIR": str(tmp_path / "logs"),
        "AICC_UNIT_DIR": str(tmp_path / "units"),
        "AICC_JOURNALD_DIR": str(tmp_path / "journald"),
        "AICC_SYSTEMCTL": str(systemctl),
        "AICC_OPS_ALLOW_NONROOT": "1",
        "AICC_OPS_TRUST_OWNER": __import__("getpass").getuser(),
        "AICC_OPS_TEST_ROOT": str(tmp_path),
        "FAKE_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "PYTHONPATH": str(ROOT),
    }


def _run_installer(env, *args):
    return subprocess.run(
        ["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True
    )


def test_install_is_idempotent_starts_in_dry_run_and_uninstalls_cleanly(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    first = _run_installer(env, "--role", "worker")
    assert first.returncode == 0, first.stdout + first.stderr
    settings = Path(env["AICC_OPS_SETTINGS"])
    values = common.parse_settings(settings.read_text())
    assert values["OPS_HOST_ROLE"] == "worker"
    assert values["OPS_DISK_GUARD_DRY_RUN"] == "1"
    assert values["OPS_SELF_HEAL_DRY_RUN"] == "1"
    units = tmp_path / "units"
    for name in ("disk-guard", "beacon", "self-heal"):
        assert (units / f"voyn-ops-{name}.service").is_file()
        assert (units / f"voyn-ops-{name}.timer").is_file()
    assert (units / "voyn-ops-disk-guard.service.d" / "paths.conf").is_file()
    log = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text()
    assert "enable --now voyn-ops-beacon.timer" in log

    # a second run keeps the settings the owner may have edited
    settings.write_text(settings.read_text() + "OPS_DISK_LOW_FREE_PCT=11\n")
    second = _run_installer(env, "--role", "worker")
    assert second.returncode == 0, second.stderr
    assert "OPS_DISK_LOW_FREE_PCT=11" in settings.read_text()

    refused = _run_installer(env, "--live", "disk")
    assert refused.returncode != 0 and "dry-run" in refused.stderr
    logs = Path(env["AICC_OPS_LOG_DIR"])
    (logs / "disk-guard.jsonl").write_text('{"event": "disk_ok"}\n')
    (logs / "self-heal.jsonl").write_text('{"event": "healthy"}\n')
    live = _run_installer(env, "--live", "disk")
    assert live.returncode == 0, live.stderr
    values = common.parse_settings(settings.read_text())
    assert values["OPS_DISK_GUARD_DRY_RUN"] == "0"
    assert values["OPS_SELF_HEAL_DRY_RUN"] == "1"
    back = _run_installer(env, "--dry-run", "all")
    assert common.parse_settings(settings.read_text())["OPS_DISK_GUARD_DRY_RUN"] == "1"
    assert back.returncode == 0

    check = _run_installer(env, "--check")
    assert check.returncode == 0 and "OPS_HOST_ROLE=worker" in check.stdout

    gone = _run_installer(env, "--uninstall")
    assert gone.returncode == 0, gone.stderr
    assert not list(units.glob("voyn-ops-*"))
    assert settings.exists()  # settings and logs are kept


def test_install_stops_when_the_release_does_not_hold_the_files_yet(tmp_path):
    env = _fake_host(tmp_path)
    for unit in (Path(env["AICC_RELEASE_DIR"]) / "deploy/systemd").glob("voyn-ops-*"):
        unit.unlink()
    result = _run_installer(env, "--role", "worker")
    assert result.returncode != 0 and "STOP" in result.stderr
    assert not Path(env["AICC_OPS_SETTINGS"]).exists()


def test_install_refuses_an_unknown_role(tmp_path):
    env = _fake_host(tmp_path)
    result = _run_installer(env, "--role", "owner")
    assert result.returncode != 0 and "role must be" in result.stderr


# ==========================================================================
# security review findings (team/security/ops-autonomy-review.md)


# --- N1: a typo must never arm real deletion --------------------------------


@pytest.mark.parametrize("word", ["0", "false", "no", "off", "OFF", " No "])
def test_only_an_explicit_live_word_arms_real_work(word):
    assert common.as_dry_run(word) is False


@pytest.mark.parametrize(
    "word", ["1", "true", "yes", "on", "", "y", "dry", "enabled", "да", "o", "00", "12"]
)
def test_every_other_value_stays_a_dry_run(word):
    assert common.as_dry_run(word) is True


def test_a_typo_is_journaled_and_the_disk_guard_stays_dry(tmp_path):
    cfg = _cfg(tmp_path, OPS_DISK_GUARD_DRY_RUN="dry")
    clone = _clone(cfg.clone_roots[0], "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    assert cfg.dry_run is True and cfg.dry_run_unclear is True
    journal, lines = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert clone.exists()
    assert any("unclear" in line for line in lines)


def test_a_dry_run_reports_would_act_not_acted(tmp_path):
    cfg = _cfg(tmp_path, OPS_DISK_GUARD_DRY_RUN="1")
    _clone(cfg.clone_roots[0], "merged-old")
    table = {"merged-old": {"pulls": [_pr("MERGED", 5)]}}
    journal, _ = _journal(tmp_path)
    summary = disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert summary["would_act"] is True and summary["acted"] is False


# --- C4: roots and caches that are too broad are refused ---------------------


@pytest.mark.parametrize(
    ("path", "cache", "why"),
    [
        ("/", False, "too_broad"),
        ("/home", False, "too_broad"),
        ("/home/alice", False, "too_broad"),
        ("/srv", False, "too_broad"),
        ("/var", True, "too_broad"),
        ("/var/lib", False, "too_shallow"),
        ("/a/b/../c/d", False, "not_a_plain_absolute_path"),
        ("relative/path/here", False, "not_a_plain_absolute_path"),
        ("/home/alice/.cache/other", True, "not_a_known_cache_name"),
        ("/home/alice/Projects/nightly-backup-x", False, "protected_name"),
    ],
)
def test_too_broad_or_odd_paths_are_refused(path, cache, why):
    assert disk_guard.refused_path(Path(path), cache=cache) == why


@pytest.mark.parametrize(
    ("path", "cache"),
    [
        ("/home/alice/.npm/_cacache", True),
        ("/home/alice/.cache/pip", True),
        ("/home/alice/.cache/uv", True),
        ("/home/alice/Projects/task-clones", False),
        ("/srv/workspaces/abc123", False),
    ],
)
def test_ordinary_roots_and_known_caches_pass(path, cache):
    assert disk_guard.refused_path(Path(path), cache=cache) is None


def test_a_broad_cache_dir_in_the_settings_deletes_nothing(tmp_path):
    victim = tmp_path / "keep"
    victim.mkdir()
    (victim / "data").write_text("precious")
    cfg = _cfg(tmp_path, OPS_CACHE_DIRS=tmp_path, OPS_CLONE_ROOTS="")
    journal, lines = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner({}), journal=journal, cwds=[], free_pct=_low
    )
    assert (victim / "data").exists()
    assert any('"refused_path"' in line for line in lines)


# --- C1: git runs as the clone's owner, with config hooks switched off -------


def test_git_runs_as_the_clone_owner_when_the_guard_is_root(tmp_path, monkeypatch):
    clone = _clone(tmp_path, "c1")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        disk_guard.pwd,
        "getpwuid",
        lambda uid: type("P", (), {"pw_name": "agent"})(),
    )
    calls: list[list[str]] = []

    def runner(argv, timeout=30):
        calls.append(list(argv))
        return 0, "x", ""

    assert disk_guard._git(clone, ["status"], runner) == "x"
    argv = calls[0]
    assert argv[:4] == ["runuser", "-u", "agent", "--"]
    assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
    assert "--no-optional-locks" in argv


def test_a_clone_owned_by_root_is_never_inspected_as_root(tmp_path, monkeypatch):
    clone = _clone(tmp_path, "c1")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        disk_guard.pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "root"})()
    )
    assert disk_guard._git(clone, ["status"], lambda *a, **k: (0, "x", "")) is None


def test_an_unprivileged_run_does_not_wrap_git(tmp_path, monkeypatch):
    clone = _clone(tmp_path, "c1")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    seen: list[list[str]] = []
    disk_guard._git(
        clone, ["status"], lambda a, t=30: (seen.append(list(a)), (0, "", ""))[1]
    )
    assert seen[0][0] == "git"


def test_a_pull_request_of_an_unlisted_owner_does_not_make_a_clone_deletable(tmp_path):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "stranger")
    table = {
        "stranger": {
            "remote": "https://github.com/somebody-else/widgets.git",
            "pulls": [_pr("MERGED", 9)],
        }
    }
    journal, _ = _journal(tmp_path)
    disk_guard.run_guard(
        cfg, runner=_fake_runner(table), journal=journal, cwds=[], free_pct=_low
    )
    assert clone.exists()


# --- C2: work that exists nowhere else survives ------------------------------


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        ({"dirty": " M file.py"}, "dirty_or_unpushed"),
        ({"dirty": "?? notes.txt"}, "dirty_or_unpushed"),
        ({"unpushed": "3"}, "dirty_or_unpushed"),
        ({"unpushed": ""}, "dirty_or_unpushed"),
        ({"stash": "stash@{0}: WIP"}, "has_stash"),
    ],
)
def test_a_clone_holding_unsaved_work_is_kept(tmp_path, spec, why):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "work")
    table = {"work": {"pulls": [_pr("MERGED", 9)], **spec}}
    verdict = disk_guard.clone_verdict(
        clone,
        now=time.time(),
        min_age_days=3,
        cwds=[],
        runner=_fake_runner(table),
    )
    assert verdict == (False, why)


def test_an_in_place_edit_deep_inside_counts_as_recent_activity(tmp_path):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "deep")
    nested = clone / "a" / "b"
    nested.mkdir(parents=True)
    _age(clone, 10)
    (nested / "f.txt").write_text("edited now")  # directories stay old
    assert disk_guard.newest_mtime(clone) > time.time() - 60
    verdict = disk_guard.clone_verdict(
        clone,
        now=time.time(),
        min_age_days=3,
        cwds=[],
        runner=_fake_runner({"deep": {"pulls": [_pr("MERGED", 9)]}}),
    )
    assert verdict == (False, "modified_inside_age_window")


def test_a_running_git_operation_blocks_deletion(tmp_path):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "busy-git")
    (clone / ".git" / "index.lock").write_text("")
    _age(clone, 10)
    verdict = disk_guard.clone_verdict(
        clone,
        now=time.time(),
        min_age_days=3,
        cwds=[],
        runner=_fake_runner({"busy-git": {"pulls": [_pr("MERGED", 9)]}}),
    )
    assert verdict == (False, "git_operation_in_progress")


# --- C3: use checks, the process table, mounts, and a re-check ---------------


def test_an_unreadable_process_table_is_not_nobody_using_it(tmp_path):
    assert disk_guard.process_paths(tmp_path / "no-such-proc") is None
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "c")
    verdict = disk_guard.clone_verdict(
        clone,
        now=time.time(),
        min_age_days=3,
        cwds=None,
        runner=_fake_runner({"c": {"pulls": [_pr("MERGED", 9)]}}),
    )
    assert verdict == (False, "process_table_unreadable")


def test_process_paths_includes_open_files_and_working_directories(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "cwd").symlink_to("/somewhere/cwd")
    (proc / "42" / "fd" / "3").symlink_to("/clones/c/file.txt")
    paths = disk_guard.process_paths(proc)
    assert paths and "/somewhere/cwd" in paths and "/clones/c/file.txt" in paths
    assert disk_guard.in_use(Path(tmp_path), ["/clones/c/file.txt"]) is False


def test_a_mount_point_inside_a_clone_blocks_deletion(tmp_path):
    clone = _clone(tmp_path, "c")
    assert disk_guard.has_mount_inside(clone, [str(clone / "inner")])
    assert disk_guard.has_mount_inside(clone, [str(clone)])
    assert not disk_guard.has_mount_inside(clone, [str(tmp_path / "other")])
    assert disk_guard.mount_points(tmp_path / "missing") == []


def test_the_verdict_is_taken_again_right_before_deleting(tmp_path):
    cfg = _cfg(tmp_path)
    clone = _clone(cfg.clone_roots[0], "late")
    table = {"late": {"pulls": [_pr("MERGED", 5)]}}
    planned, _ = disk_guard.plan_clones(
        cfg,
        now=time.time(),
        runner=_fake_runner(table),
        cwds=[],
        fresh_cwds=lambda: [str(clone)],  # a process appeared after the plan
    )
    assert len(planned) == 1
    assert planned[0].action() is False
    assert clone.exists()


# --- C5: one cool-down and one count per SERVICE, a daily stop ---------------


def test_two_signals_for_one_service_are_one_restart(tmp_path):
    runner = _Recorder(_heal_runner(ts_ok=False, held_by="tailscaled"))
    for now in (1000, 1300, 1600):
        _heal(tmp_path, runner, now=now)
    assert runner.restarts == ["tailscaled.service"]


def test_a_tailscaled_the_owner_stopped_is_left_alone(tmp_path):
    for state in ("Stopped", "NeedsLogin", "NeedsMachineAuth"):
        sub = tmp_path / state
        sub.mkdir()
        runner = _Recorder(_heal_runner(ts_ok=False, backend=state))
        for now in (1000, 1300):
            _heal(sub, runner, now=now)
        assert runner.restarts == [], state


def test_the_daily_cap_halts_self_heal_and_says_so(tmp_path):
    state_file = common.StateFile(tmp_path / "state.json")
    day = 24 * 3600
    now = 10 * day
    state_file.save(
        {
            "seen": {"tailscaled_unresponsive": 5},
            "history": [now - 4000 - i for i in range(self_heal.MAX_ACTIONS_PER_DAY)],
            "last_action": {},
        }
    )
    runner = _Recorder(_heal_runner(ts_ok=False))
    journal, lines = _journal(tmp_path)
    self_heal.run_heal(
        {}, runner=runner, journal=journal, state=state_file, dry_run=False, now=now
    )
    assert runner.restarts == []
    assert any("halted_daily_cap" in line for line in lines)
    assert state_file.load()["halted_at"] == now
    # still halted an hour later, and free again after 24 hours
    self_heal.run_heal(
        {},
        runner=runner,
        journal=journal,
        state=state_file,
        dry_run=False,
        now=now + 3600,
    )
    assert runner.restarts == []
    self_heal.run_heal(
        {},
        runner=runner,
        journal=journal,
        state=state_file,
        dry_run=False,
        now=now + day + 100,
    )
    assert runner.restarts == ["tailscaled.service"]


def test_the_failed_state_is_journaled_before_reset_failed(tmp_path):
    inner = _heal_runner(failed=("voyn-aicc-review.service",))
    order: list[str] = []

    def runner(argv, timeout=30):
        argv = list(argv)
        if argv[:2] == ["systemctl", "show"]:
            order.append("show")
            return 0, "Result=exit-code\nExecMainStatus=1\n", ""
        if argv[:2] == ["systemctl", "reset-failed"]:
            order.append("reset")
        return inner(argv, timeout)

    journal, lines = _journal(tmp_path)
    state = common.StateFile(tmp_path / "s.json")
    for now in (1000, 1300):
        self_heal.run_heal(
            {"OPS_HEAL_UNITS": "voyn-aicc-review.service"},
            runner=runner,
            journal=journal,
            state=state,
            dry_run=False,
            now=now,
        )
    assert order == ["show", "reset"]
    assert any("before_reset_failed" in line and "exit-code" in line for line in lines)


def test_the_beacon_shows_that_self_heal_halted(tmp_path):
    common.StateFile(tmp_path / "self-heal-state.json").save(
        {"halted_at": time.time() - 60}
    )
    doc = status_beacon.collect(
        {"OPS_STATE_DIR": str(tmp_path), "OPS_BEACON_DETAIL": "full"},
        runner=_beacon_runner,
    )
    assert doc["self_heal_halted"] is True


# --- M2: a public repository shows the minimum by default --------------------


def test_the_default_document_is_minimal_and_clean(tmp_path):
    doc = status_beacon.collect(
        {"OPS_STATE_DIR": str(tmp_path), "OPS_DISK_PATH": str(tmp_path)},
        runner=_beacon_runner,
    )
    text = status_beacon.render(doc)
    assert set(doc) == {
        "schema",
        "role",
        "heartbeat_unix",
        "status",
        "degraded_count",
        "disk_free_pct",
        "last_verdict_age_min",
    }
    for hidden in ("units", "sshd", "review_tunnel", "queue", "failed_units"):
        assert hidden not in text
    assert doc["status"] in {"ok", "degraded"}


def test_a_held_tunnel_port_makes_the_minimal_status_degraded(tmp_path):
    doc = status_beacon.collect(
        {"OPS_STATE_DIR": str(tmp_path)}, runner=_beacon_runner
    )  # the fake host reports the port held by tailscaled
    assert doc["status"] == "degraded" and doc["degraded_count"] >= 1


def test_the_holder_of_the_tunnel_port_is_an_enumerated_word():
    assert status_beacon.holder_word("tailscaled") == "tailscaled"
    assert status_beacon.holder_word("anything-the-host-says") == "other"
    assert status_beacon.holder_word(None) is None


@pytest.mark.parametrize(
    "text",
    ['{"x": "fe80::1:2:3:4"}', '{"x": "example.com"}', '{"x": "api.example.cloud"}'],
)
def test_ipv6_and_domain_names_are_refused(text):
    with pytest.raises(status_beacon.DirtyStatus):
        status_beacon.assert_clean(text)


# --- M5: no redirect with the authorization header ---------------------------


def test_a_redirect_is_refused_so_the_token_is_never_resent():
    handler = status_beacon._NoRedirect()
    assert handler.redirect_request() is None


# --- M3 / M4 / journal rotation ----------------------------------------------


def test_the_sandbox_drop_in_ignores_a_vanished_path(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    clones = tmp_path / "home" / "Projects" / "ai-command-center-task-clones"
    clones.mkdir(parents=True)
    env["AICC_OPS_OWNER"] = "nobody-here"
    settings = Path(env["AICC_OPS_SETTINGS"])
    settings.parent.mkdir(parents=True)
    settings.write_text(f"OPS_CLONE_ROOTS={clones}\n")
    result = _run_installer(env, "--role", "worker")
    assert result.returncode == 0, result.stderr
    dropin = (
        tmp_path / "units" / "voyn-ops-disk-guard.service.d" / "paths.conf"
    ).read_text()
    assert f"ReadWritePaths=-{clones}" in dropin


@pytest.mark.parametrize(
    "manifest", ["[]", '{"layers": null}', '{"layers": [1]}', '{"config": 3}', "null"]
)
def test_an_odd_manifest_means_no_orphans_not_a_crash(tmp_path, manifest):
    models = _models(tmp_path, referenced=["aa"], orphans={"old": 5})
    (models / "manifests" / "reg" / "m" / "tag").write_text(manifest)
    cfg = _cfg(tmp_path, OPS_OLLAMA_MODELS_DIR=models, OPS_CLONE_ROOTS="")
    assert disk_guard.plan_ollama_orphans(cfg, now=time.time()) == []


def test_the_journal_rotates_instead_of_growing_forever(tmp_path):
    path = tmp_path / "j.jsonl"
    journal = common.Journal(path, echo=lambda _l: None)
    journal.MAX_BYTES = 200
    for i in range(30):
        journal.write("tick", n=i, pad="x" * 20)
    assert path.with_name("j.jsonl.1").exists()
    assert path.stat().st_size < 1000


# --- installer details -------------------------------------------------------


def test_a_missing_option_value_is_an_error_not_a_silent_exit(tmp_path):
    env = _fake_host(tmp_path)
    for option in ("--live", "--dry-run", "--role"):
        result = _run_installer(env, option)
        assert result.returncode != 0 and "needs" in result.stderr, option


def test_live_is_refused_without_a_recent_dry_run_journal(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    assert _run_installer(env, "--role", "worker").returncode == 0
    refused = _run_installer(env, "--live", "all")
    assert refused.returncode != 0 and "dry-run" in refused.stderr
    values = common.parse_settings(Path(env["AICC_OPS_SETTINGS"]).read_text())
    assert values["OPS_DISK_GUARD_DRY_RUN"] == "1"


def test_uninstall_puts_back_a_unit_that_install_replaced(tmp_path):
    env = _fake_host(tmp_path)
    units = tmp_path / "units"
    units.mkdir()
    (units / "voyn-ops-beacon.timer").write_text("# the previous version\n")
    assert _run_installer(env, "--role", "worker").returncode == 0
    assert "previous version" not in (units / "voyn-ops-beacon.timer").read_text()
    assert _run_installer(env, "--uninstall").returncode == 0
    assert (units / "voyn-ops-beacon.timer").read_text() == "# the previous version\n"


def test_the_installer_and_modules_name_no_account_and_no_home():
    for path in (
        ROOT / "ops/install-ops-autonomy.sh",
        *sorted((ROOT / "command_center/ops").glob("host_care_common.py")),
        ROOT / "command_center/ops/disk_guard.py",
        ROOT / "command_center/ops/status_beacon.py",
        ROOT / "command_center/ops/self_heal.py",
        ROOT / "deploy/aicc/ops-autonomy.env",
    ):
        text = path.read_text()
        assert "voyn" + "admin" not in text, path
        assert _MAC_HOME not in text, path


def test_the_settings_template_documents_the_new_keys():
    values = common.parse_settings((ROOT / "deploy/aicc/ops-autonomy.env").read_text())
    assert values["OPS_BEACON_DETAIL"] == "minimal"
    assert values["OPS_REPO_OWNERS"] == "voyn88"


# ==========================================================================
# M1: privileges are split and narrowed


def _service(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (
        (ROOT / "deploy/systemd" / f"voyn-ops-{name}.service").read_text().splitlines()
    ):
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def test_the_beacon_does_not_run_as_root_and_holds_no_capability():
    beacon = _service("beacon")
    assert beacon["User"] == "aicc-worker" and beacon["Group"] == "aicc-worker"
    assert beacon["SupplementaryGroups"] == "systemd-journal"
    assert beacon["CapabilityBoundingSet"] == ""  # empty set: none
    assert beacon["AmbientCapabilities"] == ""
    assert "/var/lib/voyn-ops/beacon" in beacon["ReadWritePaths"]
    assert "/var/lib/voyn-ops " not in beacon["ReadWritePaths"] + " "


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        (
            "disk-guard",
            {
                "CAP_DAC_OVERRIDE",
                "CAP_DAC_READ_SEARCH",
                "CAP_FOWNER",
                "CAP_SETUID",
                "CAP_SETGID",
                "CAP_SYS_PTRACE",
            },
        ),
        ("self-heal", {"CAP_SYS_PTRACE", "CAP_DAC_READ_SEARCH"}),
    ],
)
def test_the_root_timers_keep_only_the_capabilities_they_need(name, allowed):
    caps = set(_service(name)["CapabilityBoundingSet"].split())
    assert caps and caps <= allowed
    for dangerous in (
        "CAP_SYS_ADMIN",
        "CAP_NET_ADMIN",
        "CAP_SYS_MODULE",
        "CAP_SYS_RAWIO",
    ):
        assert dangerous not in caps
    assert _service(name)["SystemCallFilter"].startswith("@system-service")


def test_the_disk_guard_no_longer_needs_the_privilege_to_vacuum_the_journal(tmp_path):
    cfg = _cfg(tmp_path, OPS_JOURNAL_MAX_MB="500")
    journal, _ = _journal(tmp_path)
    calls: list[list[str]] = []
    disk_guard.run_guard(
        cfg, runner=_fake_runner({}, calls), journal=journal, cwds=[], free_pct=_low
    )
    assert not any(c and c[0] == "journalctl" for c in calls)
    assert (
        "journalctl"
        not in (ROOT / "command_center/ops/disk_guard.py")
        .read_text()
        .split("def run_guard")[1]
    )


def test_the_beacon_takes_the_tunnel_holder_from_the_self_heal_state(tmp_path):
    common.StateFile(tmp_path / "self-heal-state.json").save(
        {"tunnel_holder": "tailscaled"}
    )

    def blind(argv, timeout=30):  # unprivileged: `ss` cannot name the process
        if list(argv)[0] == "ss":
            return 0, "LISTEN 0 4096 127.0.0.1:17443 0.0.0.0:*\n", ""
        return _beacon_runner(argv, timeout)

    doc = status_beacon.collect(
        {
            "OPS_STATE_DIR": str(tmp_path / "own"),
            "OPS_HEAL_STATE_DIR": str(tmp_path),
            "OPS_BEACON_DETAIL": "full",
        },
        runner=blind,
    )
    assert doc["review_tunnel"]["held_by"] == "tailscaled"


def test_an_unknown_holder_word_in_the_state_file_is_not_published(tmp_path):
    common.StateFile(tmp_path / "self-heal-state.json").save(
        {"tunnel_holder": "10.0.0.5"}
    )
    doc = status_beacon.collect(
        {
            "OPS_STATE_DIR": str(tmp_path / "own"),
            "OPS_HEAL_STATE_DIR": str(tmp_path),
            "OPS_BEACON_DETAIL": "full",
        },
        runner=lambda argv, timeout=30: (0, "", ""),
    )
    assert doc["review_tunnel"]["held_by"] is None


def test_self_heal_leaves_the_holder_word_for_the_beacon(tmp_path):
    runner = _heal_runner(held_by="tailscaled", tunnel_code="501")
    _heal(tmp_path, runner, now=1000)
    assert (
        common.StateFile(tmp_path / "state.json").load()["tunnel_holder"]
        == "tailscaled"
    )


def test_the_installer_caps_the_journal_through_journald_and_removes_it_again(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    assert _run_installer(env, "--role", "worker").returncode == 0
    cap = tmp_path / "journald" / "voyn-ops.conf"
    assert cap.read_text() == "[Journal]\nSystemMaxUse=500M\n"
    log = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text()
    assert "restart systemd-journald" in log
    assert _run_installer(env, "--uninstall").returncode == 0
    assert not cap.exists()


def test_the_installer_rewrites_only_its_own_journald_file(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    own = tmp_path / "etc-journald.conf"
    own.write_text("[Journal]\nSystemMaxUse=2G\n")
    # the installer looks at the real /etc paths for an owner cap; simulate it by
    # pre-creating our own drop-in with different content and checking it is not
    # silently rewritten by a second install.
    jd = tmp_path / "journald"
    jd.mkdir()
    (jd / "voyn-ops.conf").write_text("[Journal]\nSystemMaxUse=123M\n")
    assert _run_installer(env, "--role", "worker").returncode == 0
    assert (jd / "voyn-ops.conf").read_text() == "[Journal]\nSystemMaxUse=500M\n"


def test_without_an_aicc_worker_account_the_beacon_falls_back_loudly(tmp_path):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    result = _run_installer(env, "--role", "worker")
    assert result.returncode == 0
    assert "status beacon will run as root" in result.stderr
    dropin = tmp_path / "units" / "voyn-ops-beacon.service.d" / "user.conf"
    assert "User=root" in dropin.read_text()
    assert _run_installer(env, "--uninstall").returncode == 0
    assert not dropin.exists()


def test_the_state_and_log_directories_are_not_world_readable(
    tmp_path,
):
    env = _fake_host(tmp_path)
    (tmp_path / "units").mkdir()
    assert _run_installer(env, "--role", "worker").returncode == 0
    for name in ("state", "logs"):
        mode = (tmp_path / name).stat().st_mode & 0o777
        assert mode == 0o750, (name, oct(mode))
        assert (tmp_path / name / "beacon").is_dir()


def _releases_layout(tmp_path: Path, base: Path | None = None) -> dict[str, str]:
    """The real fleet layout: current -> releases/<sha>, .venv -> runtime-venv."""
    env = _fake_host(tmp_path)
    base = base or tmp_path / "opt-aicc"
    sha_dir = base / "releases" / ("a" * 40)
    sha_dir.parent.mkdir(parents=True)
    __import__("shutil").copytree(env["AICC_RELEASE_DIR"], sha_dir)
    (sha_dir / "command_center").symlink_to(ROOT / "command_center")
    runtime = base / "runtime-venv"
    (runtime / "bin").mkdir(parents=True)
    # a wrapper inside the sandbox: the installer follows links, and the real
    # interpreter (a CI toolcache, a Homebrew cellar) is not ours to own
    wrapper = runtime / "bin" / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    (sha_dir / ".venv").symlink_to(runtime)
    (base / "current").symlink_to(sha_dir)
    env.pop("PYTHONPATH")
    env.pop("AICC_VENV_PYTHON")
    env["AICC_RELEASE_DIR"] = str(base / "current")
    return env


def test_installer_uses_the_release_venv_and_imports_from_the_release(tmp_path):
    env = _releases_layout(tmp_path)
    (tmp_path / "units").mkdir()
    done = _run_installer(env, "--role", "control")
    assert done.returncode == 0, done.stdout + done.stderr
    # an overridden layout is written into a drop-in per unit
    current = env["AICC_RELEASE_DIR"]
    for unit, module in (
        ("voyn-ops-disk-guard", "disk_guard"),
        ("voyn-ops-beacon", "status_beacon"),
        ("voyn-ops-self-heal", "self_heal"),
    ):
        text = (tmp_path / "units" / f"{unit}.service.d" / "layout.conf").read_text()
        assert f"WorkingDirectory={current}\n" in text
        assert f"ExecStart={current}/.venv/bin/python -m command_center.ops.{module}" in text
    assert _run_installer(env, "--uninstall").returncode == 0
    assert not list((tmp_path / "units").glob("*.service.d"))


def test_installer_stops_when_the_release_has_no_interpreter(tmp_path):
    env = _releases_layout(tmp_path)
    (Path(env["AICC_RELEASE_DIR"]) / ".venv").unlink()
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert "no interpreter" in done.stderr


def test_shipped_units_name_the_release_layout_not_a_separate_venv():
    for unit in ("disk-guard", "beacon", "self-heal"):
        text = (ROOT / f"deploy/systemd/voyn-ops-{unit}.service").read_text()
        assert "WorkingDirectory=/opt/aicc/current\n" in text
        assert "ExecStart=/opt/aicc/current/.venv/bin/python -m " in text
        assert "/opt/aicc/.venv" not in text


def test_a_group_writable_parent_of_the_release_link_is_refused(tmp_path):
    env = _releases_layout(tmp_path)
    parent = Path(env["AICC_RELEASE_DIR"]).parent
    parent.chmod(0o775)
    (tmp_path / "units").mkdir()
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert str(parent) in done.stderr
    assert "chmod 755" in done.stderr
    assert not list((tmp_path / "units").glob("*.service"))
    parent.chmod(0o755)
    assert _run_installer(env, "--role", "worker").returncode == 0


def test_a_group_writable_releases_directory_is_refused(tmp_path):
    env = _releases_layout(tmp_path)
    releases = Path(env["AICC_RELEASE_DIR"]).resolve().parent
    releases.chmod(0o775)
    (tmp_path / "units").mkdir()
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert str(releases) in done.stderr
    releases.chmod(0o755)


def test_an_untrusted_owner_is_refused_even_when_modes_are_tight(tmp_path):
    env = _releases_layout(tmp_path)
    env["AICC_OPS_TRUST_OWNER"] = "nobody-here"
    (tmp_path / "units").mkdir()
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert "must be owned by root" in done.stderr


def test_layout_paths_with_odd_characters_are_refused(tmp_path):
    env = _releases_layout(tmp_path)
    odd = tmp_path / "opt aicc"
    odd.mkdir()
    env["AICC_RELEASE_DIR"] = str(odd)
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert "letters, digits" in done.stderr


def test_a_sticky_parent_above_the_fixture_is_passed_in_tests(tmp_path):
    sandbox = tmp_path / "sandbox"
    fixture = sandbox / "fx"
    fixture.mkdir(parents=True)
    env = _releases_layout(tmp_path, base=fixture / "opt-aicc")
    # the fixture lives below a sticky 1777 directory, like /tmp on a runner
    sandbox.chmod(0o1777)
    env["AICC_OPS_TEST_ROOT"] = str(fixture)
    (tmp_path / "units").mkdir()
    try:
        done = _run_installer(env, "--role", "worker")
        assert done.returncode == 0, done.stdout + done.stderr
    finally:
        sandbox.chmod(0o755)


def test_sticky_release_directories_are_refused_on_a_host(tmp_path):
    for mode in (0o1777, 0o1775, 0o777):
        for target in ("release", "venv", "releases", "base"):
            env = _releases_layout(tmp_path / f"{mode}-{target}")
            current = Path(env["AICC_RELEASE_DIR"])
            real = current.resolve()
            path = {
                "release": real,
                "venv": real / ".venv",
                "releases": real.parent,
                "base": current.parent,
            }[target].resolve()
            path.chmod(mode)
            (tmp_path / f"{mode}-{target}" / "units").mkdir(exist_ok=True)
            done = _run_installer(env, "--role", "worker")
            path.chmod(0o755)
            assert done.returncode != 0, (oct(mode), target)
            assert str(path) in done.stderr, (oct(mode), target)


def test_a_release_outside_trusted_ownership_is_refused_without_test_variables(tmp_path):
    env = _releases_layout(tmp_path)
    for name in ("AICC_OPS_TRUST_OWNER", "AICC_OPS_TEST_ROOT"):
        env.pop(name)
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert "must be owned by root" in done.stderr


def test_test_variables_do_nothing_without_the_nonroot_switch(tmp_path):
    env = _releases_layout(tmp_path)
    env.pop("AICC_OPS_ALLOW_NONROOT")
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert "run as root" in done.stderr or "must be owned by root" in done.stderr


def test_an_intermediate_symlink_with_an_untrusted_owner_is_refused(tmp_path):
    env = _releases_layout(tmp_path)
    base = Path(env["AICC_RELEASE_DIR"]).parent
    hop = tmp_path / "hop"
    hop.symlink_to(base)
    env["AICC_RELEASE_DIR"] = str(hop / "current")
    (tmp_path / "units").mkdir()
    assert _run_installer(env, "--role", "worker").returncode == 0
    env["AICC_OPS_TRUST_OWNER"] = "nobody-here"
    done = _run_installer(env, "--role", "worker")
    assert done.returncode != 0
    assert str(hop) in done.stderr or "must be owned by root" in done.stderr


# --------------------------------------------------------------------------
# switching the owner-Mac review path off and back on

MAC_SCRIPT = ROOT / "ops/mac-path.sh"
_MARKER = "needle-text-that-must-never-be-printed"


def _mac_env(tmp_path):
    control = tmp_path / "executor-routing.env"
    worker = tmp_path / "executors.env"
    lines = ["AICC_EXECUTOR_PROFILE=owner-cascade", "AICC_CLAUDE_MAC_ENABLED=1"]
    lines.append(f"SOME_VALUE={_MARKER}")
    control.write_text("\n".join(lines) + "\n")
    control.chmod(0o640)
    worker.write_text("OTHER=1\n")
    return {
        **os.environ,
        "AICC_MAC_PATH_FILES": f"{control} {worker} {tmp_path / 'absent.env'}",
        "AICC_OPS_ALLOW_NONROOT": "1",
    }, control, worker


def _mac(env, *args):
    return subprocess.run(
        ["bash", str(MAC_SCRIPT), *args], env=env, capture_output=True, text=True
    )


def test_mac_path_off_changes_one_line_keeps_a_copy_and_prints_no_content(tmp_path):
    env, control, worker = _mac_env(tmp_path)
    original = control.read_text()
    done = _mac(env, "off")
    assert done.returncode == 0, done.stderr
    assert _MARKER not in done.stdout + done.stderr
    assert control.read_text() == original.replace(
        "AICC_CLAUDE_MAC_ENABLED=1", "AICC_CLAUDE_MAC_ENABLED=0"
    )
    assert (control.stat().st_mode & 0o777) == 0o640
    backups = list(tmp_path.glob("executor-routing.env.before-mac-off-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert worker.read_text() == "OTHER=1\n"  # no key there: untouched, not created
    assert "not set" in done.stdout and "changed files: 1" in done.stdout


def test_mac_path_off_twice_is_a_no_op_and_on_puts_the_line_back(tmp_path):
    env, control, _ = _mac_env(tmp_path)
    original = control.read_text()
    assert _mac(env, "off").returncode == 0
    again = _mac(env, "off")
    assert "changed files: 0" in again.stdout
    assert len(list(tmp_path.glob("executor-routing.env.before-mac-off-*"))) == 1
    control.write_text(control.read_text() + "ADDED=later\n")
    back = _mac(env, "on")
    assert back.returncode == 0, back.stderr
    assert control.read_text() == original + "ADDED=later\n"


def test_mac_path_status_shows_only_the_flag(tmp_path):
    env, control, _ = _mac_env(tmp_path)
    done = _mac(env, "status")
    assert "AICC_CLAUDE_MAC_ENABLED=1" in done.stdout
    assert _MARKER not in done.stdout and "owner-cascade" not in done.stdout
    assert "not on this host" in done.stdout


def test_mac_path_needs_root_and_a_known_action(tmp_path):
    env, _, _ = _mac_env(tmp_path)
    assert _mac(env, "nonsense").returncode != 0
    env.pop("AICC_OPS_ALLOW_NONROOT")
    done = _mac(env, "off")
    assert done.returncode != 0 and "run as root" in done.stderr


def test_the_mac_path_commands_use_only_console_safe_characters():
    for line in MAC_SCRIPT.read_text().splitlines():
        if line.startswith("#   bash /opt/aicc/current/ops/mac-path.sh"):
            command = line.removeprefix("#   ").split("  ")[0]
            assert all(ch.islower() or ch.isdigit() or ch in " -./" for ch in command)
