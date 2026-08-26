"""Domain tests for the Wave-2 Audit engine — value objects, the pluggable
checks, the registry and the runner.

The file-parsing checks (deps, coverage) and the ruff-JSON parser run against
hand-built temp inputs, so they are fast and hermetic. One end-to-end lint pass
exercises the real ruff subprocess over a tiny tree to prove the tool plumbing.
The registry/runner tests use in-memory fake checks — no tooling at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.audit import AuditRunner, CheckContext, Finding, default_registry
from command_center.audit.checks import _ruff
from command_center.audit.checks.base import Check
from command_center.audit.checks.coverage import CoverageCheck
from command_center.audit.checks.deps import DepsCheck
from command_center.audit.checks.lint import LintCheck
from command_center.audit.registry import CheckRegistry
from command_center.audit.types import default_owner_for


def _ctx(tmp_path: Path, **options) -> CheckContext:
    return CheckContext(
        root=tmp_path, target=tmp_path, project="AICC", db_path=tmp_path / "runtime.db",
        options=options,
    )


# --- value objects --------------------------------------------------------


def test_finding_signature_dedups_on_normalized_summary() -> None:
    a = Finding(category="lint", summary="Unused  import", owner="engineering", file_path="a.py", loc="1")
    b = Finding(category="lint", summary="unused import", owner="engineering", file_path="a.py", loc="1")
    assert a.signature() == b.signature()


def test_finding_explicit_dedup_key_wins() -> None:
    f = Finding(category="lint", summary="x", owner="engineering", dedup_key="K")
    assert f.signature() == "K"


def test_default_owner_for_never_empty() -> None:
    for category in ("security", "coverage", "code-quality", "deps", "lint"):
        assert default_owner_for(category)
    assert default_owner_for("unmapped-category") == "engineering"


# --- deps check (pure file parsing) ---------------------------------------


def test_deps_check_flags_unpinned_only(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "requests\n"  # unpinned -> flagged
        "flask==2.0.0\n"  # pinned -> ok
        "# a comment\n"
        "-r other.txt\n"  # directive -> ignored
        "pkg @ git+https://example.com/x\n"  # URL install -> ok
        "\n"
    )
    findings = DepsCheck().run(_ctx(tmp_path))
    assert len(findings) == 1
    assert findings[0].category == "deps"
    assert findings[0].owner == default_owner_for("deps")
    assert "requests" in findings[0].summary
    assert findings[0].file_path == "requirements.txt"
    assert findings[0].loc == "1"


def test_deps_check_empty_when_all_pinned(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\nrequests==2.31.0\n")
    assert DepsCheck().run(_ctx(tmp_path)) == []


def test_deps_check_empty_when_no_requirements(tmp_path: Path) -> None:
    assert DepsCheck().run(_ctx(tmp_path)) == []


# --- coverage check (pure XML parsing) ------------------------------------


def _write_cov(tmp_path: Path, rate: float) -> None:
    (tmp_path / "coverage.xml").write_text(
        f'<?xml version="1.0" ?>\n<coverage line-rate="{rate}"></coverage>\n'
    )


def test_coverage_check_flags_below_threshold(tmp_path: Path) -> None:
    _write_cov(tmp_path, 0.42)
    findings = CoverageCheck().run(_ctx(tmp_path, min_coverage=0.80))
    assert len(findings) == 1
    assert findings[0].category == "coverage"
    assert findings[0].owner == default_owner_for("coverage")
    assert "42.0%" in findings[0].summary


def test_coverage_check_ok_above_threshold(tmp_path: Path) -> None:
    _write_cov(tmp_path, 0.95)
    assert CoverageCheck().run(_ctx(tmp_path, min_coverage=0.80)) == []


def test_coverage_check_info_when_no_report(tmp_path: Path) -> None:
    findings = CoverageCheck().run(_ctx(tmp_path))
    assert len(findings) == 1
    assert findings[0].severity == "info"


# --- ruff parser robustness -----------------------------------------------


def test_parse_ruff_json_tolerates_garbage() -> None:
    assert _ruff.parse_ruff_json("") == []
    assert _ruff.parse_ruff_json("not json") == []
    assert _ruff.parse_ruff_json('{"not": "a list"}') == []
    assert _ruff.parse_ruff_json('[{"code": "F401"}]') == [{"code": "F401"}]


def test_ruff_relative_file_never_leaks_absolute_path(tmp_path: Path) -> None:
    diag = {"filename": str(tmp_path / "sub" / "a.py")}
    (tmp_path / "sub").mkdir()
    assert _ruff.relative_file(diag, tmp_path) == "sub/a.py"
    outside = {"filename": "/etc/passwd"}
    assert _ruff.relative_file(outside, tmp_path) == "passwd"  # basename only


# --- lint check over a real tree (ruff subprocess) ------------------------


def test_lint_check_flags_unused_import(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("import os\nx = 1\n")
    findings = LintCheck().run(_ctx(tmp_path))
    codes = {f.summary.split(":")[0] for f in findings}
    assert "F401" in codes  # unused import detected
    assert all(f.category == "lint" and f.owner for f in findings)


# --- registry -------------------------------------------------------------


def test_default_registry_has_all_five_checks() -> None:
    names = set(default_registry().names())
    assert names == {"security", "lint", "code-quality", "deps", "coverage"}


def test_registry_refuses_duplicate_without_replace() -> None:
    reg = CheckRegistry()
    reg.register("x", lambda: _FakeCheck([]))
    with pytest.raises(ValueError):
        reg.register("x", lambda: _FakeCheck([]))
    reg.register("x", lambda: _FakeCheck([]), replace=True)  # explicit override ok


def test_registry_create_unknown_raises() -> None:
    with pytest.raises(KeyError):
        default_registry().create(["nope"])


# --- runner ---------------------------------------------------------------


class _FakeCheck(Check):
    name = "fake"
    category = "lint"

    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    def run(self, ctx: CheckContext) -> list[Finding]:
        return list(self._findings)


def test_runner_dedups_across_checks(tmp_path: Path) -> None:
    dup = Finding(category="lint", summary="same", owner="engineering", file_path="a.py", loc="1")
    reg = CheckRegistry()
    reg.register("one", lambda: _FakeCheck([dup]))
    reg.register("two", lambda: _FakeCheck([dup]))
    result = AuditRunner(registry=reg).collect(_ctx(tmp_path))
    assert len(result.findings) == 1
    assert result.deduped == 1
    assert result.checks == ["one", "two"]


def test_runner_subset_selection(tmp_path: Path) -> None:
    reg = CheckRegistry()
    reg.register("a", lambda: _FakeCheck([Finding(category="lint", summary="a", owner="eng")]))
    reg.register("b", lambda: _FakeCheck([Finding(category="lint", summary="b", owner="eng")]))
    result = AuditRunner(registry=reg).collect(_ctx(tmp_path), checks=["b"])
    assert result.checks == ["b"]
    assert [f.summary for f in result.findings] == ["b"]
