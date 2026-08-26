"""Security check: flake8-bandit (ruff ``S`` rules) over the project tree.

Selects ruff's ``S`` family — the bandit-derived security lints (hardcoded
passwords, ``subprocess`` with ``shell=True``, insecure hash/temp usage, ``eval``,
...) — and maps each diagnostic to a ``security`` finding owned by the security
role. Real and in-repo: no external scanner, no network.
"""

from __future__ import annotations

from typing import ClassVar

from command_center.audit.checks import _ruff
from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for

#: Hardcoded-credential rules — the highest-signal, lowest-false-positive class.
_CRITICAL_CODES = frozenset({"S105", "S106", "S107"})
#: ``assert`` used outside tests is a weak signal — worth flagging, not urgent.
_LOW_CODES = frozenset({"S101"})


def _severity(code: str) -> str:
    if code in _CRITICAL_CODES:
        return "critical"
    if code in _LOW_CODES:
        return "low"
    return "high"


class SecurityCheck(Check):
    """Raise one ``security`` finding per ruff ``S`` (bandit) diagnostic."""

    name: ClassVar[str] = "security"
    category: ClassVar[str] = "security"

    select: ClassVar[list[str]] = ["S"]

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
