"""Dependency check: unpinned requirements in the project's requirement files.

Scans ``requirements*.txt`` under the target for dependency lines that are not
pinned to an exact version (no ``==`` and no direct URL/hash), and raises one
``deps`` finding per unpinned requirement. An unpinned dependency is a
supply-chain and reproducibility risk — a rebuild can silently pull a different,
possibly compromised, version. Real and in-repo: pure file parsing, no network,
no ``pip`` call.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for

#: Requirement-file globs scanned under the target.
_REQUIREMENTS_GLOBS = ("requirements*.txt",)

#: Markers that mean a line is already pinned or is not a plain PyPI requirement
#: (so absence of ``==`` is not a defect): exact pins, hashes, VCS/URL installs.
_PINNED_MARKERS = ("==", " @ ", "://", "--hash")


def _is_dependency_line(line: str) -> bool:
    """True for a line that names a dependency (not blank, comment, or a pip
    directive like ``-r other.txt`` / ``--index-url ...``)."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if stripped.startswith("-"):
        return False
    return True


def _is_unpinned(line: str) -> bool:
    """True when a dependency line carries no exact pin and is not a URL/VCS/hash
    install."""
    stripped = line.split("#", 1)[0].strip()
    return not any(marker in stripped for marker in _PINNED_MARKERS)


class DepsCheck(Check):
    """Raise one ``deps`` finding per unpinned requirement across all
    ``requirements*.txt`` files under the target."""

    name: ClassVar[str] = "deps"
    category: ClassVar[str] = "deps"

    def run(self, ctx: CheckContext) -> list[Finding]:
        owner = default_owner_for(self.category)
        findings: list[Finding] = []
        for pattern in _REQUIREMENTS_GLOBS:
            for req_file in sorted(ctx.target.glob(pattern)):
                findings.extend(self._scan_file(req_file, ctx.target, owner))
        return findings

    def _scan_file(self, req_file: Path, target: Path, owner: str) -> list[Finding]:
        try:
            text = req_file.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            rel = str(req_file.relative_to(target))
        except ValueError:
            rel = req_file.name
        out: list[Finding] = []
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not _is_dependency_line(raw) or not _is_unpinned(raw):
                continue
            name = raw.split("#", 1)[0].strip()
            out.append(
                Finding(
                    category=self.category,
                    summary=f"Unpinned dependency {name!r} (no exact '==' version)",
                    owner=owner,
                    severity="medium",
                    file_path=rel,
                    loc=str(lineno),
                    source=self.name,
                )
            )
        return out
