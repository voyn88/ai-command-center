"""`prove_regression.py` refuses to confirm a node id unless every case it
collected at the base commit actually failed there.

Two shapes of the same defect shipped and were rejected before this file
existed, and every test below is named for the one it re-creates:

* `test_a_weak_node_id_next_to_a_strong_one_does_not_confirm_the_request` —
  the base run was accepted whenever *any* selected node id failed, so a
  test that already passed at the defect hid behind one that did not.
* `test_a_partially_failing_collected_case_does_not_confirm_its_node_id` —
  the same flaw one level down: a single node id can collect several cases
  (parametrize, a class, a whole file), and "at least one of them failed"
  is not "the node id catches the defect".

Both are proven against a real git repository and a real pytest subprocess,
not a mock of either: a test that cannot see the command cannot see its
divergence from the claim.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import prove_regression as pr

SOURCE = "def add(a, b):\n    return a - b  # bug: should be a + b\n"

TESTS = """\
import pytest

from source import add


def test_add():
    assert add(2, 3) == 5


def test_weak():
    # Passes regardless of `add`'s implementation: exactly the shape that
    # must not be able to hide behind `test_add` failing next to it.
    assert True


@pytest.mark.parametrize("x", [1, 2])
def test_partial(x):
    assert x == 2
"""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def base_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A throwaway git repo, one commit deep, with a real defect and three
    tests of differing honesty about catching it. Returns the commit sha.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "source.py").write_text(SOURCE, encoding="utf-8")
    (repo / "test_source.py").write_text(TESTS, encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "defect"], repo)
    commit = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    monkeypatch.setattr(pr, "ROOT", repo)
    return commit


# ---------------------------------------------------------------------------
# Pure logic: NodeVerdict and parse_junit, no subprocess involved.
# ---------------------------------------------------------------------------


def test_all_cases_failed_confirms_the_node() -> None:
    verdict = pr.NodeVerdict(
        node_id="t.py::test_x",
        cases=(pr.CaseResult("t.py::test_x", "failed"),),
    )
    assert verdict.confirmed is True


def test_a_passing_case_among_failures_does_not_confirm() -> None:
    verdict = pr.NodeVerdict(
        node_id="t.py::test_x",
        cases=(
            pr.CaseResult("t.py::test_x[1]", "failed"),
            pr.CaseResult("t.py::test_x[2]", "passed"),
        ),
    )
    assert verdict.confirmed is False
    assert "test_x[2]" in verdict.reason
    assert "1 did not fail" in verdict.reason


def test_a_skipped_case_does_not_confirm() -> None:
    verdict = pr.NodeVerdict(
        node_id="t.py::test_x",
        cases=(pr.CaseResult("t.py::test_x", "skipped"),),
    )
    assert verdict.confirmed is False


def test_zero_collected_cases_does_not_confirm() -> None:
    verdict = pr.NodeVerdict(node_id="t.py::test_x", cases=())
    assert verdict.confirmed is False
    assert "collected no test cases" in verdict.reason


def test_error_counts_as_the_test_dying_on_the_defect() -> None:
    verdict = pr.NodeVerdict(
        node_id="t.py::test_x",
        cases=(pr.CaseResult("t.py::test_x", "error"),),
    )
    assert verdict.confirmed is True


def test_parse_junit_reads_failure_error_skipped_and_passed() -> None:
    xml = """<?xml version="1.0"?>
    <testsuites>
      <testsuite>
        <testcase classname="t" name="a"><failure message="x"/></testcase>
        <testcase classname="t" name="b"><error message="x"/></testcase>
        <testcase classname="t" name="c"><skipped/></testcase>
        <testcase classname="t" name="d"/>
      </testsuite>
    </testsuites>
    """
    cases = pr.parse_junit(xml)
    assert {c.node_id: c.status for c in cases} == {
        "t::a": "failed",
        "t::b": "error",
        "t::c": "skipped",
        "t::d": "passed",
    }


def test_parse_junit_rejects_unparseable_xml() -> None:
    with pytest.raises(pr.ProofError, match="not valid XML"):
        pr.parse_junit("not xml")


# ---------------------------------------------------------------------------
# check() against a real git worktree and a real pytest subprocess.
# ---------------------------------------------------------------------------


def test_a_real_regression_test_is_confirmed(base_repo: str) -> None:
    verdicts = pr.check(base_repo, ["test_source.py::test_add"])
    assert verdicts["test_source.py::test_add"].confirmed is True


def test_a_weak_node_id_next_to_a_strong_one_does_not_confirm_the_request(
    base_repo: str,
) -> None:
    """Round-1 regression: two node ids, one honest and one weak.

    The old `check()` printed `confirmed` for both because *something*
    failed in the combined run. Each node id here gets its own pytest
    invocation and its own verdict, so the weak one cannot hide.
    """
    verdicts = pr.check(
        base_repo, ["test_source.py::test_add", "test_source.py::test_weak"]
    )
    assert verdicts["test_source.py::test_add"].confirmed is True
    assert verdicts["test_source.py::test_weak"].confirmed is False
    # The property a caller must actually check: not "any confirmed" but
    # "every requested node id confirmed".
    assert not all(v.confirmed for v in verdicts.values())


def test_a_partially_failing_collected_case_does_not_confirm_its_node_id(
    base_repo: str,
) -> None:
    """Round-2 regression: one node id, two parametrized cases, one failure.

    `test_partial` is a single node id that collects two cases; only `x=1`
    fails. The old `check()` confirmed the node id because its aggregate
    `failed` count was nonzero. Every collected case must fail now.
    """
    verdicts = pr.check(base_repo, ["test_source.py::test_partial"])
    verdict = verdicts["test_source.py::test_partial"]
    assert verdict.confirmed is False
    assert len(verdict.cases) == 2
    statuses = sorted(case.status for case in verdict.cases)
    assert statuses == ["failed", "passed"]


def test_cli_reports_each_node_id_independently_and_fails_closed(
    base_repo: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = pr.main(
        ["check", base_repo, "test_source.py::test_add", "test_source.py::test_weak"]
    )
    out, err = capsys.readouterr()
    assert exit_code == 1
    assert "confirmed: `test_source.py::test_add`" in out
    assert "test_weak" in err
    assert "NOT confirmed" in err


def test_cli_confirms_when_every_node_id_fails_in_full(
    base_repo: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = pr.main(["check", base_repo, "test_source.py::test_add"])
    out, _err = capsys.readouterr()
    assert exit_code == 0
    assert "confirmed: `test_source.py::test_add`" in out


def test_an_unresolvable_base_ref_is_refused(base_repo: str) -> None:
    with pytest.raises(pr.ProofError, match="does not resolve to a commit"):
        pr.check("not-a-real-ref", ["test_source.py::test_add"])


def test_a_node_id_pytest_cannot_find_is_refused(base_repo: str) -> None:
    with pytest.raises(pr.ProofError, match="pytest could not run"):
        pr.check(base_repo, ["test_source.py::test_does_not_exist"])


def test_no_node_ids_is_refused(base_repo: str) -> None:
    with pytest.raises(pr.ProofError, match="at least one pytest node id"):
        pr.check(base_repo, [])


def test_worktree_is_removed_after_check(base_repo: str) -> None:
    pr.check(base_repo, ["test_source.py::test_add"])
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=pr.ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert listing.strip().splitlines() == [listing.strip().splitlines()[0]]
