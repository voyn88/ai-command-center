from __future__ import annotations

import json
import subprocess
from pathlib import Path

from command_center.ops import source_clone_refresh


def _git(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], capture_output=True, text=True)


def _repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "one",
        ],
        check=True,
    )
    return _git("-C", str(path), "rev-parse", "HEAD").stdout.strip()


def test_refresh_fast_forwards_the_source_clone(tmp_path) -> None:
    origin = tmp_path / "origin"
    source = tmp_path / "source"
    _repo(origin)
    assert _git("clone", "-q", str(origin), str(source)).returncode == 0
    old_head = _git("-C", str(source), "rev-parse", "HEAD").stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "two",
        ],
        check=True,
    )
    new_head = _git("-C", str(origin), "rev-parse", "HEAD").stdout.strip()
    assert old_head != new_head

    result = source_clone_refresh.refresh_source_clone(source)

    assert result.ok
    assert result.head == new_head
    assert result.upstream == "origin/main"
    assert _git("-C", str(source), "rev-parse", "HEAD").stdout.strip() == new_head


def test_refresh_stores_pr_head_as_a_reachable_local_ref(tmp_path) -> None:
    origin = tmp_path / "origin"
    source = tmp_path / "source"
    _repo(origin)
    assert _git("clone", "-q", str(origin), str(source)).returncode == 0
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "pr",
        ],
        check=True,
    )
    pin = _git("-C", str(origin), "rev-parse", "HEAD").stdout.strip()
    assert (
        _git("-C", str(origin), "update-ref", "refs/pull/17/head", pin).returncode == 0
    )
    assert _git("-C", str(origin), "reset", "-q", "--hard", "HEAD~1").returncode == 0

    result = source_clone_refresh.refresh_source_clone(source, pr_number="17")

    assert result.ok
    assert (
        _git(
            "-C", str(source), "rev-parse", "refs/remotes/origin/pr/17/head"
        ).stdout.strip()
        == pin
    )


def test_refresh_skips_fetch_when_the_clone_is_read_only(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    head = _repo(source)
    real_git = source_clone_refresh._git

    def fake_git(repo, args, *, timeout=120):
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                stdout="",
                stderr="error: cannot open '.git/FETCH_HEAD': Read-only file system\n",
            )
        return real_git(repo, args, timeout=timeout)

    monkeypatch.setattr(source_clone_refresh, "_git", fake_git)

    result = source_clone_refresh.refresh_source_clone(source)

    assert result.ok
    assert result.head == head
    assert result.error is None


def test_read_only_refresh_fails_closed_when_the_pr_ref_is_absent(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "source"
    _repo(source)
    real_git = source_clone_refresh._git

    def fake_git(repo, args, *, timeout=120):
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                stdout="",
                stderr="error: cannot open '.git/FETCH_HEAD': Read-only file system\n",
            )
        return real_git(repo, args, timeout=timeout)

    monkeypatch.setattr(source_clone_refresh, "_git", fake_git)

    result = source_clone_refresh.refresh_source_clone(source, pr_number="17")

    assert not result.ok
    assert result.error is not None
    assert "read-only and PR ref is absent" in result.error


def test_cli_reports_json_and_nonzero_on_failure(tmp_path, capsys) -> None:
    """A clone that is PRESENT and cannot be refreshed is still a failure:
    that is a fault the tick measures, and the exit code must carry it."""
    broken = tmp_path / "not-a-clone"
    broken.mkdir()

    code = source_clone_refresh.main(["--repo", str(broken)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["skipped"] is False
    assert payload["path"] == str(broken.resolve())
    assert "fetch failed" in payload["error"]


# ---------------------------------------------------------------------------
# VOYN-MON-WORKER-01-INFRA-FAILED-UNITS (monitor_finding 2408).
#
# The refresh unit is a `Type=oneshot` tick that runs every two minutes, and
# one of the clones it refreshes is one the fleet deliberately runs hosts
# without. Asking for it unconditionally ended the unit `failed` on exactly
# those hosts -- permanently, because nothing reaps a failed unit and the
# next tick fails identically -- and the host unit-health probe counts units
# that are failed NOW. That is a standing `failed_units` finding no repair of
# the launcher (2051, 2295) could ever clear.
# ---------------------------------------------------------------------------


def _unit(name: str) -> str:
    return Path("deploy/systemd", name).read_text()


def _missing_tolerant(text: str, directive: str) -> set[str]:
    """Paths a unit declares with systemd's missing-tolerant `-` prefix."""
    tolerated: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == directive:
            tolerated.update(
                token[1:] for token in value.split() if token.startswith("-")
            )
    return tolerated


def _refreshed_clones(text: str) -> set[str]:
    """Clones the refresh unit's ExecStart lines hand to `--repo`."""
    clones: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        tokens = line.split()
        clones.update(
            argument for flag, argument in zip(tokens, tokens[1:]) if flag == "--repo"
        )
    return clones


def test_a_clone_the_host_does_not_have_is_a_skip_not_a_failed_unit(
    tmp_path, capsys
) -> None:
    """The tick cannot create a clone, so an absent one is not its failure.

    Before this, the absent clone exited 1, the oneshot unit ended `failed`,
    and it stayed failed until the next tick failed the same way -- host
    state the unit-health probe reads as broken infrastructure.
    """
    absent = tmp_path / "not-on-this-host"

    code = source_clone_refresh.main(["--repo", str(absent)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["skipped"] is True
    assert payload["path"] == str(absent.resolve())


def test_the_refresh_unit_refreshes_a_clone_the_fleet_declares_optional(
    tmp_path,
) -> None:
    """The two shipped files must not contradict each other.

    The worker drop-in binds a clone with the missing-tolerant `-` so that a
    host without it still serves ai-command-center tasks, the refresh unit
    repeats that `-` in its own ReadWritePaths -- and then refreshes the same
    clone as if it were required. This asserts the overlap exists (so the
    check cannot pass vacuously) and that the tick tolerates it being absent.
    """
    optional = _missing_tolerant(
        _unit("voyn-aicc-worker-principal-isolation.conf"), "BindReadOnlyPaths"
    )
    refresh_unit = _unit("voyn-aicc-source-clone-refresh.service")
    refreshed = _refreshed_clones(refresh_unit)

    tolerated_and_refreshed = optional & refreshed
    assert tolerated_and_refreshed, (
        "no refreshed clone is declared missing-tolerant: this check would "
        "pass without exercising the shape it exists for"
    )
    assert tolerated_and_refreshed <= _missing_tolerant(
        refresh_unit, "ReadWritePaths"
    ), "the refresh unit must repeat the drop-in's missing-tolerant declaration"

    for clone in sorted(tolerated_and_refreshed):
        stand_in = tmp_path / Path(clone).name
        assert source_clone_refresh.refresh_tick(stand_in).skipped
        assert source_clone_refresh.main(["--repo", str(stand_in)]) == 0


def test_the_worker_review_path_still_fails_on_an_absent_clone(tmp_path) -> None:
    """Only the periodic tick skips. The worker refreshes the repository a
    task pins before an isolated review clones from it; absent there means
    the review would run against nothing, and the attempt must fail."""
    absent = tmp_path / "missing"

    result = source_clone_refresh.refresh_source_clone(absent)

    assert not result.ok
    assert result.skipped is False
    assert result.error == "source clone is absent"
