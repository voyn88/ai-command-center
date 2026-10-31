# ADR 0012 — A resolved conflict is terminal

Status: **Accepted, implemented.** This ADR is written after the fact, to give the conflict
lifecycle's terminal-state rule (`VOYN-W2-CONFLICT`) an architecture-tier record. The code it
describes is already on `main`: `command_center/runtime/db/conflict.py`
(`CONFLICT_TRANSITIONS`, `_conflict_transition`) and `command_center/db/conflict_store.py` (the
PostgreSQL mirror).

## Context

A conflict moves through an explicit status allowlist: `open → mitigating → resolved`. The
question this ADR answers is what happens at the far end of that chain — specifically, whether
`resolved` is an ordinary state a conflict can leave, or a terminal one.

An earlier slice of this same migration (PR #288) answered that question wrong, and did so in a
way worth recording precisely because of *how* it went wrong: its acceptance story asserted that
`resolved_at` is cleared back to `NULL` when a resolved conflict reopens. That claim was never
backed by a test that ran the real writer — it was read off a "clearing" branch inside
`_conflict_transition` without noticing that the allowlist check immediately above that branch
makes it unreachable, and separately "proved" against two hand-built dicts upserted straight into
the PostgreSQL mirror, which is data the writer itself can never produce. Independent review ran
the actual writer and disproved it: a resolved conflict cannot transition anywhere.

## Decision

### 1. `resolved` has no outgoing edges

```python
CONFLICT_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"mitigating", "resolved"}),
    "mitigating": frozenset({"open", "resolved"}),
    "resolved": frozenset(),
}
```

`_conflict_transition` checks the caller's `expected_version` first (a stale writer always loses
as a stale writer, regardless of status), then checks `new_status in CONFLICT_TRANSITIONS[row["status"]]`.
Because `resolved`'s edge set is empty, every attempt to move a resolved conflict anywhere —
including back to `open` — raises `InvalidConflictTransitionError` before any column is touched.
`update_conflict_fields` enforces the same rule independently for the mutable owner/mitigation
fields, raising `ConflictResolvedError` rather than silently no-op'ing.

This is pinned as a fact, not left in prose, by `test_a_resolved_conflict_is_terminal`
(`tests/db/test_conflict_store.py`): it resolves a conflict and then asserts that both `open` and
`mitigating` are rejected as illegal transitions out of it. The next person who opens a
`resolved -> open` edge needs this test to fail, because the PostgreSQL mirror's `resolved_at`
handling and the intake dedup below (Decision 2) all move together with this invariant.

### 2. The intake dedup does not distinguish an open conflict from a resolved one — and that is a separate, weaker claim than "reopen is impossible"

`ConflictIntake.on_incident_opened` deduplicates a redelivered `IncidentOpened` by looking up
`get_conflict_by_source_ref`, which has no status filter: it returns the most recent conflict for
that `source_ref` regardless of whether it is `open`, `mitigating`, or `resolved`. If a match
exists at all, intake returns `None` and opens nothing.

The observable consequence is: a fresh `IncidentOpened` against a `source_ref` whose only existing
row is `resolved` opens **no** conflict — neither a resurrection of the old row (impossible per
Decision 1) nor a new one (prevented by the status-blind dedup lookup). The incident is silently
dropped at the intake boundary. This is the current, tested behaviour, not an inferred one:
`test_incident_intake_does_not_resurrect_a_resolved_conflict`
(`tests/test_conflict_intake.py`) resolves a conflict, redelivers the same incident, and asserts
exactly one row remains for that `source_ref`, still `resolved`.

The previous version of this decision point claimed, without a test citation, that a fresh
incident "opens a new conflict rather than resurrecting the old one" and called that "the intended
behaviour, not a gap." That claim was never verified and does not match what the code does — it
silently swallows the redelivery instead. Whether *that* is the intended long-term behaviour (as
opposed to, say, opening a fresh conflict once the prior one has resolved) is a real open product
question this ADR does not resolve; it is called out here, hedged, and pinned by the test above so
it cannot drift again without the test failing.

### 3. The unreachable "clearing" branch in `_conflict_transition` stays, labelled rather than deleted

```python
if new_status == "resolved":
    fields["resolved_at"] = now
elif row["status"] == "resolved":
    # Unreachable while `resolved` is terminal: the allowlist check above
    # rejects every edge out of it, so no call reaches here with a resolved
    # row. ...
    fields["resolved_at"] = None
```

This branch is exercised by no live call path today — Decision 1 makes it unreachable. It is kept
rather than deleted because removing it would silently drop the `resolved_at` reset the day someone
*does* open a `resolved -> open` (or `resolved -> mitigating`) edge, and that person would have no
signal that the reset needs re-adding.

This is stated as a hedge, not a guarantee: nothing here has verified that clearing `resolved_at`
is the *correct* handling for a future reopen — that would depend on decisions this ADR does not
make (does reopening need a new `resolved_at`-shaped audit trail instead? does the mirror's
divergence check need to change?). The comment on the branch and this ADR both say only that the
code is annotated and left in place *as a starting point*, not that it is pre-verified for a
change no one has designed yet. Whoever opens that edge inherits `test_a_resolved_conflict_is_terminal`
and this branch together, and must re-justify — not merely uncomment — the reset.

## Consequences

- `resolved` is a true terminal state at the repository layer: no caller, however constructed, can
  move a resolved conflict's status or mutable fields. The business policy layer (service tier)
  never has to defend against a resolve-then-mutate race, because the repository already refuses
  it structurally.
- A redelivered `IncidentOpened` against an already-resolved conflict is currently a silent no-op,
  not a fresh conflict. If a future change wants a new incident on the same `source_ref` to open a
  new conflict once the prior one resolved, that requires an explicit status-aware change to
  `get_conflict_by_source_ref` or its caller, plus a new pinning test — it is not the current
  behaviour and must not be asserted as such without one.
- Any future decision to allow `resolved -> open` (or any edge out of `resolved`) must supersede
  this ADR explicitly, re-examine the dead branch in Decision 3 on its own merits, and update
  `test_a_resolved_conflict_is_terminal` and `test_incident_intake_does_not_resurrect_a_resolved_conflict`
  accordingly — not simply delete them.
