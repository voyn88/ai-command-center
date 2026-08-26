"""Every mirrored table must have a reader that returns the stored shape.

Three tables have now hit the same trap. `digest_item` popped `refs_json` in
every public reader and slice 4 was rejected for shipping a reconciliation
nobody could run. `model_event` decodes the same way, and slice 6 shipped the
reader with the mirror only because slice 4's rejection was fresh. `audit_run`
decodes too, and slice 8 got it right for the same reason — memory.

Memory is what fails on the fourth table. This is the mechanical version: it
reads the authority's own source, finds which readers decode, and requires a
stored-shape reader wherever one does. A table that repeats the trap now says
so in CI instead of waiting for a reviewer to ask what an operator would call.

Deliberately structural rather than behavioural. Asking "does reconciliation
fail against the decoding reader?" needs a database and a row per table; asking
"does a stored reader exist, and does the reconciliation's own docstring point
at it?" needs neither and catches the same omission at the moment it is made.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from command_center.db.mirror_registry import mirror_classes

ROOT = Path(__file__).resolve().parents[2]
AUTHORITY = ROOT / "command_center/runtime/db"


def _mirrored_tables() -> dict[str, object]:
    """Every declared mirror's module, keyed by table — shared with the contract.

    Both suites used to carry their own `command_center/db/*_store.py` scan.
    Slice 9's acceptance defeated that rule by putting a mirror in a file with
    another name, so the rule now lives once, in `mirror_registry`, and is
    about what a class is rather than where it sits.
    """
    return {table: module for table, (_mirror, module) in mirror_classes().items()}


MIRRORED = _mirrored_tables()


def _returns_decoded(function: ast.FunctionDef) -> bool:
    """True when this reader hands back something other than the stored row.

    Two ways that happens, and the second cost a slice to notice:

    * **decoding** — the row passes through a `_decode_*` helper that pops a
      column (slice 4's trap, on `digest_item`, `model_event`, `audit_run`);
    * **projecting** — the reader selects an explicit column list rather than
      `*`, so a column the mirror needs is simply absent. `list_run_events`
      does this with `id`, and reconciliation pairing rows by key then sees
      `None` on every row. The first version of this gate looked only for
      decoders and passed it.
    """
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id.startswith("_decode"):
                return True
        # The third variant, and the one that got past this gate: decoding
        # written inline instead of in a helper. `list_proposal_evidence` does
        # `raw = item.pop("data_json")` and hands back `data` — a `SELECT *`
        # with no `_decode_` call anywhere, so both rules above pass it while
        # the column the mirror needs is gone. Any `.pop("<literal>")` on a row
        # is the same act as a decoder, whoever wrote it.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return True
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            statement = " ".join(node.value.split())
            if statement.upper().startswith("SELECT ") and " FROM " in statement.upper():
                selected = statement[len("SELECT ") : statement.upper().index(" FROM ")]
                if "*" not in selected and "COUNT(" not in selected.upper():
                    return True
    return False


def _reads_table(function: ast.FunctionDef, table: str) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if f"FROM {table} " in node.value or node.value.endswith(f"FROM {table}"):
                return True
            if f"FROM {table}\n" in node.value or f"FROM {table}{{" in node.value:
                return True
    return False


def _readers(table: str) -> tuple[list[str], list[str]]:
    """`(decoding, stored)` reader names across the authority package."""
    decoding: list[str] = []
    stored: list[str] = []
    for path in sorted(AUTHORITY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not _reads_table(node, table):
                continue
            # Only the readers an operator would actually call. The first
            # version of this rule matched any function containing a `SELECT
            # ... FROM <table>`, and promptly mis-classified `append_model_event`
            # — a write path that reads `model_entry` to check the model exists
            # and returns a decoded *event* — as a decoding reader of
            # `model_entry`. A write path is not a reconciliation entry point,
            # and the convention this package follows is `get_*` / `list_*`.
            if not node.name.startswith(("get_", "list_")):
                continue
            (decoding if _returns_decoded(node) else stored).append(node.name)
    return decoding, stored


@pytest.mark.parametrize("table", list(MIRRORED), ids=list(MIRRORED))
def test_a_table_whose_readers_decode_has_a_stored_reader(table: str) -> None:
    """The rule, stated once instead of remembered per slice.

    A decoding reader is fine — it is the right default for callers. What is
    not fine is *only* decoding readers: reconciliation compares what the
    authority stores against what the mirror stores, so fed a decoded row it
    reports every row divergent on the converted column, and the failure looks
    like a broken mirror rather than a wrong question.
    """
    decoding, stored = _readers(table)
    if not decoding:
        pytest.skip(f"{table}: no reader decodes, so any of them serves reconciliation")
    assert stored, (
        f"{table}: every reader decodes ({', '.join(sorted(decoding))}) and none returns the "
        "stored shape — reconciliation has no entry point. Add a `*_stored` reader beside them "
        "(see list_digest_items_stored / list_model_events_stored / list_audit_runs_stored)."
    )


@pytest.mark.parametrize("table", list(MIRRORED), ids=list(MIRRORED))
def test_the_reconciliation_points_at_the_stored_reader(table: str) -> None:
    """Where the trap exists, the warning must be where the mistake is made.

    An operator wiring the cutover gate reaches for the public reader first,
    because it is the one that exists. The reconciliation's own docstring is
    the last thing between them and a permanently-red gate, so it has to name
    the reader that works — and `help()` has to show it, which is why slice 8
    gave the closures their docstrings back.
    """
    decoding, stored = _readers(table)
    if not decoding:
        pytest.skip(f"{table}: nothing to warn about")

    module = MIRRORED[table]
    # This table's own reconciliation, not any of the module's. The first
    # version searched every `*divergence` attribute in the module and accepted
    # a match anywhere in it: slice 9's acceptance moved `list_events_stored`
    # out of `event_divergence`'s docstring and parked it in the motion's, and
    # the gate stayed green — while the operator who opens `help(event_
    # divergence)` reads nothing. `divergence_against` names each closure
    # `<table>_divergence`, so the right one is findable.
    own = [
        value
        for name, value in vars(module).items()
        if getattr(value, "__name__", None) == f"{table}_divergence"
    ]
    assert own, (
        f"{table}: no reconciliation named `{table}_divergence` in {module.__name__} — "
        "build it with `divergence_against`, which names the closure after its table."
    )
    doc = getattr(own[0], "__doc__", "") or ""
    assert any(reader in doc for reader in stored), (
        f"{table}: readers {sorted(decoding)} decode, and `{table}_divergence`'s own docstring "
        f"names none of {sorted(stored)}. The warning has to be readable where the mistake is "
        "made, which is `help()` at cutover time."
    )
