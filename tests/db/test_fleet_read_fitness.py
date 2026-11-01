"""Structural pins for the Fleet read store — no database required.

Same shape as ``test_work_queue_read_fitness``: the Fleet page's safety claim
is "read-only by construction", and a silent INSERT/UPDATE (or a swap from
``work_attempt_public`` back to the capability-bearing base table) would not
be visible in a mocked unit test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "command_center/db/fleet_read.py"

ALLOWED_RELATIONS = {
    "backlog_task",
    "backlog_evidence",
    "work_item_public",
    "work_attempt_public",
    "monitor_finding",
}

_WRITE_OR_PROTOCOL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE\s+|CALL\s+|queue_\w+\s*\()",
    re.I,
)


def _code_strings() -> list[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_every_from_targets_the_declared_read_surface() -> None:
    # CTE aliases (``ranked``, ``top_executors``) appear in FROM clauses too;
    # only base/view relations are the declared read surface.
    _cte_aliases = {"ranked", "top_executors"}
    relations = {
        match
        for text in _code_strings()
        for match in re.findall(r"\bFROM\s+([a-z_]+)", text)
    } - _cte_aliases
    assert relations == ALLOWED_RELATIONS, (
        f"fleet_read.py reads {sorted(relations)}; declared surface is "
        f"{sorted(ALLOWED_RELATIONS)}"
    )


def test_the_store_stays_read_only_by_construction() -> None:
    offending = [text for text in _code_strings() if _WRITE_OR_PROTOCOL.search(text)]
    assert offending == [], (
        f"write or protocol SQL found in the Fleet read store: {offending}"
    )


def test_work_attempt_reads_go_through_the_public_view() -> None:
    joined = "\n".join(_code_strings())
    assert "work_attempt_public" in joined
    assert not re.search(r"\bFROM\s+work_attempt\b", joined)
