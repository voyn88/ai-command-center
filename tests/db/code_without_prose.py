"""A function's executable code, with comments and docstrings removed.

The "SQLite remains the authority" guard in the mirror test suites greps a
function's source for `postgres`; the source text also contains the word in
comments explaining why PostgreSQL is *not* consulted, so grepping raw source
made the test fail on its own explanation. Prose is stripped so the assertion
is about the code rather than about how it is described — which is what the
test claims to check.

`VOYN-W0-AICC-TEST-HELPER-DUPLICATION`: this used to be three separate
definitions (`test_conflict_store.py`, `test_networking_store.py`, and an
inline copy in `test_digest_item_store.py`), and by the time a third slice
added its own copy the second had already drifted from the first — it walked
`ast.walk` without `ast.ClassDef`, so a docstring on a nested class would have
survived the strip. Harmless only because none of the checked functions
happened to declare a class. `mirror_support` was written to end exactly this
failure mode — a rule restated in more than one place ends up restated
differently — and this helper was the counter-example living in the same test
suite. One copy now; `test_code_without_prose.py` pins the `ClassDef` case so
a future edit that drops it again fails loudly instead of silently.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

__all__ = ["code_without_prose"]


def code_without_prose(function: object) -> str:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)  # comments never survive a parse/unparse round trip
