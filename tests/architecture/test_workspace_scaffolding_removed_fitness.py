"""Fitness gate for AUDIT-W2-005 — the Universal Workspace scaffolding stays removed.

``command_center/workspace_context.py``, ``workspace_service.py`` and
``ui/panel_registry.py`` were deleted as dead, disconnected scaffolding (see
``docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md``,
AUDIT-W2-005). The closure that removed them shipped no executable gate, so a
later change reintroducing one of the three names would read as fine until
someone actually ran it. This test is that gate; the scanner and its import
resolution live in ``tests/architecture/workspace_scaffolding.py``.
"""

from __future__ import annotations

import ast

from tests.architecture import workspace_scaffolding as scaffolding


def test_removed_scaffolding_is_never_imported_again():
    """No file in the repository imports any of the three removed modules.

    Covers every ``*.py`` in the repository, statically, in every import
    form ``ast`` can express — see ``workspace_scaffolding.py`` for the list.
    """
    violations: list[str] = []
    for path in scaffolding.iter_python_files():
        rel_path = path.relative_to(scaffolding.REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        for lineno, description in scaffolding.find_removed_scaffolding_imports(tree, rel_path):
            violations.append(f"{rel_path}:{lineno}: {description}")
    assert not violations, (
        "AUDIT-W2-005 removed command_center/workspace_context.py, "
        "workspace_service.py and ui/panel_registry.py as dead, disconnected "
        "scaffolding; none of the three may be imported again, in any form "
        "(docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md):\n"
        + "\n".join(violations)
    )


def test_scanner_catches_every_import_form():
    """Mutation check for :func:`workspace_scaffolding.find_removed_scaffolding_imports`.

    The first version of this gate recorded only ``ast.ImportFrom.module``
    and ignored the imported names, so ``from command_center import
    workspace_context`` — module ``command_center``, name
    ``workspace_context`` — passed silently. Every shape below must be
    caught, and the negative case must not be.
    """
    positive_cases = [
        ("import command_center.workspace_context\n", "command_center/app.py"),
        ("import command_center.workspace_context as wc\n", "command_center/app.py"),
        ("from command_center import workspace_context\n", "command_center/app.py"),
        ("from command_center.workspace_context import get_context\n", "command_center/app.py"),
        ("from command_center.ui import panel_registry\n", "command_center/app.py"),
        ("from . import workspace_context\n", "command_center/app.py"),
        ("from .workspace_context import get_context\n", "command_center/app.py"),
        ("from .. import workspace_service\n", "command_center/ui/task_cards.py"),
        ("from command_center import workspace_service as svc\n", "command_center/ui/task_cards.py"),
    ]
    for source, rel_path in positive_cases:
        tree = ast.parse(source, filename=rel_path)
        assert scaffolding.find_removed_scaffolding_imports(tree, rel_path), (source, rel_path)

    unrelated = ast.parse(
        "from command_center import tasks_repository\n"
        "import command_center.workspace_home\n"
        "from . import agent_runner\n",
        filename="command_center/app.py",
    )
    assert scaffolding.find_removed_scaffolding_imports(unrelated, "command_center/app.py") == []


def test_gate_is_not_silently_empty():
    """The tree-wide scan actually finds and reads Python files."""
    files = scaffolding.iter_python_files()
    assert any(path.name == "app.py" for path in files)
    assert any(path.as_posix().endswith("command_center/tasks_repository.py") for path in files)
