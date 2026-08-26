"""`assert_test_catches_named_defect.py`, checked against the failure it exists to catch.

AICC #304's fourth round shipped two tests that passed against the very
defects they were named for — one asserted on substrings already present in
the unfixed file, the other bound only one side of a two-sided invariant.
Both were caught by hand: restore the pre-fix file, watch the test stay
green. This tool does that restoration mechanically, so every scenario below
is built the same way #304's review built its findings — a real git history
with a real bug at the base and a real fix at the head, not a description of
one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "assert_test_catches_named_defect.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
    return result


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--root", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_confirms_a_test_that_actually_fails_on_the_pre_fix_code(tmp_path) -> None:
    """The tool's own green path: a real regression test, on a real bug."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "production.py").write_text("def compute(a, b):\n    return a - b\n", encoding="utf-8")
    base = _commit(repo, "buggy: compute subtracts")

    (repo / "production.py").write_text("def compute(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_production.py").write_text(
        "from production import compute\n\n\ndef test_compute_adds():\n    assert compute(2, 3) == 5\n",
        encoding="utf-8",
    )
    _commit(repo, "fix: compute adds")

    result = _run(repo, "test_production.py::test_compute_adds", "--base", base)
    assert result.returncode == 0, result.stdout
    assert "confirmed" in result.stdout


def test_refuses_to_confirm_a_weak_test_bundled_with_a_strong_one(tmp_path) -> None:
    """A PR #409 review finding: multiple node ids were judged as one
    aggregate pytest outcome. Bundled with a real regression test that fails
    at base, a weak test that still passes at base made the combined summary
    report "at least one failed" and the tool printed `confirmed` for both.
    Each node id must be proven independently — a strong companion cannot
    vouch for a weak one.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "production.py").write_text("def compute(a, b):\n    return a - b\n", encoding="utf-8")
    base = _commit(repo, "buggy: compute subtracts")

    (repo / "production.py").write_text("def compute(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_production.py").write_text(
        "from production import compute\n\n\n"
        "def test_compute_adds():\n    assert compute(2, 3) == 5\n\n\n"
        "def test_compute_is_not_absurd():\n    assert compute(2, 3) < 100\n",
        encoding="utf-8",
    )
    _commit(repo, "fix: compute adds, one strong test and one too loose to notice")

    result = _run(
        repo,
        "test_production.py::test_compute_adds",
        "test_production.py::test_compute_is_not_absurd",
        "--base",
        base,
    )
    assert result.returncode != 0, result.stdout
    assert "confirmed" not in result.stdout
    assert "test_compute_is_not_absurd still passed" in result.stdout


def test_flags_a_test_that_passes_against_the_defect_it_is_named_for(tmp_path) -> None:
    """The AICC #304 round-four shape: a test that never exercises the fix.

    Same base bug as above, but the "regression" test asserts a bound so loose
    that the buggy subtraction satisfies it too. This is exactly the failure
    mode the tool exists to name: a green test that proves nothing about the
    line it is supposed to guard.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "production.py").write_text("def compute(a, b):\n    return a - b\n", encoding="utf-8")
    base = _commit(repo, "buggy: compute subtracts")

    (repo / "production.py").write_text("def compute(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_production.py").write_text(
        "from production import compute\n\n\n"
        "def test_compute_is_not_absurd():\n    assert compute(2, 3) < 100\n",
        encoding="utf-8",
    )
    _commit(repo, "fix: compute adds, with a test too loose to notice")

    result = _run(repo, "test_production.py::test_compute_is_not_absurd", "--base", base)
    assert result.returncode != 0, result.stdout
    assert "still passed" in result.stdout
    assert "AICC #304" in result.stdout


def test_refuses_a_named_test_that_does_not_even_pass_at_head(tmp_path) -> None:
    """A test that cannot pass now is not evidence of anything yet — checked
    before the worktree is built, so a broken test fails fast rather than
    reporting a confusing result from the base tree."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "production.py").write_text("def compute(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_production.py").write_text(
        "from production import compute\n\n\ndef test_compute_adds():\n    assert compute(2, 3) == 999\n",
        encoding="utf-8",
    )
    base = _commit(repo, "already broken")

    result = _run(repo, "test_production.py::test_compute_adds", "--base", base)
    assert result.returncode != 0
    assert "does not pass at the current head" in result.stdout


def test_also_carries_a_shared_fixture_so_the_base_run_fails_for_the_right_reason(tmp_path) -> None:
    """Without `--also`, a fixture the fix introduced is missing at the base
    tree and the run fails with a collection error, not the defect — a
    "confirmed" that would be true for the wrong reason. `--also` carries the
    shared file across so the base run exercises the actual bug.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "conftest.py").write_text("", encoding="utf-8")
    (repo / "production.py").write_text("def compute(a, b):\n    return a - b\n", encoding="utf-8")
    base = _commit(repo, "buggy: compute subtracts, no fixture yet")

    (repo / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef expected():\n    return 5\n", encoding="utf-8"
    )
    (repo / "production.py").write_text("def compute(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_production.py").write_text(
        "from production import compute\n\n\n"
        "def test_compute_adds(expected):\n    assert compute(2, 3) == expected\n",
        encoding="utf-8",
    )
    _commit(repo, "fix: compute adds, test needs a new fixture")

    without_also = _run(repo, "test_production.py::test_compute_adds", "--base", base)
    assert without_also.returncode != 0
    assert "no failed assertion" in without_also.stdout
    assert "fixture 'expected' not found" in without_also.stdout

    with_also = _run(
        repo,
        "test_production.py::test_compute_adds",
        "--base",
        base,
        "--also",
        "conftest.py",
    )
    assert with_also.returncode == 0, with_also.stdout
    assert "confirmed" in with_also.stdout
