"""scripts/ci/prepush/leak_guard.sh (VOYN-OPS-PUBLIC-REPO-CLAUDE-MD-LEAK):
the real script against real throwaway git repos. The guard's contract is
deterministic: block agent-instruction files by name at any depth, block
ADDED lines carrying absolute home paths, never flag pre-existing content,
and a bypass is printed rather than silent."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "ci"
    / "prepush"
    / "leak_guard.sh"
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    """A repo with the guard installed at its canonical path, one base commit
    on main, and a feature branch checked out — the pre-push shape."""
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-b", "main", str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    target = work / "scripts" / "ci" / "prepush" / "leak_guard.sh"
    target.parent.mkdir(parents=True)
    target.write_text(SCRIPT.read_text())
    target.chmod(0o755)
    (work / "base.txt").write_text("clean\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    return work


def _guard(work, env_extra=None):
    import os

    env = dict(os.environ, VOYN_LEAK_GUARD_BASE="main")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "scripts/ci/prepush/leak_guard.sh"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_clean_commit_passes(repo):
    (repo / "ok.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "ok")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LEAK_GUARD: pass" in r.stdout


@pytest.mark.parametrize("name", ["CLAUDE.md", "nested/dir/CLAUDE.local.md"])
def test_agent_instruction_file_is_refused_at_any_depth(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file" in r.stdout
    assert name in r.stdout


def test_added_absolute_home_path_is_refused(repo):
    (repo / "doc.md").write_text("see /Users/someone/Projects/x for details\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo)
    assert r.returncode == 1
    assert "absolute home paths" in r.stdout


def test_staged_leak_is_caught_before_commit(repo):
    (repo / "CLAUDE.md").write_text("instructions\n")
    _git(repo, "add", "CLAUDE.md")
    r = _guard(repo)
    assert r.returncode == 1
    assert "agent-instruction file 'CLAUDE.md'" in r.stdout


def test_preexisting_home_path_lines_do_not_flag_adjacent_edits(repo):
    """Tracked files already carry historical /Users/ examples; the guard
    scans ADDED lines only, so editing next to one must stay green."""
    doc = repo / "doc.md"
    _git(repo, "checkout", "-q", "main")
    doc.write_text("historical /Users/example/path\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "historical")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "main")
    doc.write_text("historical /Users/example/path\na clean new line\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "adjacent edit")
    r = _guard(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_bypass_is_printed_never_silent(repo):
    (repo / "CLAUDE.md").write_text("instructions\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "leak")
    r = _guard(repo, {"VOYN_LEAK_GUARD": "off"})
    assert r.returncode == 0
    assert "bypassed" in r.stdout
