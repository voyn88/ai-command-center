"""VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT fitness gate: no mutating
executor gets a shared checkout.

``command_center/workspace_provisioning.py`` implements a fail-closed gate
(``verify_workspace`` / ``provision_and_verify``) that refuses to launch a
feature/audit-branch task in the primary repository working tree — every such
task must run in its own isolated worktree or task-local clone
(``isolated_worktree_required`` verification step). ``WORKTREE-LEASE-TOCTOU``
and ``WORKTREE-LEAK-RETRY`` hardened the same gate against a held-lease race
and leaked worktrees respectively.

Unit tests in ``tests/test_workspace_provisioning.py`` already prove the gate
itself refuses a shared checkout. What they cannot see is the *other* way this
invariant breaks: a mutating-executor entry point quietly stops calling the
gate at all (a refactor drops the call, a new launch path is added that skips
it). This fitness test pins each known process-spawning entry point to the
gate by construction, so removing the call — not just weakening it — fails
CI. Static AST check, not a substring grep, so a mention in a comment or
docstring does not trip it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every module that can spawn a mutating executor process (an agent run that
# may commit/push) must call one of these before spawning it — the fail-closed
# isolation gate `workspace_provisioning` exposes for exactly this purpose.
GATE_FUNCTIONS = frozenset({"verify_workspace", "provision_and_verify"})

# Known entry points that provision/launch a workspace for a mutating
# executor attempt. A new launch path belongs in this tuple *and* must call
# the gate — adding one without the other is the bug this test exists to
# catch.
MUTATING_EXECUTOR_ENTRY_POINTS = (
    REPO_ROOT / "command_center" / "launch_service.py",
    REPO_ROOT / "command_center" / "worker" / "handlers.py",
    REPO_ROOT / "command_center" / "runtime" / "supervisor.py",
)


def _calls_gate_function(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name in GATE_FUNCTIONS:
            found.add(name)
    return found


def test_every_mutating_executor_entry_point_calls_the_isolation_gate():
    missing = {
        str(path.relative_to(REPO_ROOT)): _calls_gate_function(path)
        for path in MUTATING_EXECUTOR_ENTRY_POINTS
    }
    offenders = {name: hits for name, hits in missing.items() if not hits}
    assert not offenders, (
        "mutating-executor launch path no longer calls "
        "workspace_provisioning.verify_workspace/provision_and_verify — a "
        "shared/primary checkout can no longer be refused before launch: "
        f"{offenders}"
    )


def test_entry_point_list_itself_is_not_empty():
    """Guards the guard: an empty tuple would make the test above vacuously
    pass. If a listed entry point is genuinely retired, remove it here
    deliberately rather than letting the file disappear silently."""
    assert MUTATING_EXECUTOR_ENTRY_POINTS
    for path in MUTATING_EXECUTOR_ENTRY_POINTS:
        assert path.is_file(), f"entry point moved or renamed: {path}"
