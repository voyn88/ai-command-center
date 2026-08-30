from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prove_regression.py"

sys.path.insert(0, str(ROOT / "scripts"))
import prove_regression as pr  # noqa: E402


# ---------------------------------------------------------------------------
# Unit-level tests: parse_junit and NodeVerdict, no subprocesses involved.
# ---------------------------------------------------------------------------


def test_parse_junit_reads_status_per_case_not_the_summary_line():
    xml_text = """<?xml version="1.0"?>
    <testsuites>
      <testsuite>
        <testcase classname="pkg.test_mod" name="test_a"><failure>boom</failure></testcase>
        <testcase classname="pkg.test_mod" name="test_b"><error>boom</error></testcase>
        <testcase classname="pkg.test_mod" name="test_c"><skipped/></testcase>
        <testcase classname="pkg.test_mod" name="test_d"/>
      </testsuite>
    </testsuites>"""
    cases = pr.parse_junit(xml_text)
    statuses = {case.node_id: case.status for case in cases}
    assert statuses == {
        "pkg.test_mod::test_a": "failed",
        "pkg.test_mod::test_b": "error",
        "pkg.test_mod::test_c": "skipped",
        "pkg.test_mod::test_d": "passed",
    }


def test_parse_junit_rejects_invalid_xml():
    with pytest.raises(pr.ProofError):
        pr.parse_junit("not xml at all")


def _verdict(base_statuses, head_statuses, node_id="t.py::test_x"):
    base_cases = tuple(
        pr.CaseResult(node_id=f"{node_id}[{i}]", status=s)
        for i, s in enumerate(base_statuses)
    )
    head_cases = tuple(
        pr.CaseResult(node_id=f"{node_id}[{i}]", status=s)
        for i, s in enumerate(head_statuses)
    )
    return pr.NodeVerdict(node_id=node_id, base_cases=base_cases, head_cases=head_cases)


def test_node_verdict_confirms_only_when_all_die_at_base_and_all_pass_at_head():
    assert _verdict(["failed"], ["passed"]).confirmed is True
    assert _verdict(["error"], ["passed"]).confirmed is True


def test_node_verdict_rejects_one_survivor_among_several_base_failures():
    # This is the round-2 defect: a parametrized node id where one collected
    # case passes at the base commit while its siblings fail must not confirm.
    verdict = _verdict(["failed", "passed"], ["passed", "passed"])
    assert verdict.confirmed is False
    assert "did not fail in full" in verdict.reason


def test_node_verdict_rejects_error_that_also_survives_at_head():
    # This is the round-3 defect: an error at the base that is unrelated to
    # the defect (e.g. a broken fixture) reproduces at head too and must not
    # be read as proof.
    verdict = _verdict(["error"], ["error"])
    assert verdict.confirmed is False
    assert "does not pass cleanly" in verdict.reason


def test_node_verdict_rejects_mismatched_case_sets():
    verdict = pr.NodeVerdict(
        node_id="t.py::test_x",
        base_cases=(pr.CaseResult(node_id="t.py::test_x[0]", status="failed"),),
        head_cases=(
            pr.CaseResult(node_id="t.py::test_x[0]", status="passed"),
            pr.CaseResult(node_id="t.py::test_x[1]", status="passed"),
        ),
    )
    assert verdict.confirmed is False
    assert "not comparable" in verdict.reason


def test_node_verdict_rejects_empty_collection():
    assert _verdict([], []).confirmed is False


# ---------------------------------------------------------------------------
# Integration tests: real git repos, real pytest subprocesses, real CLI.
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    return completed


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_check(repo: Path, base_ref: str, *node_ids: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "check", base_ref, *node_ids],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_confirms_a_test_that_genuinely_fails_at_base_and_passes_at_head(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "mymodule.py", "def compute(x):\n    return x - 1\n")
    _write(
        repo / "test_mymodule.py",
        "from mymodule import compute\n\n"
        "def test_compute_adds_one():\n"
        "    assert compute(2) == 3\n",
    )
    base = _commit(repo, "base: buggy compute")

    _write(repo / "mymodule.py", "def compute(x):\n    return x + 1\n")

    result = _run_check(repo, base, "test_mymodule.py::test_compute_adds_one")
    assert result.returncode == 0, result.stderr
    assert "confirmed: `test_mymodule.py::test_compute_adds_one`" in result.stdout


def test_does_not_confirm_when_fix_is_missing(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "mymodule.py", "def compute(x):\n    return x - 1\n")
    _write(
        repo / "test_mymodule.py",
        "from mymodule import compute\n\n"
        "def test_compute_adds_one():\n"
        "    assert compute(2) == 3\n",
    )
    base = _commit(repo, "base: buggy compute")
    # No fix applied: the current tree is identical to the base commit.

    result = _run_check(repo, base, "test_mymodule.py::test_compute_adds_one")
    assert result.returncode == 1
    assert "does not pass cleanly against the current tree" in result.stderr


