"""`reconcile_stage.reconciled_stage`, proved against the property it exists for.

No PostgreSQL here: `mirror_support.divergence` is pure, and a `Mirror` that
keeps its rows in a `dict` is a faithful enough double of a real one — it has
exactly one behaviour a real mirror does not, a way to skip a call to `upsert`
to stand in for a dual-write hook swallowing an exception, which is the one
thing this file needs to control that a real PostgreSQL server would not let
it.
"""

from __future__ import annotations

import functools

import pytest

from command_center.db.mirror_support import divergence as _divergence
from tests.db.reconcile_stage import reconciled_stage

#: `mirror_support.divergence` takes `columns` positionally, the way a real
#: table's declaration binds it (see `table_mirror.divergence_against`); the
#: fixture rows below only ever carry `id` and `status`.
divergence = functools.partial(_divergence, columns=("id", "status"))


class Mirror:
    """A mirror's `upsert`/`list_records`, minus everything PostgreSQL-specific."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def upsert(self, record: dict) -> None:
        self.rows[record["id"]] = dict(record)

    def list_records(self) -> list[dict]:
        return list(self.rows.values())


def test_end_state_reconciliation_cannot_see_a_write_a_later_one_repairs() -> None:
    """The structural property `VOYN-W0-AICC-MIRROR-STAGED-RECONCILE-PATTERN`
    records: a later whole-row write to the same key hides an earlier one that
    never reached the mirror, because both leave the identical row behind.
    """
    mirror = Mirror()
    authority: dict[str, dict] = {}

    authority["x"] = {"id": "x", "status": "open"}
    # The dual-write hook's mirror call raised and was swallowed — nothing
    # written here, exactly like a lost write in production.

    authority["x"] = {"id": "x", "status": "closed"}
    mirror.upsert(authority["x"])  # this write succeeds and covers the whole row

    assert divergence(list(authority.values()), mirror) == []


def test_reconciled_stage_catches_the_same_loss_with_an_address() -> None:
    """The fix: check after the write that was lost, before anything later
    can repair it."""
    mirror = Mirror()
    authority: dict[str, dict] = {}
    reconciled = reconciled_stage((divergence, lambda: list(authority.values()), mirror))

    authority["x"] = {"id": "x", "status": "open"}
    # Mirror write lost, same as above.

    with pytest.raises(AssertionError, match="x created"):
        reconciled("x created")

    # The later, successful write happens after the failure already surfaced.
    authority["x"] = {"id": "x", "status": "closed"}
    mirror.upsert(authority["x"])
    reconciled("x closed")


def test_reconciled_stage_reads_the_authority_fresh_on_every_call() -> None:
    """Not a snapshot taken once when `reconciled` was built: each call must
    see whatever the authority holds at that moment, or a check built early in
    a scenario would silently stop meaning anything for the writes after it."""
    mirror = Mirror()
    authority: dict[str, dict] = {}
    reconciled = reconciled_stage((divergence, lambda: list(authority.values()), mirror))

    authority["a"] = {"id": "a", "status": "open"}
    mirror.upsert(authority["a"])
    reconciled("a created")  # would raise if this saw an empty authority

    authority["b"] = {"id": "b", "status": "open"}
    mirror.upsert(authority["b"])
    reconciled("b created")  # would raise if this only re-checked "a"


def test_reconciled_stage_composes_more_than_one_table() -> None:
    """`council`'s test needs four checks in one `reconciled`; this is the
    shape that makes that possible without a bespoke closure."""
    parents = Mirror()
    children = Mirror()
    parent_rows: dict[str, dict] = {}
    child_rows: dict[str, dict] = {}
    reconciled = reconciled_stage(
        (divergence, lambda: list(parent_rows.values()), parents),
        (divergence, lambda: list(child_rows.values()), children),
    )

    parent_rows["p1"] = {"id": "p1"}
    parents.upsert(parent_rows["p1"])
    reconciled("parent created")

    # The child's check alone should be enough to catch a lost child write,
    # even though the parent side is clean.
    child_rows["c1"] = {"id": "c1", "parent_id": "p1"}
    # children.upsert intentionally not called: a lost write.

    with pytest.raises(AssertionError, match="child created"):
        reconciled("child created")
