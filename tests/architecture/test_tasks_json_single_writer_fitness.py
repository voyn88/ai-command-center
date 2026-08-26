"""NIGHT-W9-AICC-ARCH slice 1 fitness gate: app.py never writes tasks.json.

``docs/AUTHORITY_MAP.md`` names ``tasks_repository.py`` as the **single
writer** of ``data/tasks.json``; ``app.py`` (and the legacy task helpers
extracted from it into ``command_center/ui/legacy_task_helpers.py``) must only
ever *delegate* to that repository, never open or write the store directly.
This is the "no second engine" acceptance criterion of the decomposition
issue, kept as a checked property instead of a hope: a direct
``TASKS_FILE.write_text(...)``, ``json.dump(..., open(TASKS_FILE))``,
``atomic_write_json(TASKS_FILE, ...)`` or similar reappearing in either file
fails this gate.

The check is AST-based (not a substring grep) so comments and docstrings that
merely *mention* tasks.json — of which both files legitimately have several —
do not trip it, while any actual call that hands a tasks-store path to a
writing primitive does.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Files that must stay delegation-only with respect to the tasks store.
GUARDED_FILES = (
    REPO_ROOT / "app.py",
    REPO_ROOT / "command_center" / "ui" / "legacy_task_helpers.py",
    # API surface: the read service never touches tasks.json as a store, and the
    # Wave-1 write service reaches the board only through
    # ``tasks_repository.create_task`` (the single writer), never directly.
    REPO_ROOT / "command_center" / "api" / "service.py",
    REPO_ROOT / "command_center" / "api" / "wave1_service.py",
)

# Method names that write, replace or delete a file when invoked on a path to
# the store (Path methods + json/os-level primitives).
_WRITING_ATTRS = frozenset(
    {
        "write_text",
        "write_bytes",
        "write",
        "dump",
        "unlink",
        "replace",
        "rename",
        "open",
        "touch",
    }
)

# Source fragments identifying the tasks store in an argument/receiver
# expression. `TASKS_FILE` / `TASKS_EXAMPLE_FILE` are app.py's own constants
# for it; the literal covers any freshly-built `... / "tasks.json"` path.
_STORE_MARKERS = ("TASKS_FILE", "tasks.json")


def _mentions_store(fragment: str | None) -> bool:
    if not fragment:
        return False
    return any(marker in fragment for marker in _STORE_MARKERS)


def _direct_write_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_src = ast.get_source_segment(source, node) or ""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _WRITING_ATTRS:
            receiver_src = ast.get_source_segment(source, func.value) or ""
            arg_srcs = " ".join(
                filter(
                    None,
                    (ast.get_source_segment(source, arg) for arg in list(node.args) + [kw.value for kw in node.keywords]),
                )
            )
            if _mentions_store(receiver_src) or _mentions_store(arg_srcs):
                violations.append(f"{path.name}:{node.lineno}: {call_src}")
        elif isinstance(func, ast.Name) and func.id in {"open", "atomic_write_json"}:
            arg_srcs = " ".join(
                filter(
                    None,
                    (ast.get_source_segment(source, arg) for arg in list(node.args) + [kw.value for kw in node.keywords]),
                )
            )
            if _mentions_store(arg_srcs):
                violations.append(f"{path.name}:{node.lineno}: {call_src}")
    return violations


def test_app_and_legacy_helpers_never_write_tasks_json_directly():
    violations = [v for path in GUARDED_FILES for v in _direct_write_violations(path)]
    assert not violations, (
        "direct tasks.json access outside tasks_repository.py (the single "
        "writer per docs/AUTHORITY_MAP.md) — route through the repository "
        f"instead: {violations}"
    )


def test_legacy_task_helpers_delegate_and_bring_no_second_engine():
    """The extracted helpers module may import only the canonical engine
    (`tasks_repository`, `models`) plus stdlib typing/pathlib — no json/sqlite/
    storage primitives of its own, which is what "no second engine" means at
    the import level."""
    path = REPO_ROOT / "command_center" / "ui" / "legacy_task_helpers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    allowed = {
        "__future__",
        "__future__.annotations",
        "pathlib",
        "pathlib.Path",
        "typing",
        "typing.Callable",
        "command_center",
        "command_center.models",
        "command_center.tasks_repository",
    }
    unexpected = imported - allowed
    assert not unexpected, (
        "legacy_task_helpers must stay pure delegation to tasks_repository/"
        f"models; unexpected imports: {sorted(unexpected)}"
    )
