"""The store contract for row-oriented tables mirrored into PostgreSQL.

Slice 1 mirrored `queue_entry`, whose contract is whole-list replacement: the
queue *is* its ordered list, so the mirror is set to exactly that list and
order is data. `command_center/queue_store.py` holds that contract.

Row-oriented tables are a different shape and deliberately get a different
protocol rather than one flattened to fit both. `owner_item` rows are created
one at a time, updated in place under an optimistic `version`, and never
reordered; a `replace_entries` contract would force a caller to read the whole
table to change one row, and would make a partial mirror indistinguishable from
a deletion. Two contracts that each say something true beat one that says
something vague about both.

What is shared is the property that matters for the migration gate: the
authority is compared against *every* mirror through the same shape, so adding
a destination cannot silently narrow the reconciliation that gates a cutover,
and an unreachable mirror is reported rather than counted as agreement.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RecordMirror(Protocol):
    """A store that mirrors keyed domain records.

    `upsert` rather than `insert`, because the backfill is expected to run more
    than once — once per operator, once after a rollback and re-advance — and
    an insert-only mirror would fail the second run on rows it wrote itself.
    """

    #: Stable identifier used in divergence records, so an operator can tell
    #: *which* mirror disagreed without reading the code.
    name: str

    def upsert(self, record: dict) -> None:
        """Write `record`, replacing any existing row with the same id."""

    def list_records(self) -> list[dict]:
        """Every mirrored record, shaped like the authority's own row."""
