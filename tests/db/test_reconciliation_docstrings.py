"""A reconciliation's prose belongs to the closure, not to the line above it.

Slice 7 replaced five module-level `def`s with closures built by
`divergence_against`, and their docstrings became `#:` comments above an
assignment: still in the source, still rendered by a documentation build, and
invisible to `help()`. Slice 8 handed the three warnings that matter most their
`__doc__` back — the ones telling an operator that reconciliation takes the
*stored* reader — and `tests/db/test_batch_stores.py` pinned those three by
name.

Three pinned by name is the arrangement the stored-reader trap was in before
`test_stored_reader_fitness.py` made it mechanical: correct today, and correct
on the next table only if whoever adds it remembers. This is the mechanical
version, and it works on the mistake rather than on its consequence. Prose
written above the assignment never reaches `__doc__`, so the operator in a REPL
at cutover time reads the generic fallback instead of the warning someone wrote
for them — and nothing anywhere says so, because the words are right there in
the file for every reviewer who looks.

Reconciliations are found by asking the objects, not by matching the call:
`divergence_against` stamps each closure with the table it was built from, so
one built through an aliased import is still one of these. Sources are parsed
only to locate an already-named assignment, which is the half of the question
Python cannot answer — a comment is not in the AST's nodes, and by runtime it
is gone entirely.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from command_center.db.table_mirror import MirroredTable
from tests.db.mirror_discovery import mirror_classes, modules_declaring_mirrors


def _reconciliations(module: object) -> dict[str, object]:
    """`{name: closure}` for every reconciliation this module holds."""
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(getattr(value, "mirrored_table", None), MirroredTable)
    }


def _modules_with_reconciliations() -> dict[str, object]:
    """Every `command_center.db` module that declares at least one.

    The candidate list is `mirror_discovery`'s, so this suite is not a third
    rule about which files matter: a module that builds a reconciliation
    imports `table_mirror` to get the factory, which is exactly what that
    module already looks for.
    """
    found: dict[str, object] = {}
    for module_name in modules_declaring_mirrors():
        module = importlib.import_module(module_name)
        if _reconciliations(module):
            found[module_name] = module
    return dict(sorted(found.items()))


MODULES = _modules_with_reconciliations()
MIRRORED = {table: module for table, (_mirror, module) in mirror_classes().items()}


def _is_prose(comment: str) -> bool:
    """True for a comment carrying words, false for a section rule.

    `# --- reads ---` above an assignment is layout, not documentation, and
    refusing it would teach nothing except to add a blank line.
    """
    return any(character.isalnum() for character in comment.lstrip("#:").strip())


def _attached_comment(lines: list[str], lineno: int) -> list[str]:
    """The comment block sitting directly on top of the statement at `lineno`.

    Directly: a blank line ends it. Python's own attachment rule for `#:`
    comments is the same one, and a comment separated by a blank line is not
    claiming to document what follows it.
    """
    block: list[str] = []
    index = lineno - 2
    while index >= 0 and lines[index].strip().startswith("#"):
        block.insert(0, lines[index].strip())
        index -= 1
    return [comment for comment in block if _is_prose(comment)]


@pytest.mark.parametrize("module_name", list(MODULES), ids=list(MODULES))
def test_no_reconciliation_is_documented_above_its_assignment(module_name: str) -> None:
    """The mistake itself, refused where it is made.

    Not "does this closure have a docstring" — the factory always supplies a
    fallback, so that question is answered `yes` by the very case this exists
    for. The answerable one is whether anybody wrote prose the runtime will
    never show, and a comment block resting on the assignment is that, whatever
    it says.
    """
    module = MODULES[module_name]
    source = inspect.getsourcefile(module)
    assert source, f"{module_name}: imported from no file, so nothing to read"
    lines = Path(source).read_text(encoding="utf-8").splitlines()
    built = _reconciliations(module)

    misplaced: list[str] = []
    for node in ast.parse("\n".join(lines)).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # Names, and the module's own: a re-exported reconciliation is
            # bound here by an `import`, has no assignment to sit above, and is
            # checked in the module that declares it.
            if not isinstance(target, ast.Name) or target.id not in built:
                continue
            comment = _attached_comment(lines, node.lineno)
            if comment:
                misplaced.append(f"{target.id} (line {node.lineno}): {comment[0]}")

    assert not misplaced, (
        f"{module_name}: prose above an assignment is a docstring `help()` cannot reach — "
        f"{'; '.join(misplaced)}. Pass it as `divergence_against`'s `doc` argument, which is "
        "read by the operator who needs it, in a REPL at cutover time."
    )


@pytest.mark.parametrize("table", list(MIRRORED), ids=list(MIRRORED))
def test_every_mirrored_table_has_a_reconciliation_that_names_it(table: str) -> None:
    """One per table, and each one able to introduce itself at runtime.

    `test_stored_reader_fitness.py` asks a sharper version of this question,
    but only of the tables whose readers decode — the rest it skips, and a
    table that reconciles nowhere at all would be skipped just as quietly.
    This one covers every declared table, and asks only what a reconciliation
    must be able to do for the operator holding it: name the table it reports
    on.
    """
    module = MIRRORED[table]
    spec = mirror_classes()[table][0].spec
    built = _reconciliations(module).values()
    own = [value for value in built if value.mirrored_table == spec]

    assert own, (
        f"{table}: {module.__name__} declares a mirror and no reconciliation built from its "
        "table. Build one with `divergence_against(SPEC)` — the cutover gate compares the "
        "authority against the mirror through it, and there is nothing else to call."
    )
    for reconciliation in own:
        doc = reconciliation.__doc__ or ""
        assert table in doc, (
            f"{table}: `{reconciliation.__name__}`'s docstring is {doc!r}, which does not name "
            "the table it reports on. Whoever reads it in a REPL is holding several of these."
        )
