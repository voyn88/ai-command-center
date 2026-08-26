"""Lint check: pyflakes/pycodestyle diagnostics via ruff.

Selects ruff's ``F`` (pyflakes) and ``E``/``W`` (pycodestyle) families — the
everyday correctness-and-style signal — and maps each diagnostic to a ``lint``
finding. Real and in-repo: it runs the same linter the project already uses, over
the project's own tree, with no network and no paid API.
"""

from __future__ import annotations

from typing import ClassVar

from command_center.audit.checks import _ruff
from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for


def _severity(code: str) -> str:
    """Undefined names / syntax errors are real defects (``medium``); the rest of
    the lint family is stylistic (``low``)."""
    if code.startswith(("E9", "F82", "F81", "F63")):
        return "medium"
    return "low"


class LintCheck(Check):
    """Raise one ``lint`` finding per ruff pyflakes/pycodestyle diagnostic."""

    name: ClassVar[str] = "lint"
    category: ClassVar[str] = "lint"

    #: The ruff rule families this check enables.
    select: ClassVar[list[str]] = ["F", "E", "W"]

    def run(self, ctx: CheckContext) -> list[Finding]:
        owner = default_owner_for(self.category)
        findings: list[Finding] = []
        for diag in _ruff.run_ruff(ctx.target, select=self.select):
            code = str(diag.get("code") or "")
            message = str(diag.get("message") or "").strip()
            findings.append(
                Finding(
                    category=self.category,
                    summary=f"{code}: {message}" if code else message,
                    owner=owner,
                    severity=_severity(code),
                    file_path=_ruff.relative_file(diag, ctx.target),
                    loc=_ruff.location(diag),
                    source=self.name,
                )
            )
        return findings
