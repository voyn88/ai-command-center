# ADR-0011: split SRV-04 into the writer lease (04a) and the queue claim (04b)

Status: accepted, retroactively documented for `VOYN-W0-AICC-SRV-04-SCOPE`.

## Context

`SRV-04`'s wording conflated two invariants that look alike but are decided by
different facts, in different databases, with different recovery policies:

- "who may **mutate** repository `R`" — a writer-lease invariant.
- "who is **executing** attempt `N` of work item `W`" — a queue-claim
  invariant.

The ticket's acceptance criteria described only the second (exclusivity,
stale-owner rejection, SIGKILL/partition recovery, durable-ack, bounded retry,
DLQ). The first was left implicit, even though `SRV-03`/`SRV-05` and any
future removal of the single-writer constraint on a repository depend on it
being a stable, separately-reasoned-about foundation. `backlog_triage`
(`VOYN-W0-BACKLOG-RECONCILE-ALL`, 2026-08-20) flagged the conflation and
required the split recorded here.

Both halves were, in fact, already built and shipped under their own ticket
IDs before this ADR existed to name the split explicitly. This document does
not introduce new mechanism; it records the boundary between what already
shipped, so `SRV-03`/`SRV-05` and anything that follows can depend on the
right one without re-deriving it from five docstrings.

## Decision

Two authorities, composed at the mutation site rather than nested:

### SRV-04a — the repository writer lease

Authority for "who may mutate repository `R`" lives **outside AICC**, in the
external `voyn-lease` tool backed by `VOYN_LEASE_DSN` (the AIOS platform's
`repo_lease`, `aios/migrations/versions/0010_repo_lease.py` — `grep -rn
repo_lease` over an AICC checkout returns nothing, and must keep returning
nothing). AICC only consumes it:

- `command_center/worker/lease_client.py` — the one place the external tool's
  argv/identity shape is defined, shared by every caller so they cannot drift.
- `command_center/worker/writer_lease.py` — the full-lifecycle lease
  (`VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE`): acquired before workspace
  provisioning, renewed on a background thread through the agent run and any
  tests/lint it runs, released after publish. A lost renewal sets the same
  `lease_lost` event that a lost queue-visibility timeout would, cancelling
  the run through one mechanism rather than two.
- `command_center/worker/worktree_lease.py` — a deliberately read-only
  preflight (`VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE`) that refuses a
  *mutating* dispatch into a worktree another writer holds. It never acquires
  anything; a read-only run gets the read-only sandbox profile instead.
- `command_center/orchestrator/publish.py` — a short, repository-scoped lease
  held only around the `git push`, which is the one operation that genuinely
  needs one clone-wide writer at a time.

Scope is the task, not the repository (`_task_lease_scope`, `"<project>:
<backlog_task_id>"`, #365): two attempts of the *same* task still collide
correctly (they share one worktree by design), but two different tasks in the
same repository do not block each other now that each task has its own
worktree. Recovery requires **proof of death** (`--auto-takeover`, same-host
`pid`/`process_start` confirmation) because a second live writer in one
worktree corrupts it irreversibly.

### SRV-04b — the queue execution-attempt claim

Authority for "who is executing attempt `N` of work item `W`" lives **inside**
AICC's own PostgreSQL database: `command_center/db/sql/0002_queue_claim.up.sql`
(`work_item`, `work_attempt`, `work_result`, `work_event`; delivered by #311).
The claimant is `session_user` — established by SCRAM at connect time,
undeclarable by any argument — and a superseded attempt is refused by the
fence (`work_item.current_attempt_id`), not by a clock or a proof of death.
Recovery requires **only an elapsed visibility timeout** (`queue_reap`):
requiring proof of death here would mean a SIGKILLed worker on an unreachable
host blocks its task forever, which is the opposite of this protocol's
purpose.

### The seam between them

`work_item.repository_id` is opaque provenance, read by no decision in the
claim protocol — asserted by
`tests/db/test_queue_claim.py::test_repository_id_is_provenance_and_gates_nothing`,
which claims two items of the same repository from two different hosts and
requires both claims to succeed. A task whose work *mutates* a repository must
**additionally** hold the SRV-04a lease, checked at the mutation site,
immediately before and after each mutating operation — a claim-time lease
check would already be stale by the time a git command ran. Claim is
necessary and not sufficient; the two protocols compose, they do not nest, and
neither's tests depend on the other's schema.

## Consequences

- `SRV-03`/`SRV-05` and any future work to relax or remove the single-writer
  constraint on a repository must be scoped against SRV-04a's semantics
  (task-scoped, proof-of-death recovery) — not against the queue-claim's
  visibility-timeout semantics, and not by adding a lease check inside
  `queue_claim()`.
- A design that reads "one queue claim ⇒ one repository writer" (or the
  reverse) reintroduces the coupling this split exists to prevent, and should
  be rejected at review rather than accepted as a simplification.
- Work items still naming bare `SRV-04` should be re-filed against `04a` or
  `04b` explicitly; both are otherwise complete for their stated acceptance.

## Evidence

- `command_center/db/sql/0002_queue_claim.up.sql` — the queue-claim schema and
  its own "WHAT THIS IS NOT: the repository writer lease" section, which this
  ADR promotes to a first-class, indexed decision.
- `tests/db/test_queue_claim.py` — `test_repository_id_is_provenance_and_gates_nothing`,
  `test_queue_claim_takes_no_actor_argument`, `test_the_claimant_cannot_be_declared`.
- `command_center/worker/{lease_client,writer_lease,worktree_lease}.py`,
  `command_center/orchestrator/publish.py`, `tests/worker/test_writer_lease.py`.
- `docs/AIOS_BOUNDARY.md` — "Server work queue (SRV lane)" — names both
  authorities as accepted clients/protocols composed by
  `command_center/orchestrator/`.
- PRs #311 (`VOYN-W0-AICC-SRV-04b`), #365 (task-scoped lease), and the
  `VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE` /
  `VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE` backlog entries.
