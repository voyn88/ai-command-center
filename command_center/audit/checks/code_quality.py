"""Code-quality check: cyclomatic complexity via ruff's mccabe (``C90``) rule.

Selects ruff's ``C90`` family with an explicit max-complexity threshold and maps
each over-complex function to a ``code-quality`` finding. Real and in-repo: it
reuses ruff's built-in mccabe analysis over the project's own tree — no extra
tool. The threshold is tunable via ``ctx.options['max_complexity']``.
"""

from __future__ import annotations

from typing import ClassVar

from command_center.audit.checks import _ruff
from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for

#: Default cyclomatic-complexity ceiling. A function above this is flagged for a
#: look, not condemned — hence ``medium`` severity.
_DEFAULT_MAX_COMPLEXITY = 10


class CodeQualityCheck(Check):
    """Raise one ``code-quality`` finding per function over the complexity ceiling."""

    name: ClassVar[str] = "code-quality"
    category: ClassVar[str] = "code-quality"

    select: ClassVar[list[str]] = ["C90"]

    def run(self, ctx: CheckContext) -> list[Finding]:
        max_complexity = int(ctx.options.get("max_complexity", _DEFAULT_MAX_COMPLEXITY))
        owner = default_owner_for(self.category)
        diags = _ruff.run_ruff(
            ctx.target,
            select=self.select,
            extra_args=["--config", f"lint.mccabe.max-complexity={max_complexity}"],
        )
        findings: list[Finding] = []
        for diag in diags:
            code = str(diag.get("code") or "")
            message = str(diag.get("message") or "").strip()
            findings.append(
                Finding(
                    category=self.category,
                    summary=f"{code}: {message}" if code else message,
                    owner=owner,
                    severity="medium",
                    file_path=_ruff.relative_file(diag, ctx.target),
                    loc=_ruff.location(diag),
                    source=self.name,
                )
            )
        return findings
