"""Founder Functional Audit 9761459, row AUDIT-W2-005 — stay removed.

`command_center/workspace_context.py`, `command_center/workspace_service.py`
and `command_center/panel_registry.py` were a closed cluster that imported
each other but nothing in production imported them (audit finding H7/H8:
"Universal Workspace scaffolding ... fully implemented and tested in
isolation ... but never wired into app.py"). They and their ~1120 lines of
tests were deleted in `552f2d6` / `b798bf2`.

That removal shipped with no fitness gate: nothing stopped the same
dead-cluster pattern from being reintroduced under these names, silently,
the way BLOCKER-1's fix regressed through a path no test watched. This
module is that gate — it fails if any of the three files reappear, or if
anything outside their own historical names starts importing those module
names.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REMOVED_MODULE_PATHS = (
    "command_center/workspace_context.py",
    "command_center/workspace_service.py",
    "command_center/panel_registry.py",
)

REMOVED_MODULE_NAMES = frozenset({"workspace_context", "workspace_service", "panel_registry"})


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.rsplit(".", 1)[-1])
    return names


def test_removed_workspace_scaffolding_files_do_not_reappear():
    reintroduced = [p for p in REMOVED_MODULE_PATHS if (REPO_ROOT / p).exists()]
    assert not reintroduced, (
        "AUDIT-W2-005 removed this dead, never-wired cluster (552f2d6/b798bf2); "
        f"it must not come back under the same name(s): {reintroduced}"
    )


def test_nothing_imports_the_removed_workspace_scaffolding_modules():
    violations: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        hit = REMOVED_MODULE_NAMES & _imported_module_names(tree)
        if hit:
            violations.append(f"{rel}: imports {sorted(hit)}")
    assert not violations, (
        "these module names were removed as a dead, unwired cluster (audit "
        f"H7/H8, AUDIT-W2-005) and must not be imported anywhere: {violations}"
    )
