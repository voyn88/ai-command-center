"""Composite-call check: ``(f(x)).*`` / ``(f(x)).col`` runs ``f`` once per column.

PostgreSQL evaluates a row- or set-returning function referenced in a SELECT
list as ``(f(x)).*`` or ``(f(x)).col`` **once per referenced column** — silent,
plausible-looking, and disastrous for a function with side effects. A prototype
``queue_claim`` call site written this way burned seven claim attempts on a
single call. The correct form, ``SELECT * FROM f(x)``, evaluates ``f`` once.

``tests/db/test_queue_claim.py`` already pins this rule against its own source
so a new call site added to that one file cannot reintroduce the bug, but (by
its own docstring) that guard "would catch it for queue_claim, but not for a
call added to some other function." This check is that generalization: it
scans every ``.sql`` file and every SQL-shaped Python string literal in the
tree, so the mistake is caught wherever it is written next, not only here.

Restricted to strings that look like SQL (contain ``select``, case-insensitive)
to avoid flagging ordinary Python method chaining like ``Path(x).read_text()``,
which shares the same paren-then-dot shape but is not this bug.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import ClassVar

from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for

#: ``(identifier(args)).col`` or ``(identifier(args)).*`` — the per-column form.
#: Deliberately blind to nested calls (``[^()]*`` cannot cross a paren) — a
#: missed nested case is a false negative, not a false positive, and the simple
#: form covers every call site seen in this repo so far.
_COMPOSITE_CALL_RE = re.compile(
    r"\(\s*[A-Za-z_][A-Za-z0-9_.]*\s*\([^()]*\)\s*\)\s*\.\s*(?:\*|[A-Za-z_][A-Za-z0-9_]*)"
)

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _iter_files(root: Path, suffix: str) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob(f"*{suffix}"))
        if not _SKIP_DIRS.intersection(path.parts)
    ]


def _relative(path: Path, target: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return path.name


def _strip_sql_comments(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _sql_offenders(text: str) -> list[tuple[int, str]]:
    offenders = []
    for lineno, line in enumerate(_strip_sql_comments(text).splitlines(), start=1):
        match = _COMPOSITE_CALL_RE.search(line)
        if match:
            offenders.append((lineno, match.group(0)))
    return offenders


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Identity, not pattern: a module that documents this rule (like the
    ``queue_claim`` test) names the bad form on purpose, in prose the scan must
    not mistake for the mistake it describes."""
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _python_offenders(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    docstrings = _docstring_ids(tree)
    offenders = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            continue
        # Per line, not per string: a multi-line literal can embed both a
        # correct SQL statement and unrelated Python (e.g. a child-process
        # script), and "select" appearing anywhere in the blob must not make
        # every other line in it fair game for the composite-call pattern.
        for offset, line in enumerate(node.value.splitlines()):
            if "select" not in line.lower():
                continue
            match = _COMPOSITE_CALL_RE.search(line)
            if match:
                offenders.append((node.lineno + offset, match.group(0)))
    return offenders


class CompositeCallCheck(Check):
    """Raise one ``lint`` finding per per-column composite-call site found in a
    ``.sql`` file or a SQL-shaped Python string literal under the target."""

    name: ClassVar[str] = "composite-call"
    category: ClassVar[str] = "lint"

    def run(self, ctx: CheckContext) -> list[Finding]:
        owner = default_owner_for(self.category)
        findings: list[Finding] = []
        for path in _iter_files(ctx.target, ".sql"):
            text = self._read(path)
            for lineno, snippet in _sql_offenders(text):
                findings.append(self._finding(owner, path, ctx.target, lineno, snippet))
        for path in _iter_files(ctx.target, ".py"):
            text = self._read(path)
            for lineno, snippet in _python_offenders(text):
                findings.append(self._finding(owner, path, ctx.target, lineno, snippet))
        return findings

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _finding(
        self, owner: str, path: Path, target: Path, lineno: int, snippet: str
    ) -> Finding:
        return Finding(
            category=self.category,
            summary=(
                f"per-column composite call `{snippet}` runs the function once per "
                "referenced column instead of once; use `SELECT * FROM f(...)`"
            ),
            owner=owner,
            severity="high",
            file_path=_relative(path, target),
            loc=str(lineno),
            source=self.name,
        )
