"""Domain tests for the Silent Audit Simulator.

``run_silent_audit`` is exercised for its two defining properties: it never
raises (a broken check becomes a failed result, not an exception) and it never
touches the real target tree (the sandbox copy is what a check actually scans).
``evaluate_silent_audit_coverage`` is tested as the pure function it is, the
same way ``tests/test_delivery_gate.py`` tests ``evaluate_delivery``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.audit.checks.base import Check
from command_center.audit.registry import CheckRegistry
from command_center.audit.silent import (
    evaluate_silent_audit_coverage,
    mini_sandbox,
    run_silent_audit,
)
from command_center.audit.types import CheckContext, Finding

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


class _RecordingCheck(Check):
    """Records the target it was actually asked to scan, and returns one
    finding referencing a file only the sandbox copy (not the real target)
    contains."""

    name = "recording"
    category = "code-quality"

    def __init__(self) -> None:
        self.seen_targets: list[Path] = []

    def run(self, ctx: CheckContext) -> list[Finding]:
        self.seen_targets.append(ctx.target)
        return [Finding(category=self.category, summary="probe", owner="engineering")]


class _BoomCheck(Check):
    name = "boom"
    category = "code-quality"

    def run(self, ctx: CheckContext) -> list[Finding]:
        raise RuntimeError("tool is not installed")


def _registry_of(*checks: Check) -> CheckRegistry:
    registry = CheckRegistry()
    for check in checks:
        registry.register(check.name, lambda c=check: c)
    return registry


# --- mini_sandbox -----------------------------------------------------------


def test_mini_sandbox_copies_tree_and_cleans_up(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "a.py").write_text("x = 1\n")

    with mini_sandbox(target) as sandbox_root:
        assert sandbox_root != target
        assert (sandbox_root / "a.py").read_text() == "x = 1\n"
        captured = sandbox_root

    assert not captured.exists()
    # the real target is untouched and still there
    assert (target / "a.py").read_text() == "x = 1\n"


def test_mini_sandbox_skips_vcs_and_cache_dirs(tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / ".git").mkdir(parents=True)
    (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (target / "__pycache__").mkdir()
    (target / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (target / "src").mkdir()
    (target / "src" / "a.py").write_text("x = 1\n")

    with mini_sandbox(target) as sandbox_root:
        assert (sandbox_root / "src" / "a.py").exists()
        assert not (sandbox_root / ".git").exists()
        assert not (sandbox_root / "__pycache__").exists()


# --- run_silent_audit ---------------------------------------------------


def test_run_silent_audit_scans_the_sandbox_copy_not_the_real_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "a.py").write_text("x = 1\n")
    check = _RecordingCheck()

    result = run_silent_audit(
        candidate_sha=SHA_A,
        project="AICC",
        target=target,
        db_path=tmp_path / "runtime.db",
        registry=_registry_of(check),
    )

    assert result.ok is True
    assert result.candidate_sha == SHA_A
    assert result.finding_count == 1
    assert len(check.seen_targets) == 1
    assert check.seen_targets[0] != target
    assert target.is_dir()  # the real tree still exists, untouched


def test_run_silent_audit_never_raises_on_a_broken_check(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    result = run_silent_audit(
        candidate_sha=SHA_A,
        project="AICC",
        target=target,
        db_path=tmp_path / "runtime.db",
        registry=_registry_of(_BoomCheck()),
    )

    assert result.ok is False
    assert result.finding_count == 0
    assert "tool is not installed" in (result.error or "")


def test_run_silent_audit_never_raises_when_target_is_missing(tmp_path: Path) -> None:
    result = run_silent_audit(
        candidate_sha=SHA_A,
        project="AICC",
        target=tmp_path / "does-not-exist",
        db_path=tmp_path / "runtime.db",
        registry=_registry_of(_RecordingCheck()),
    )

    assert result.ok is False
    assert result.error


def test_run_silent_audit_records_timestamps(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    result = run_silent_audit(
        candidate_sha=SHA_A,
        project="AICC",
        target=target,
        db_path=tmp_path / "runtime.db",
        registry=_registry_of(_RecordingCheck()),
    )

    assert result.started_at
    assert result.completed_at
    assert result.started_at <= result.completed_at


# --- evaluate_silent_audit_coverage -------------------------------------


def test_coverage_meets_threshold_when_all_changes_audited() -> None:
    coverage = evaluate_silent_audit_coverage(
        merged_shas=[SHA_A, SHA_B],
        audited_shas=[SHA_A, SHA_B],
    )

    assert coverage.total_changes == 2
    assert coverage.audited_changes == 2
    assert coverage.ratio == 1.0
    assert coverage.meets_threshold is True
    assert coverage.missing_shas == ()


def test_coverage_flags_missing_shas_below_threshold() -> None:
    coverage = evaluate_silent_audit_coverage(
        merged_shas=[SHA_A, SHA_B, SHA_C],
        audited_shas=[SHA_A],
    )

    assert coverage.total_changes == 3
    assert coverage.audited_changes == 1
    assert coverage.ratio == pytest.approx(1 / 3)
    assert coverage.meets_threshold is False
    assert coverage.missing_shas == (SHA_B, SHA_C)


def test_coverage_at_exactly_ninety_percent_meets_default_threshold() -> None:
    merged = [f"{i:040x}" for i in range(10)]
    audited = merged[:9]

    coverage = evaluate_silent_audit_coverage(merged_shas=merged, audited_shas=audited)

    assert coverage.ratio == pytest.approx(0.9)
    assert coverage.meets_threshold is True


def test_coverage_just_below_ninety_percent_fails_default_threshold() -> None:
    merged = [f"{i:040x}" for i in range(11)]
    audited = merged[:9]

    coverage = evaluate_silent_audit_coverage(merged_shas=merged, audited_shas=audited)

    assert coverage.ratio < 0.9
    assert coverage.meets_threshold is False


def test_coverage_is_vacuously_full_with_no_merged_changes() -> None:
    coverage = evaluate_silent_audit_coverage(merged_shas=[], audited_shas=[])

    assert coverage.total_changes == 0
    assert coverage.ratio == 1.0
    assert coverage.meets_threshold is True


def test_coverage_dedups_repeated_merged_shas() -> None:
    coverage = evaluate_silent_audit_coverage(
        merged_shas=[SHA_A, SHA_A, SHA_B],
        audited_shas=[SHA_A, SHA_B],
    )

    assert coverage.total_changes == 2
    assert coverage.audited_changes == 2
