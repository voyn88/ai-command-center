"""Integration tests for scripts/prove_regression.py.

Every scenario here mirrors a specific defect an earlier version of the tool
was rejected for, and is built to actually exercise the real git + pytest
machinery rather than mock it -- a test that cannot see the command cannot
see its divergence.

The synthetic repo used below is built the way a real PR looks: the *base*
commit is the pre-fix state and does not contain the new/changed test at
all. Only the *head* commit adds the fix, the new tests, and the new
conftest fixture together. Earlier review rounds flagged fixtures that
committed both the buggy implementation and the new tests into the base
commit as hiding the exact defect being tested for; this fixture does not
do that.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prove_regression as pr  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


BASE_IMPL = """\
def compute(x):
    return x - 1  # bug: should add, not subtract
"""

HEAD_IMPL = """\
def compute(x):
    return x + 1


SENTINEL = "fixed"
"""

BASE_TEST = """\
def test_existing():
    assert True
"""

HEAD_TEST = """\
import pytest

from pkg.impl import compute


def test_existing():
    assert True


def test_regression_compute():
    assert compute(1) == 2


def test_weak():
    # Does not actually exercise compute() -- must not be confirmed.
    assert True


@pytest.mark.parametrize("x,expected", [(1, 2), (5, 4)])
def test_regression_partial(x, expected):
    # The second case (5, 4) happens to match the buggy base formula
    # (x - 1) too, so it passes at base even though it doesn't prove
    # anything about the fix. Only the first case is real evidence.
    assert compute(x) == expected


@pytest.fixture
def guarded_sentinel():
    from pkg.impl import SENTINEL
    return SENTINEL


def test_regression_needs_fixture(guarded_sentinel):
    assert guarded_sentinel == "fixed"


def test_regression_via_conftest_fixture(computed_value):
    assert computed_value == 4
"""

HEAD_CONFTEST = """\
import pytest

from pkg.impl import compute


@pytest.fixture
def computed_value():
    return compute(3)
