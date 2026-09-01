"""Integration + targeted regression tests for scripts/prove_regression.py.

Each test below is paired with the specific false-confirmation this tool was
rejected for across five review rounds; see the module docstring of
prove_regression.py for the full defect list. A test here must fail if its
paired defect is reintroduced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import prove_regression
from scripts.prove_regression import CaseResult, RunResult


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# --------------------------------------------------------------------------
# Happy path: also proves no test-support transplant is needed at all, since
# the base commit here has neither a test file nor a conftest.py — only the
# implementation. If the tool ever needed to check out base's test infra
# (the round-3/round-4 defect shape) this would fail outright.
# --------------------------------------------------------------------------


def test_confirms_a_genuine_regression_test(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy implementation, no tests yet")

    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(
        repo,
        "conftest.py",
        "import pytest\n\n@pytest.fixture\ndef multiplier():\n    return 2\n",
    )
    _write(
        repo,
        "test_impl.py",
        "from impl import compute\n\n"
        "def test_compute(multiplier):\n"
        "    assert compute(3) == 3 * multiplier\n",
    )
    head = _commit(repo, "fix compute() and add the regression test + fixture")

    verdicts = prove_regression.check(repo, base, head, ["impl.py"], ["test_impl.py::test_compute"])
    assert len(verdicts) == 1
    assert verdicts[0].confirmed, verdicts[0].reason


def test_cli_exits_zero_when_confirmed(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy")
    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(
        repo,
        "test_impl.py",
        "from impl import compute\n\ndef test_compute():\n    assert compute(3) == 6\n",
    )
    head = _commit(repo, "fix + test")

    code = prove_regression.main(
        ["--repo", str(repo), "--base", base, "--head", head, "--fix", "impl.py", "test_impl.py::test_compute"]
    )
    assert code == 0


# --------------------------------------------------------------------------
# Round 1: two node IDs must never be graded as one aggregate result. A weak
# test that passes regardless of the defect must be rejected on its own,
# even sitting next to a strong test that correctly fails on base.
# --------------------------------------------------------------------------


def test_two_node_ids_are_judged_independently_not_aggregated(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy")

    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(
        repo,
        "test_impl.py",
        "from impl import compute\n\n"
        "def test_compute_strong():\n"
        "    assert compute(3) == 6\n\n"
        "def test_compute_weak():\n"
        "    assert 1 + 1 == 2\n",
    )
    head = _commit(repo, "fix + one real test + one test that ignores the defect entirely")

    verdicts = prove_regression.check(
        repo,
        base,
        head,
        ["impl.py"],
        ["test_impl.py::test_compute_strong", "test_impl.py::test_compute_weak"],
    )
    by_id = {v.node_id: v for v in verdicts}
    assert by_id["test_impl.py::test_compute_strong"].confirmed
    assert not by_id["test_impl.py::test_compute_weak"].confirmed

    code = prove_regression.main(
        [
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--fix",
            "impl.py",
            "test_impl.py::test_compute_strong",
            "test_impl.py::test_compute_weak",
        ]
    )
    assert code != 0, "a weak test hiding behind a strong one must not let the whole run exit 0"


# --------------------------------------------------------------------------
# Round 2: the same flaw one level down — a single node ID that expands to
# several parametrized cases must have *every* collected case fail on base,
# not just one of them.
# --------------------------------------------------------------------------


def test_single_node_id_requires_every_parametrized_case_to_fail_on_base(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy")

    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(
        repo,
        "test_impl.py",
        "import pytest\nfrom impl import compute\n\n"
        '@pytest.mark.parametrize("x,expected", [(3, 6), (0, 0)])\n'
        "def test_compute(x, expected):\n"
        "    assert compute(x) == expected\n",
    )
    head = _commit(repo, "fix + a parametrized test where one case can't tell buggy from fixed")

    verdicts = prove_regression.check(repo, base, head, ["impl.py"], ["test_impl.py::test_compute"])
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert not verdict.confirmed, "case (0, 0) passes under both the bug and the fix and must block confirmation"
    assert "non-failing" in verdict.reason


# --------------------------------------------------------------------------
# Round 3: an unrelated collection/import error at base must never be read
# as the expected assertion failure.
# --------------------------------------------------------------------------


def test_unrelated_collection_error_at_base_is_not_confirmed(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy, and MARKER does not exist yet")

    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n\nMARKER = 'fixed'\n")
    _write(
        repo,
        "test_impl.py",
        "from impl import compute, MARKER\n\n"
        "def test_compute():\n"
        "    assert compute(3) == 6\n"
        "    assert MARKER == 'fixed'\n",
    )
    head = _commit(repo, "fix + test that also imports a symbol the base module lacks")

    verdicts = prove_regression.check(repo, base, head, ["impl.py"], ["test_impl.py::test_compute"])
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert not verdict.confirmed
    assert "assertion failure" in verdict.reason or "no test cases" in verdict.reason


# --------------------------------------------------------------------------
# Round 4a: --fix must never resolve through a symlink, in either direction
# (restoring to head or reverting to base) — a symlinked fix path must be
# rejected outright rather than writing through it.
# --------------------------------------------------------------------------


def test_symlinked_fix_path_is_rejected_without_touching_its_target(tmp_path):
    outside_target = tmp_path / "outside_target.txt"
    outside_target.write_text("original")

    repo = _init_repo(tmp_path)
    (repo / "impl.py").symlink_to(outside_target)
    base = _commit(repo, "impl.py is a symlink at base too")

    _write(
        repo,
        "test_impl.py",
        "def test_noop():\n    assert True\n",
    )
    # Re-create the symlink after _write's parent.mkdir touched nothing else;
    # commit it again in case git normalized anything on checkout.
    head = _commit(repo, "add a trivial test; impl.py is still a symlink")

    verdicts = prove_regression.check(repo, base, head, ["impl.py"], ["test_impl.py::test_noop"])
    assert len(verdicts) == 1
    assert not verdicts[0].confirmed
    assert "rejected" in verdicts[0].reason
    assert outside_target.read_text() == "original"


# --------------------------------------------------------------------------
# Round 4b (path traversal) + unit coverage for _safe_target directly.
# --------------------------------------------------------------------------


def test_safe_target_rejects_absolute_path(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with pytest.raises(prove_regression.UnsafePathError):
        prove_regression._safe_target(worktree, "/etc/passwd")


def test_safe_target_rejects_parent_traversal(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with pytest.raises(prove_regression.UnsafePathError):
        prove_regression._safe_target(worktree, "../outside.txt")
    with pytest.raises(prove_regression.UnsafePathError):
        prove_regression._safe_target(worktree, "a/../../outside.txt")


def test_safe_target_rejects_symlink(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    (worktree / "impl.py").symlink_to(outside)
    with pytest.raises(prove_regression.UnsafePathError):
        prove_regression._safe_target(worktree, "impl.py")
    assert outside.read_text() == "do not touch"


def test_safe_target_accepts_a_plain_relative_path(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    target = prove_regression._safe_target(worktree, "src/impl.py")
    assert target == worktree / "src" / "impl.py"


def test_check_rejects_path_traversal_fix_argument_end_to_end(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("untouched")

    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy")
    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(repo, "test_impl.py", "from impl import compute\n\ndef test_compute():\n    assert compute(3) == 6\n")
    head = _commit(repo, "fix + test")

    verdicts = prove_regression.check(repo, base, head, ["../sentinel.txt"], ["test_impl.py::test_compute"])
    assert len(verdicts) == 1
    assert not verdicts[0].confirmed
    assert "rejected" in verdicts[0].reason
    assert sentinel.read_text() == "untouched"


# --------------------------------------------------------------------------
# Round 4c: base/head must collect the exact same cases with the exact same
# multiplicity. A frozenset comparison would let a duplicated/dropped case
# slip through unnoticed; exercised directly against judge_node so the
# comparison that matters (the one actually used to grade a node) is what's
# under test, not a copy of it.
# --------------------------------------------------------------------------


def test_judge_node_rejects_case_multiplicity_mismatch(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    head_cases = (
        CaseResult("test_impl.py::test_a", "passed"),
        CaseResult("test_impl.py::test_a", "passed"),
    )
    base_cases = (CaseResult("test_impl.py::test_a", "failed"),)
    responses = iter([RunResult(0, head_cases, "", ""), RunResult(1, base_cases, "", "")])

    monkeypatch.setattr(prove_regression, "_run_pytest_node", lambda wt, node_id: next(responses))
    monkeypatch.setattr(prove_regression, "_restore_fix_paths", lambda *a, **k: None)
    monkeypatch.setattr(prove_regression, "_revert_fix_paths", lambda *a, **k: None)

    verdict = prove_regression.judge_node(worktree, "base", "head", ["impl.py"], "test_impl.py::test_a")
    assert not verdict.confirmed
    assert "different cases" in verdict.reason


# --------------------------------------------------------------------------
# Round 5a: judge_node must not ignore the pytest exit code. A base run
# whose one recorded case happens to say "failed" must still be rejected if
# the process itself reported a session-level failure (interrupted,
# internal error, usage error) rather than a clean "tests ran, some failed".
# --------------------------------------------------------------------------


def test_judge_node_rejects_base_run_with_non_assertion_exit_code(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    head_cases = (CaseResult("test_impl.py::test_a", "passed"),)
    base_cases = (CaseResult("test_impl.py::test_a", "failed"),)
    responses = iter([RunResult(0, head_cases, "", ""), RunResult(2, base_cases, "", "")])

    monkeypatch.setattr(prove_regression, "_run_pytest_node", lambda wt, node_id: next(responses))
    monkeypatch.setattr(prove_regression, "_restore_fix_paths", lambda *a, **k: None)
    monkeypatch.setattr(prove_regression, "_revert_fix_paths", lambda *a, **k: None)

    verdict = prove_regression.judge_node(worktree, "base", "head", ["impl.py"], "test_impl.py::test_a")
    assert not verdict.confirmed
    assert "exit code" in verdict.reason


def test_judge_node_rejects_head_run_with_nonzero_exit_code_even_if_cases_look_passed(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    head_cases = (CaseResult("test_impl.py::test_a", "passed"),)
    responses = iter([RunResult(2, head_cases, "", "")])

    monkeypatch.setattr(prove_regression, "_run_pytest_node", lambda wt, node_id: next(responses))
    monkeypatch.setattr(prove_regression, "_restore_fix_paths", lambda *a, **k: None)
    monkeypatch.setattr(prove_regression, "_revert_fix_paths", lambda *a, **k: None)

    verdict = prove_regression.judge_node(worktree, "base", "head", ["impl.py"], "test_impl.py::test_a")
    assert not verdict.confirmed
    assert "exit code" in verdict.reason


def test_judge_node_requires_at_least_one_collected_case(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo, "impl.py", "def compute(x):\n    return x\n")
    base = _commit(repo, "buggy")
    _write(repo, "impl.py", "def compute(x):\n    return x * 2\n")
    _write(repo, "test_impl.py", "from impl import compute\n\ndef test_compute():\n    assert compute(3) == 6\n")
    head = _commit(repo, "fix + test")

    verdicts = prove_regression.check(repo, base, head, ["impl.py"], ["test_impl.py::test_does_not_exist"])
    assert len(verdicts) == 1
    assert not verdicts[0].confirmed
