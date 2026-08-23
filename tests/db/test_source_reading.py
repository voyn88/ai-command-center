"""The prose stripper, pinned at the depth its drifted copy stopped reaching.

`tests/db/source_reading.py` was hoisted out of three copies. Two agreed; the
third had lost `ClassDef`, which is invisible until a guarded function documents
a class inside itself — none did, so nothing failed and the drift survived a
slice. These tests state the depths as facts, so a future edit that narrows the
node tuple again fails here instead of silently weakening every authority guard
that imports it.

Plain `pytest`, no PostgreSQL: this is a rule about source text, and it should
keep running on the machines where the integration tests skip.
"""

from __future__ import annotations

from tests.db.source_reading import code_without_prose


def _documents_a_nested_class() -> str:
    """postgres: the module docstring case."""

    class Inner:
        """postgres: the nested class case, which the drifted copy kept."""

        def method(self) -> str:
            """postgres: a method inside that class."""
            return "kept"

    return Inner().method()


def _documents_an_inner_function() -> str:
    def inner() -> str:
        """postgres: the nested function case."""
        return "kept"

    return inner()


async def _is_async() -> str:
    """postgres: the async case."""
    return "kept"


def test_a_docstring_on_a_nested_class_is_stripped() -> None:
    """The exact drift: `ClassDef` missing meant this docstring survived.

    Asserted as absence *and* presence — a stripper that returned the empty
    string would satisfy the first half alone.
    """
    code = code_without_prose(_documents_a_nested_class)

    assert "postgres" not in code
    assert "class Inner" in code
    assert "return 'kept'" in code


def test_docstrings_are_stripped_at_every_depth_a_guard_can_meet() -> None:
    for function in (_documents_a_nested_class, _documents_an_inner_function, _is_async):
        code = code_without_prose(function)
        assert "postgres" not in code, function.__name__
        assert "kept" in code, function.__name__


def test_a_word_in_executable_code_survives() -> None:
    """The other half of the contract, and the reason the guards are worth
    anything: stripping prose must not strip the thing being looked for."""

    def reads_postgres() -> str:
        """This explanation is not evidence."""
        marker = "postgres"
        return marker

    assert "postgres" in code_without_prose(reads_postgres)


def test_comments_do_not_survive_the_round_trip() -> None:
    def commented() -> None:
        pass  # postgres, mentioned only in a comment

    assert "postgres" not in code_without_prose(commented)
