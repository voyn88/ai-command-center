"""Direct coverage for `tests/db/code_without_prose.py`.

`VOYN-W0-AICC-TEST-HELPER-DUPLICATION`: this AST walk used to be copied into
three mirror test suites, and by the time the third copy was written the
second had already dropped `ast.ClassDef` from the tuple of node types it
strips docstrings from. Harmless in every mirror test that existed at the
time, because none of the checked functions declare a nested class — exactly
the gap this file pins, so a future edit that drops `ClassDef` again fails a
test instead of shipping silently.
"""

from __future__ import annotations

from tests.db.code_without_prose import code_without_prose


def test_a_docstring_on_a_nested_class_is_stripped() -> None:
    def has_a_nested_class() -> str:
        class Marker:
            """postgres — present only here, in a nested class's docstring."""

            postgres = "not a docstring, must survive"

        return Marker.postgres

    code = code_without_prose(has_a_nested_class)

    assert "present only here" not in code
    assert "postgres" in code  # the class body below the docstring is real code


def test_the_function_and_module_docstrings_are_also_stripped() -> None:
    def documented() -> int:
        """postgres, mentioned only in this function's own docstring."""
        return 1

    code = code_without_prose(documented)

    assert "mentioned only in this function" not in code
    assert "return 1" in code
