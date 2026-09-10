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


def test_cli_reports_json_and_nonzero_on_failure(tmp_path, capsys) -> None:
    missing = tmp_path / "missing"

    code = source_clone_refresh.main(["--repo", str(missing)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["path"] == str(missing.resolve())
