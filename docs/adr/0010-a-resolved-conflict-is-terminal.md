# ADR 0010 — A resolved conflict is terminal; there is no reopen edge

Status: **Accepted, implemented.** The rule this ADR records is already enforced in
`command_center/runtime/db/conflict.py` and pinned by
`tests/db/test_conflict_store.py::test_a_resolved_conflict_is_terminal`. This ADR is written
**after** the fact, to give a decision that has so far lived only in code comments and a test
docstring an architecture-tier record, so it cannot be silently re-opened by a future change that
never reads those comments.

## Context

The Wave-2 Conflicts/Incidents engine (`VOYN-W2-CONFLICT`, #273) moves a conflict through
`open → mitigating → resolved`. Whether a `resolved` conflict can ever move again — reopen back to
`open`, or clear its `resolved_at` timestamp for a fresh mitigation cycle — was never settled by
that engine's original design and was left as an open question tracked as
`VOYN-W0-AICC-CONFLICT-REOPEN-DECISION`.

The question was answered once already, but only implicitly. `command_center/runtime/db/conflict.py`
declares `CONFLICT_TRANSITIONS["resolved"] = frozenset()` — no outbound edge — which makes
`resolved` terminal by construction. Slice 3 of the PostgreSQL mirror migration (#288) then wrote an
acceptance story that assumed the opposite: its docstring in
`command_center/db/conflict_store.py` claimed a reopened conflict would see `resolved_at` cleared
back to `NULL`, and offered a test as evidence. Independent review disproved the claim by running
the real writer rather than trusting the test's hand-built rows: the "clearing" branch in
`_conflict_transition` (`command_center/runtime/db/conflict.py:370-379`) is unreachable, because the
transition allowlist checked just above it (`command_center/runtime/db/conflict.py:362-366`)
already rejects every edge out of `resolved` before that branch could run. The false claim was
corrected in the same slice and pinned by
`tests/db/test_conflict_store.py::test_a_resolved_conflict_is_terminal`, but the decision itself —
that this is the intended lifecycle, not an accident of an unfinished feature — was never lifted out
of a test docstring into a citable record. `VOYN-W0-AICC-CONFLICT-REOPEN-DECISION`'s dispatch
cascade then stalled entirely on an unrelated infrastructure fault
(`VOYN_LEASE_REFUSED`, fixed by `VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM`, #358) before it could
produce this record, leaving the question formally open even though the code had already answered
it.

## Decision

**A `resolved` conflict is terminal. There is no `resolved -> open` or `resolved -> mitigating`
edge, now or as a planned future addition.**

1. `CONFLICT_TRANSITIONS["resolved"]` (`command_center/runtime/db/conflict.py:62`) stays the empty
   set. `_conflict_transition` raises `InvalidConflictTransitionError` for any attempted move out of
   `resolved` (`command_center/runtime/db/conflict.py:360-366`), and
   `update_conflict_fields`/`set_mitigation`/`assign_conflict` all raise `ConflictResolvedError` or
   return not-found rather than touch a resolved row
   (`command_center/runtime/db/conflict.py:304-307`, `command_center/conflicts/service.py:148-177`).
2. There is no `POST /conflicts/{id}/reopen` route, and none should be added
   (`command_center/api/conflict_routes.py`). A resolved conflict is frozen at every tier: db, service,
   and HTTP surface agree.
3. The dead "clearing" branch in `_conflict_transition`
   (`command_center/runtime/db/conflict.py:370-379`) is left in place, not deleted, because it is the
   correct behaviour *if* this decision is ever reversed by a future ADR that opens the
   `resolved -> open` edge. It is unreachable today and is exercised by no live call path.
4. Recurrence of the same underlying problem after resolution is **a new conflict**, not a reopened
   one. `get_conflict_by_source_ref` / the `IncidentOpened` intake dedup on `source_ref`
   (`command_center/runtime/db/conflict.py:208-223`) only suppresses a duplicate open against an
   existing *unresolved* row from the same source; a fresh `IncidentOpened` for a `source_ref` whose
   only existing row is `resolved` opens a new conflict rather than resurrecting the closed one. This
   is the intended behaviour, not a gap: it keeps `resolved_at` meaningful as "when this specific
   conflict was closed" rather than a value that can be clobbered by an unrelated later event.

## Consequences

- `VOYN-W0-AICC-CONFLICT-REOPEN-DECISION` (and its retry, this task) is closed: "resolved is
  terminal" was correct when independent review pinned it during #288 and remains the decision.
- The PostgreSQL mirror's whole-row upsert in `command_center/db/conflict_store.py` keeps its
  stated rationale (a field-by-field mirror would need to un-write a resolution the authority
  withdrew, `command_center/db/conflict_store.py:36-39`) as a documented hedge against a future
  reversal, not as a description of current behaviour.
- `test_a_resolved_conflict_is_terminal` is now backed by an architecture-tier record in addition to
  its own docstring; the next change proposing `resolved -> open` must supersede this ADR, not just
  edit or delete that test.

## Non-goals

- **Adding a reopen path.** No route, transition, or UI affordance for resurrecting a resolved
  conflict is introduced or planned by this record.
- **Changing dedup behaviour on `source_ref`.** Whether a *new* conflict opened against a
  previously-resolved `source_ref` should carry a back-reference to the closed one is a product
  question this ADR does not decide.

## References

- `command_center/runtime/db/conflict.py` — `CONFLICT_STATUSES`, `CONFLICT_TRANSITIONS`,
  `InvalidConflictTransitionError`, `ConflictResolvedError`, `_conflict_transition`,
  `get_conflict_by_source_ref`
- `command_center/conflicts/service.py` — `resolve_conflict`, `assign_conflict`, `set_mitigation`
- `command_center/api/conflict_routes.py` — the `/api/v1/conflicts` surface (no reopen route)
- `command_center/db/conflict_store.py` — the PostgreSQL mirror's whole-row upsert rationale
- `tests/db/test_conflict_store.py::test_a_resolved_conflict_is_terminal` — the executable pin
- PR #273 (`VOYN-W2-CONFLICT`) — the original engine; PR #288 (`VOYN-W0-AICC-SRV-01B` slice 3) —
  where the false reopen claim was made and then disproved in review
