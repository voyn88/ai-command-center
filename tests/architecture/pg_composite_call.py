"""Repo-wide scanner for the `(f(...)).col` composite-dereference anti-pattern.

PostgreSQL evaluates `(f(x)).col` and `(f(x)).*` once **per dereferenced
column**, not once per row: a composite-returning function referenced this
way runs again for every column pulled out of it. `SELECT * FROM f(x)` (or,
inside PL/pgSQL, assigning the call to a variable once and reading fields off
that) evaluates it exactly once.

`tests/db/test_queue_claim.py::test_no_call_site_uses_the_per_column_expansion_form`
pinned this for `queue_\\w+` call sites in its own file, but nothing stopped
the same shape from reappearing against `backlog_*`, `identity_*`, `enroll_*`,
or any function added later, anywhere else in the repo. This scanner has no
function-name allowlist and covers every `*.py` and `*.sql` file — the shape
is unsafe for any composite-returning function, not just the ones this
repo happens to have today.

Consumed by `tests/architecture/test_pg_composite_call_fitness.py`.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directory names never scanned (VCS, caches, third-party trees, envs).
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".claude",
    }
)

#: `(f(args)).col` / `(f(args)).*`. Deliberately fires on a single dereferenced
#: column, not just `.*`: Postgres already re-evaluates `f` per *reference*, so
#: one column read today is two silent calls the day someone adds a second.
BAD_FORM = re.compile(r"\(\s*[A-Za-z_]\w*\s*\([^)]*\)\s*\)\s*\.")


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    snippet: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.snippet.strip()}"


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in rel_parts)


def _python_docstring_ids(tree: ast.Module) -> set[int]:
    """Identity, not pattern: prose that *names* the bad form must not trip it.

    A module/class/function docstring's leading string constant is excluded by
    object identity. Only the first statement of a body qualifies, so a string
    constant used as a value (not a docstring position) is never exempted.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _scan_python_file(path: Path, rel: str) -> list[Violation]:
    """Scan string constants, never raw source.

    Ordinary Python already contains this exact shape as method chaining —
    `f(x).attr` on a real object is unremarkable code, not embedded SQL.
    Restricting the scan to non-docstring string literals keeps the gate about
    SQL text a call site sends to Postgres, not about how Python reads.
    Concatenated literals (`"a" + "b"`) are scanned piecewise as separate AST
    nodes, so a sample deliberately assembled from pieces — the documented way
    `test_queue_claim.py` keeps its own canary from tripping this rule — is not
    mistaken for a real call site.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except SyntaxError:
        return []
    docstrings = _python_docstring_ids(tree)
    return [
        Violation(rel, node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and BAD_FORM.search(node.value)
    ]


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line[: line.find("--")] if "--" in line else line for line in text.splitlines())


def _scan_sql_file(path: Path, rel: str) -> list[Violation]:
    """Scan raw SQL text with comments stripped first.

    `.sql` files have no docstring concept, so documentation that names the bad
    form (a migration's own comment) would otherwise trip the gate the same way
    a code comment would; stripping `--` and `/* */` comments before matching
    keeps the scan on statements Postgres will actually execute.
    """
    stripped = _strip_sql_comments(path.read_text(encoding="utf-8"))
    return [
        Violation(rel, lineno, line)
        for lineno, line in enumerate(stripped.splitlines(), start=1)
        if BAD_FORM.search(line)
    ]


def find_violations(root: Path = REPO_ROOT) -> list[Violation]:
    """Every `(f(...)).col` / `(f(...)).*` call site in the repo, `.py` and `.sql` alike."""
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded(path.relative_to(root).parts):
            continue
        violations.extend(_scan_python_file(path, path.relative_to(root).as_posix()))
    for path in sorted(root.rglob("*.sql")):
        if _is_excluded(path.relative_to(root).parts):
            continue
        violations.extend(_scan_sql_file(path, path.relative_to(root).as_posix()))
    return violations
