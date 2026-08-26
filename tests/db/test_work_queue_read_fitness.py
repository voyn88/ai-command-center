"""Source pins for the read store — structural, no database.

Two mutants acceptance cares about, neither observable behaviourally today:

* ``FROM work_item_public`` silently becoming ``FROM work_item``. The base
  table shares every column the store selects, so the swap returns identical
  rows — but the view is the declared read path (the same statement of intent
  that routes ``work_attempt`` reads through its redacted view), and the app
  role's direct ``SELECT`` on ``work_item`` is a separately pinned SQL-lane
  decision this store must not lean on. Revoking that grant instead was
  considered and rejected here: `tests/db/test_roles_render.py` and
  `test_queue_claim.py` pin it as accepted authority, and flipping an
  accepted grant does not belong in an HTTP slice.
* The "read-only by construction" claim in the module docstring quietly
  gaining a write. The HTTP layer's safety argument rests on it.

Deliberately structural (AST string-scan over the module, docstrings
excluded), like `test_stored_reader_fitness`: behavioural detection would
need a database and still could not see the view->table swap, because there
is no behavioural difference to see.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "command_center/db/work_queue_read.py"

#: The store's whole read surface. `work_item_public` and `work_attempt_public`
#: are the redacted views; `work_result` is a granted base-table read (the
#: coordination record the control plane exists to consume).
ALLOWED_RELATIONS = {"work_item_public", "work_attempt_public", "work_result"}

_WRITE_OR_PROTOCOL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE\s+|CALL\s+|queue_\w+\s*\()",
    re.I,
)


def _code_strings() -> list[str]:
    """Every string literal in the module EXCEPT docstrings.

    Docstrings legitimately discuss SQL verbs in prose ("no INSERT/UPDATE");
    the strings that feed cursors do not. Fragments of f-strings appear as
    separate constants, so concatenated SQL is still scanned piecewise.
    """
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
    relations = {
        match
        for text in _code_strings()
        for match in re.findall(r"\bFROM\s+([a-z_]+)", text)
    }
    assert relations == ALLOWED_RELATIONS, (
        f"work_queue_read.py reads {sorted(relations)}; its declared surface is "
        f"{sorted(ALLOWED_RELATIONS)}. Reading the base work_item table would "
        "bypass the view that is the store's recorded read path."
    )


def test_the_store_stays_read_only_by_construction() -> None:
    offending = [text for text in _code_strings() if _WRITE_OR_PROTOCOL.search(text)]
    assert offending == [], (
        f"write or protocol SQL found in the read store: {offending}"
    )
