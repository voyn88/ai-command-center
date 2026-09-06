"""`reconciled(stage)`, extracted once instead of reinvented per table.

**The property this exists for.** Every mirror in this migration writes whole
rows (`table_mirror.py`'s `upsert` docstring: "never the changed columns"), so
a later write to a row a caller updates repairs whatever an earlier write to
that same row left behind — including a mirror write that silently failed.
Comparing authority and mirror once, after a batch of writes, cannot tell "every
write landed" from "one was lost and a later one for the same row covered it":
both end in the identical row. Reconciling after *each* authority write closes
that gap, because the loss is checked before anything later has a chance to
repair it, and the failing `stage` names which write it was.

**Found twice, accepted as coverage both times.** `digest_item` (slice 4) and
`contact`/`message` (slice 5) each shipped an end-to-end reconciliation test
whose docstring claimed to prove every write mirrors, checked once at the end
of a short scenario. `digest_item` has no update path — a day is deleted and
its rows re-created, never revised in place — so the gap in its test never
had a row shaped to expose it. `contact` does update in place
(`update_contact_fields`), and independent review found the gap the same way
twice: by failing one mirror write in turn and watching an end-state check
stay clean (`tests/db/mirror_probe.py`'s docstring records the second
finding). `test_networking_store.py`'s
`test_reconciliation_is_clean_for_rows_the_application_actually_wrote` is the
rewrite that came out of it — reconcile after every authority write, not once
at the end — and every mirrored table with an update path added from slice 9
on (`council`, `run`, `completion`, `provenance`, `proposal`, the batch
stores, `run_children`, `model_registry`, `execution`) restated the same
closure rather than referencing one place that has it. This module is that
place, so the count stops at thirteen restatements instead of growing with
every table this migration or the next one adds.

**Not applied to slices 2 and 3.** `owner_item` and `conflict` both update
rows in place and both still reconcile only at the end of their tests
(`test_owner_item_store.py::test_reconciliation_is_clean_for_a_row_the_application_actually_wrote`,
`test_conflict_store.py::test_reconciliation_is_clean_for_rows_the_application_actually_wrote`)
— the same gap `contact` had, left open on the same reasoning that keeps
`digest_item` as it is: the cost of a rewrite-only PR against an accepted
slice is higher than the benefit while nothing else is touching that file.
Bring them to this form the next time either file changes for another
reason, not before. Tracked as `VOYN-W0-AICC-MIRROR-STAGED-RECONCILE-PATTERN`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

#: One table's reconciliation: the production `divergence` function paired
#: with a zero-argument reader of the *current* authority rows (called fresh
#: every stage, never a snapshot taken when the check was built) and the
#: mirror to compare it against.
StageCheck = tuple[
    Callable[[Iterable[dict], object], list],
    Callable[[], Iterable[dict]],
    object,
]

__all__ = ["reconciled_stage"]


def reconciled_stage(*checks: StageCheck) -> Callable[[str], None]:
    """Build a `reconciled(stage)` assertion from one or more `StageCheck`s.

    One check per mirrored table a scenario touches — `council`'s four tables
    pass four, a single-table test passes one. Call the result after *every*
    authority write, not only at the end: that is the whole fix, and nothing
    here enforces a caller doing so. Each check's reader runs at call time, so
    two calls to the same `reconciled` see two different snapshots of the
    authority as a scenario progresses.
    """

    def reconciled(stage: str) -> None:
        for divergence, read_authority, mirror in checks:
            assert divergence(read_authority(), mirror) == [], stage

    return reconciled
