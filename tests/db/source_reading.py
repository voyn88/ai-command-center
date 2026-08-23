"""Reading a function's code without reading its explanation.

Every slice of the runtime migration carries the same guard: SQLite stays the
authority, so no read path may mention PostgreSQL. It is written by grepping the
function's source for `postgres` — and the source also contains that word in the
comments explaining why PostgreSQL is *not* consulted, so grepping raw text made
the guard fail on its own explanation. Stripping prose first is what makes the
assertion about the code rather than about how the code is described.

Slices 3, 4 and 5 each wrote that out again, and by slice 4's acceptance the
copies had already diverged: the `digest_item` one had lost `ClassDef` from the
node tuple, so a docstring one level in survived the strip and could fail a
guard on a word it only ever explained. Harmless where it stood and exactly the
failure mode `mirror_support` exists to end — "every restatement is subtly
different" — reproduced in the test suite one slice after the lesson. The rule
lives here now, once, with `test_source_reading.py` pinning the nesting the
drifted copy dropped.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

__all__ = ["code_without_prose"]

#: Node types that can carry a docstring as their first statement. `Module` is
#: included for completeness rather than for effect — parsing a function yields
#: a module whose first statement is the `def` — so that the rule is "anything
#: Python lets you document", not a list of the cases one caller happened to hit.
_CAN_BE_DOCUMENTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)


def code_without_prose(function: object) -> str:
    """A function's executable code, with comments and docstrings removed.

    Every docstring is dropped, at any depth: a nested class or inner function
    is prose too, and the guards using this cannot tell the difference between a
    word that is executed and a word that is explained.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, _CAN_BE_DOCUMENTED):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)  # comments never survive a parse/unparse round trip
