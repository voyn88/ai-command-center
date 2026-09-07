"""Silent Audit Simulator — an unobtrusive trial audit run per change.

Every other entry point into the Audit engine (:mod:`command_center.api.audit_service`)
is a deliberate, user-visible action: someone (or an automation) asks for a run,
waits for it, and reviews the findings. This module is the opposite shape: a
best-effort pass a completion path can fire off for *every* candidate change
without asking permission, without blocking on the result, and without ever
being able to fail the caller.

Two properties make a pass "silent":

* **Sandboxed** — :func:`mini_sandbox` copies the target tree into a throwaway
  temp directory before any check touches it, so a check's tool (ruff,
  coverage parsing) never mutates or races the live workspace a task or agent
  may still be writing to.
* **Non-blocking** — :func:`run_silent_audit` never raises. A broken check, a
  missing tool, or a sandbox copy failure becomes a failed
  :class:`SilentAuditResult` instead of an exception, so a caller can fire this
  off without wrapping it in its own try/except.

:func:`evaluate_silent_audit_coverage` is the pure counterpart to
:func:`command_center.delivery_gate.evaluate_delivery`: given the set of merged
change shas and the set of shas a silent pass actually recorded, it answers
whether the "90% of changes carry a silent audit result before merge"
acceptance bar holds — without performing any action itself.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from command_center.audit.registry import CheckRegistry, default_registry
from command_center.audit.runner import AuditRunner
from command_center.audit.types import CheckContext, Finding
from command_center.models import iso_now

#: Directories/files a sandbox copy skips — build artifacts and VCS metadata
#: that are irrelevant to every check and can be large enough to make the copy
#: itself the slow part of an otherwise fast silent pass.
_SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    "*.pyc",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
)


@contextmanager
def mini_sandbox(target: Path) -> Iterator[Path]:
    """Copy ``target`` into a throwaway temp directory and yield its path.

    Checks run against the copy, never ``target`` itself, so a check that
    crashes mid-scan, a tool that writes cache files next to what it reads, or
    a pass racing a concurrent edit can never corrupt or block the real
    workspace. The copy is removed on exit regardless of how the ``with``
    block ends (return, or an exception the caller lets propagate).
    """
    with tempfile.TemporaryDirectory(prefix="silent-audit-") as tmp:
        sandbox_root = Path(tmp) / "sandbox"
        shutil.copytree(target, sandbox_root, ignore=_SANDBOX_IGNORE, dirs_exist_ok=True)
        yield sandbox_root


@dataclass(frozen=True, slots=True)
class SilentAuditResult:
    """The outcome of one silent pass over a candidate change.

    Always constructible without a check having run at all — ``ok=False`` with
    ``error`` set is a first-class result, not an exceptional one. That is what
    lets :func:`run_silent_audit` promise it never raises: every code path ends
    by building one of these, never by propagating.
    """

    candidate_sha: str
    project: str
    ok: bool
    checks: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    deduped: int = 0
    error: str | None = None
    started_at: str = ""
    completed_at: str = ""

    @property
    def finding_count(self) -> int:
        return len(self.findings)


def run_silent_audit(
    *,
    candidate_sha: str,
    project: str,
    target: Path,
    db_path: Path,
    registry: CheckRegistry | None = None,
    checks: list[str] | None = None,
) -> SilentAuditResult:
    """Run one silent, sandboxed audit pass over ``target`` for ``candidate_sha``.

    Best-effort by construction: any exception raised while copying the
    sandbox or running checks is caught and folded into a failed
    :class:`SilentAuditResult` rather than propagated. A completion path can
    call this inline, fire-and-forget, with no try/except of its own — the
    worst a broken check can do is produce a result with ``ok=False``.
    """
    started_at = iso_now()
    try:
        with mini_sandbox(target) as sandbox_root:
            runner = AuditRunner(registry=registry or default_registry())
            ctx = CheckContext(
                root=sandbox_root,
                target=sandbox_root,
                project=project,
                db_path=db_path,
            )
            collected = runner.collect(ctx, checks=checks)
        return SilentAuditResult(
            candidate_sha=candidate_sha,
            project=project,
            ok=True,
            checks=tuple(collected.checks),
            findings=tuple(collected.findings),
            deduped=collected.deduped,
            started_at=started_at,
            completed_at=iso_now(),
        )
    except Exception as exc:  # noqa: BLE001 — a silent pass must never raise
        return SilentAuditResult(
            candidate_sha=candidate_sha,
            project=project,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
            completed_at=iso_now(),
        )


@dataclass(frozen=True, slots=True)
class SilentAuditCoverage:
    """Whether recorded silent-audit evidence meets the acceptance bar."""

    total_changes: int
    audited_changes: int
    ratio: float
    meets_threshold: bool
    missing_shas: tuple[str, ...]


def evaluate_silent_audit_coverage(
    *,
    merged_shas: Iterable[str],
    audited_shas: Iterable[str],
    threshold: float = 0.9,
) -> SilentAuditCoverage:
    """Measure how many merged changes carry a recorded silent-audit result.

    A pure function over two sha collections — no I/O, no store lookup — so the
    acceptance bar ("90% of changes have a silent audit result before merge")
    can be asserted directly against whatever evidence a caller already holds,
    the same way :func:`command_center.delivery_gate.evaluate_delivery` turns CI
    evidence into a yes/no answer without performing an action itself. An empty
    ``merged_shas`` is vacuously fully covered (``ratio=1.0``) — there is
    nothing to have missed a silent pass.
    """
    merged = list(dict.fromkeys(merged_shas))
    audited = set(audited_shas)
    total = len(merged)
    missing = tuple(sha for sha in merged if sha not in audited)
    covered = total - len(missing)
    ratio = covered / total if total else 1.0
    return SilentAuditCoverage(
        total_changes=total,
        audited_changes=covered,
        ratio=ratio,
        meets_threshold=ratio >= threshold,
        missing_shas=missing,
    )