"""


@dataclass
class Repo:
    path: Path
    base_sha: str
    head_sha: str


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Repo:
    root = tmp_path_factory.mktemp("prove_regression_repo")
    _git(root, "init", "-q")

    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "impl.py").write_text(BASE_IMPL)
    (root / "tests").mkdir()
    (root / "tests" / "test_impl.py").write_text(BASE_TEST)
    base_sha = _commit(root, "base: buggy compute()")

    (root / "pkg" / "impl.py").write_text(HEAD_IMPL)
    (root / "tests" / "test_impl.py").write_text(HEAD_TEST)
    (root / "tests" / "conftest.py").write_text(HEAD_CONFTEST)
    head_sha = _commit(root, "head: fix compute() and add regression tests")

    return Repo(path=root, base_sha=base_sha, head_sha=head_sha)


def _check(repo: Repo, node_ids: list[str]) -> dict[str, pr.NodeVerdict]:
    return pr.check(
        repo.path,
        base_sha=repo.base_sha,
        head_sha=repo.head_sha,
        impl_paths=["pkg/impl.py"],
        node_ids=node_ids,
    )


def test_confirms_a_real_regression_test(repo):
    verdicts = _check(repo, ["tests/test_impl.py::test_regression_compute"])
    verdict = verdicts["tests/test_impl.py::test_regression_compute"]
    assert verdict.confirmed, verdict.reason


def test_rejects_a_test_that_passes_even_on_the_defect(repo):
    verdicts = _check(repo, ["tests/test_impl.py::test_weak"])
    verdict = verdicts["tests/test_impl.py::test_weak"]
    assert not verdict.confirmed
    assert "passed" in verdict.reason


def test_rejects_parametrized_node_id_when_only_some_cases_catch_the_defect(repo):
    verdicts = _check(repo, ["tests/test_impl.py::test_regression_partial"])
    verdict = verdicts["tests/test_impl.py::test_regression_partial"]
    assert not verdict.confirmed
    assert "not every collected case failed at base" in verdict.reason


def test_multiple_node_ids_are_judged_independently(repo):
    verdicts = _check(
        repo,
        [
            "tests/test_impl.py::test_regression_compute",
            "tests/test_impl.py::test_weak",
        ],
    )
    assert verdicts["tests/test_impl.py::test_regression_compute"].confirmed
    assert not verdicts["tests/test_impl.py::test_weak"].confirmed


def test_rejects_when_base_run_errors_instead_of_failing(repo):
    verdicts = _check(repo, ["tests/test_impl.py::test_regression_needs_fixture"])
    verdict = verdicts["tests/test_impl.py::test_regression_needs_fixture"]
    assert not verdict.confirmed
    assert "error" in verdict.reason


def test_confirms_regression_that_depends_on_a_new_conftest_fixture(repo):
    """The fixture used by this test lives in conftest.py, which was added
    at head and is not in --impl. If the tool only transplanted the single
    test file (the defect from an earlier review round), the base run would
    error with "fixture 'computed_value' not found" instead of genuinely
    failing the assertion, and this real regression test would be wrongly
    rejected.
    """
    verdicts = _check(repo, ["tests/test_impl.py::test_regression_via_conftest_fixture"])
    verdict = verdicts["tests/test_impl.py::test_regression_via_conftest_fixture"]
    assert verdict.confirmed, verdict.reason


def test_cli_exit_code_reflects_all_node_ids(repo, capsys):
    exit_code = pr.main(
        [
            "--repo", str(repo.path),
            "--base", repo.base_sha,
            "--head", repo.head_sha,
            "--impl", "pkg/impl.py",
            "tests/test_impl.py::test_regression_compute",
            "tests/test_impl.py::test_weak",
        ]
    )
    assert exit_code == 1
    out = capsys.readouterr().out
    assert '"confirmed": true' in out
    assert '"confirmed": false' in out


def test_cli_exit_code_zero_when_all_confirmed(repo, capsys):
    exit_code = pr.main(
        [
            "--repo", str(repo.path),
            "--base", repo.base_sha,
            "--head", repo.head_sha,
            "--impl", "pkg/impl.py",
            "tests/test_impl.py::test_regression_compute",
        ]
    )
    assert exit_code == 0


# --- Pure unit tests over judge_node(), independent of git/pytest ---------


def test_judge_node_rejects_duplicate_count_mismatch_a_frozenset_would_miss():
    base_run = pr.RunResult(
        node_id="t::x",
        exit_code=1,
        cases=(
            pr.CaseResult("t::x", pr.FAILED),
            pr.CaseResult("t::x", pr.FAILED),
        ),
        stdout="", stderr="",
    )
    head_run = pr.RunResult(
        node_id="t::x",
        exit_code=0,
        # Same unique id, but collected only once -- a frozenset comparison
        # of {"t::x"} == {"t::x"} would miss this and wrongly confirm.
        cases=(pr.CaseResult("t::x", pr.PASSED),),
        stdout="", stderr="",
    )
    verdict = pr.judge_node("t::x", base_run, head_run)
    assert not verdict.confirmed
    assert "differ" in verdict.reason


def test_judge_node_confirms_when_duplicate_counts_match_on_both_sides():
    base_run = pr.RunResult(
        node_id="t::x",
        exit_code=1,
        cases=(
            pr.CaseResult("t::x", pr.FAILED),
            pr.CaseResult("t::x", pr.FAILED),
        ),
        stdout="", stderr="",
    )
    head_run = pr.RunResult(
        node_id="t::x",
        exit_code=0,
        cases=(
            pr.CaseResult("t::x", pr.PASSED),
            pr.CaseResult("t::x", pr.PASSED),
        ),
        stdout="", stderr="",
    )
    verdict = pr.judge_node("t::x", base_run, head_run)
    assert verdict.confirmed, verdict.reason


def test_judge_node_rejects_missing_junit_report():
    base_run = pr.RunResult(node_id="t::x", exit_code=4, cases=(), stdout="", stderr="", junit_missing=True)
    head_run = pr.RunResult(node_id="t::x", exit_code=0, cases=(pr.CaseResult("t::x", pr.PASSED),), stdout="", stderr="")
    verdict = pr.judge_node("t::x", base_run, head_run)
    assert not verdict.confirmed
    assert "no JUnit report" in verdict.reason


def test_judge_node_treats_error_outcome_as_not_proof_even_though_pytest_exit_code_is_1():
    # pytest exits with code 1 for both assertion failures and unhandled
    # errors -- the verdict must be driven by the JUnit outcome, not the
    # process exit code.
    base_run = pr.RunResult(node_id="t::x", exit_code=1, cases=(pr.CaseResult("t::x", pr.ERROR),), stdout="", stderr="")
    head_run = pr.RunResult(node_id="t::x", exit_code=0, cases=(pr.CaseResult("t::x", pr.PASSED),), stdout="", stderr="")
    verdict = pr.judge_node("t::x", base_run, head_run)
    assert not verdict.confirmed
    assert "error" in verdict.reason
