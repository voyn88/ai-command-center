"""Scanner for AUDIT-W1-007 — UI task deletion stays behind confirm_dialog.

AUDIT-W1-007 closed the one-click Kanban delete by requiring an explicit
checkbox inside a modal before ``delete_task`` runs. That closure originally
shipped with only an AppTest and a stale "coverage gap" note, so a later UI
surface that wired ``Удалить`` straight to ``delete_task`` would not be caught
by an architecture fitness gate. The shared ``confirm_dialog`` helper is now
the single bar every destructive UI delete must clear
(``command_center/ui/confirm_dialog.py``); this scanner keeps that bar in place.

Two mechanical checks, both AST-only:

1. Every Streamlit button labelled ``Удалить`` in ``command_center/ui/`` must
   open confirmation in its click path and must not call a delete helper there.
2. Every ``delete_task`` call in ``command_center/ui/`` (excluding the helper
   definition itself) must sit inside an ``on_confirm=`` keyword of
   ``render_destructive_confirmation``, or inside a private ``_delete_*``
   helper that is only referenced from such an ``on_confirm``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture import aios_boundary

REPO_ROOT = aios_boundary.REPO_ROOT
UI_ROOT = REPO_ROOT / "command_center" / "ui"

DELETE_BUTTON_LABEL = "Удалить"
_DELETE_HELPER_PREFIX = "_delete_"
_CONFIRM_OPEN = "open_confirmation"
_CONFIRM_RENDER = "render_destructive_confirmation"
_DELETE_CALL_NAMES = frozenset({"delete_task"})


def iter_ui_python_files() -> list[Path]:
    return sorted(
        path
        for path in UI_ROOT.rglob("*.py")
        if path.is_file() and path.name != "confirm_dialog.py"
    )


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_delete_button_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _call_name(node) != "button":
        return False
    if not node.args:
        return False
    label = node.args[0]
    return isinstance(label, ast.Constant) and label.value == DELETE_BUTTON_LABEL


def _subtree_calls_named(node: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and _call_name(child) in names
    ]


def _click_bodies_for_delete_buttons(tree: ast.AST) -> list[tuple[int, list[ast.stmt]]]:
    """(lineno, body stmts) for every ``if <Удалить-button>:`` / ``if clicked:`` path."""
    bodies: list[tuple[int, list[ast.stmt]]] = []
    button_assign_names: dict[str, int] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _is_delete_button_call(node.value):
                button_assign_names[target.id] = node.lineno

    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if _is_delete_button_call(test):
            bodies.append((test.lineno, list(node.body)))
            continue
        if isinstance(test, ast.Name) and test.id in button_assign_names:
            bodies.append((button_assign_names[test.id], list(node.body)))
    return bodies


def find_unguarded_delete_buttons(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """Buttons labelled ``Удалить`` whose click path deletes or skips confirmation."""
    problems: list[tuple[int, str]] = []
    for lineno, body in _click_bodies_for_delete_buttons(tree):
        body_root = ast.Module(body=body, type_ignores=[])
        opens = _subtree_calls_named(body_root, frozenset({_CONFIRM_OPEN}))
        deletes = _subtree_calls_named(body_root, _DELETE_CALL_NAMES)
        if deletes:
            problems.append(
                (
                    lineno,
                    f"{rel_path}:{lineno}: 'Удалить' click path calls delete_task "
                    "directly — must only open confirm_dialog",
                )
            )
        if not opens:
            problems.append(
                (
                    lineno,
                    f"{rel_path}:{lineno}: 'Удалить' click path never calls "
                    "confirm_dialog.open_confirmation",
                )
            )
    return problems


def _on_confirm_value_nodes(tree: ast.AST) -> list[ast.AST]:
    values: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != _CONFIRM_RENDER:
            continue
        for keyword in node.keywords:
            if keyword.arg == "on_confirm":
                values.append(keyword.value)
    return values


def _names_referenced_as_on_confirm(tree: ast.AST) -> set[str]:
    """Names passed as ``on_confirm`` or called from an ``on_confirm`` lambda.

    Covers both ``on_confirm=_delete_finding`` and the more common
    ``on_confirm=lambda: _delete_finding(...)`` shape used by the live UI.
    """
    names: set[str] = set()
    for value in _on_confirm_value_nodes(tree):
        if isinstance(value, ast.Name):
            names.add(value.id)
            continue
        if isinstance(value, ast.Lambda):
            for child in ast.walk(value):
                if isinstance(child, ast.Call):
                    called = _call_name(child)
                    if called:
                        names.add(called)
                elif isinstance(child, ast.Name):
                    names.add(child.id)
    return names


def _function_defs(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def find_unguarded_delete_calls(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """``delete_task`` calls in UI code that are not behind confirm_dialog."""
    if rel_path.endswith("legacy_task_helpers.py"):
        # The shared helper *defines* delete_task; callers are gated elsewhere.
        return []

    allowed_helper_names = {
        name
        for name in _names_referenced_as_on_confirm(tree)
        if name.startswith(_DELETE_HELPER_PREFIX)
    }
    defs = _function_defs(tree)
    problems: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _DELETE_CALL_NAMES:
            continue
        # Inside an on_confirm= lambda passed to render_destructive_confirmation.
        if _call_is_inside_on_confirm_lambda(tree, node):
            continue
        owner = _enclosing_function_name(tree, node)
        if owner is not None and owner in allowed_helper_names:
            continue
        if owner is not None and owner.startswith(_DELETE_HELPER_PREFIX):
            # Helper exists but is not wired through on_confirm — still a gap.
            if owner not in allowed_helper_names:
                problems.append(
                    (
                        node.lineno,
                        f"{rel_path}:{node.lineno}: {owner} calls delete_task but is "
                        "not referenced as render_destructive_confirmation(on_confirm=...)",
                    )
                )
            continue
        problems.append(
            (
                node.lineno,
                f"{rel_path}:{node.lineno}: delete_task call is not behind "
                "confirm_dialog.render_destructive_confirmation(on_confirm=...)",
            )
        )

    # Helpers that look like delete helpers but are never confirmed.
    for name, fn in defs.items():
        if not name.startswith(_DELETE_HELPER_PREFIX):
            continue
        if name in allowed_helper_names:
            continue
        if _subtree_calls_named(fn, _DELETE_CALL_NAMES):
            problems.append(
                (
                    fn.lineno,
                    f"{rel_path}:{fn.lineno}: {name} deletes a task but is never "
                    "passed as on_confirm to render_destructive_confirmation",
                )
            )
    return problems


def _enclosing_function_name(tree: ast.AST, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is target:
                return node.name
    return None


def _call_is_inside_on_confirm_lambda(tree: ast.AST, target: ast.AST) -> bool:
    for value in _on_confirm_value_nodes(tree):
        if not isinstance(value, ast.Lambda):
            continue
        for child in ast.walk(value):
            if child is target:
                return True
    return False


def scan_ui_tree() -> list[str]:
    """All W1-007 confirmation violations across ``command_center/ui/``."""
    problems: list[str] = []
    for path in iter_ui_python_files():
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        for _, message in find_unguarded_delete_buttons(tree, rel_path):
            problems.append(message)
        for _, message in find_unguarded_delete_calls(tree, rel_path):
            problems.append(message)
    return problems