def test_two_node_ids_are_judged_independently_not_aggregated(tmp_path):
    # Round-1 regression: a strong, genuinely-confirming test must not cause
    # a weak, never-failing test to be reported as confirmed too.
    repo = _init_repo(tmp_path)
    _write(repo / "mymodule.py", "def compute(x):\n    return x - 1\n")
    _write(
        repo / "test_mymodule.py",
        "from mymodule import compute\n\n"
        "def test_compute_adds_one():\n"
        "    assert compute(2) == 3\n",
    )
    _write(
        repo / "test_other.py",
        "def test_always_passes():\n    assert True\n",
    )
    base = _commit(repo, "base: buggy compute, plus an unrelated always-true test")

    _write(repo / "mymodule.py", "def compute(x):\n    return x + 1\n")

    result = _run_check(
        repo,
        base,
        "test_mymodule.py::test_compute_adds_one",
        "test_other.py::test_always_passes",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "confirmed: `test_mymodule.py::test_compute_adds_one`" in result.stdout
    assert "NOT confirmed" in result.stderr
    assert "test_other.py::test_always_passes" in result.stderr


def test_single_node_id_with_one_surviving_parametrized_case_is_not_confirmed(tmp_path):
    # Round-2 regression: one node id can collect several parametrized cases;
    # one surviving case among several failures must sink the whole node id.
    repo = _init_repo(tmp_path)
    _write(
        repo / "clamp_mod.py",
        "def clamp(x, lo, hi):\n"
        "    if x < lo:\n"
        "        return lo\n"
        "    return x\n",  # bug: never clamps to hi
    )
    _write(
        repo / "test_clamp.py",
        "import pytest\n"
        "from clamp_mod import clamp\n\n"
        '@pytest.mark.parametrize("x,lo,hi,expected", [(-5, 0, 10, 0), (50, 0, 10, 10)])\n'
        "def test_clamp(x, lo, hi, expected):\n"
        "    assert clamp(x, lo, hi) == expected\n",
    )
    base = _commit(repo, "base: clamp forgets the upper bound")

    _write(
        repo / "clamp_mod.py",
        "def clamp(x, lo, hi):\n"
        "    if x < lo:\n"
        "        return lo\n"
        "    if x > hi:\n"
        "        return hi\n"
        "    return x\n",
    )

    result = _run_check(repo, base, "test_clamp.py::test_clamp")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "did not fail in full against the base commit" in result.stderr
    assert "test_clamp::test_clamp[-5-0-10-0] (passed)" in result.stderr


def test_confirms_a_test_file_that_does_not_exist_at_the_base_commit(tmp_path):
    # Round-3a regression: the pull request can add a brand-new regression
    # test. It must be transplanted onto the base commit's implementation
    # rather than refused for not existing there.
    repo = _init_repo(tmp_path)
    _write(
        repo / "feature.py",
        "def parse_amount(raw):\n    return float(raw)\n",  # bug: no "$" strip
    )
    base = _commit(repo, "base: parse_amount does not strip currency symbol")

    _write(
        repo / "feature.py",
        "def parse_amount(raw):\n    return float(raw.lstrip('$'))\n",
    )
    _write(
        repo / "test_feature.py",
        "from feature import parse_amount\n\n"
        "def test_parse_amount_strips_dollar_sign():\n"
        "    assert parse_amount('$5.00') == 5.0\n",
    )

    result = _run_check(repo, base, "test_feature.py::test_parse_amount_strips_dollar_sign")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "confirmed: `test_feature.py::test_parse_amount_strips_dollar_sign`" in result.stdout


def test_does_not_confirm_an_error_unrelated_to_the_defect(tmp_path):
    # Round-3b regression: a fixture that fails regardless of the
    # implementation (e.g. a missing external dependency) produces an
    # all-error report at the base commit. That report must not be read as
    # proof, because the same error reproduces against the current tree too.
    repo = _init_repo(tmp_path)
    _write(repo / "mymodule.py", "def compute(x):\n    return x - 1\n")
    _write(
        repo / "test_broken_fixture.py",
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def unrelated():\n"
        "    raise RuntimeError('missing external service')\n\n"
        "def test_uses_unrelated(unrelated):\n"
        "    assert True\n",
    )
    base = _commit(repo, "base: buggy compute, plus a fixture broken for unrelated reasons")

    # The "fix" changes compute() but the broken fixture is untouched, so the
    # same unrelated error is present on both sides of the comparison.
    _write(repo / "mymodule.py", "def compute(x):\n    return x + 1\n")

    result = _run_check(repo, base, "test_broken_fixture.py::test_uses_unrelated")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NOT confirmed" in result.stderr
    assert "does not pass cleanly against the current tree" in result.stderr


def test_refuses_a_node_id_with_no_test_file_anywhere(tmp_path):
    repo = _init_repo(tmp_path)
    _write(repo / "mymodule.py", "def compute(x):\n    return x + 1\n")
    base = _commit(repo, "base")

    result = _run_check(repo, base, "test_missing.py::test_nope")
    assert result.returncode == 1
    assert "regression proof refused" in result.stderr
    assert "does not exist in the current tree" in result.stderr
