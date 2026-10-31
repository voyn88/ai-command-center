"""Import scanner for AUDIT-W2-005 — the removed Universal Workspace scaffolding.

``command_center/workspace_context.py``, ``command_center/workspace_service.py``
and ``command_center/ui/panel_registry.py`` were deleted as dead, disconnected
scaffolding (``docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md``,
AUDIT-W2-005: "Done (resolved by removal)", git ``552f2d6``/``b798bf2``). That
closure carried no executable gate, so nothing would stop a later change from
re-importing one of the three names — the gate that matters is static (the
files are gone; importing them fails at runtime regardless), but a static gate
is also the one that catches the mistake before anyone tries to run it.

``find_removed_scaffolding_imports`` resolves every import form to its fully
dotted target before comparing against ``REMOVED_MODULES``, so an absolute
import (``import command_center.workspace_context``), a package-relative
import (``from command_center import workspace_context`` — the alias form the
prior gate missed, since it only read ``ast.ImportFrom.module``), a direct
submodule import (``from command_center.workspace_context import
get_context``) and a same-package relative import (``from . import
workspace_context``, from a module inside ``command_center/``) are all caught
identically.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture import aios_boundary

REPO_ROOT = aios_boundary.REPO_ROOT

REMOVED_MODULES: frozenset[str] = frozenset(
    {
        "command_center.workspace_context",
        "command_center.workspace_service",
        "command_center.ui.panel_registry",
    }
)


def _matches_removed(dotted: str) -> bool:
    return dotted in REMOVED_MODULES or any(
        dotted.startswith(f"{removed}.") for removed in REMOVED_MODULES
    )


def _package_parts(rel_path: str) -> tuple[str, ...]:
    """The dotted package a relative import in this file resolves against.

    Equivalent to Python's ``__package__``: the directory containing the
    file. That is the same computation for a plain module and for that
    directory's own ``__init__.py`` alike — dropping the file's own path
    segment is the whole rule either way.
    """
    return tuple(rel_path[:-3].split("/")[:-1])


def _resolve_relative(rel_path: str, level: int, module: str | None) -> str:
    base = _package_parts(rel_path)
    for _ in range(level - 1):
        base = base[:-1]
    if module:
        base = base + tuple(module.split("."))
    return ".".join(base)


def find_removed_scaffolding_imports(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """(lineno, description) for every import that resolves onto a removed module."""
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_removed(alias.name):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            dots = "." * node.level
            if node.level == 0:
                source = node.module or ""
            else:
                source = _resolve_relative(rel_path, node.level, node.module)
            if source and _matches_removed(source):
                violations.append((node.lineno, f"from {dots}{node.module or ''} import ..."))
            for alias in node.names:
                candidate = f"{source}.{alias.name}" if source else alias.name
                if _matches_removed(candidate):
                    violations.append(
                        (node.lineno, f"from {dots}{node.module or ''} import {alias.name}")
                    )
    return violations


def iter_python_files() -> list[Path]:
    return aios_boundary.iter_python_files(REPO_ROOT)
