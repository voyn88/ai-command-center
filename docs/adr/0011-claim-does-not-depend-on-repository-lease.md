# ADR-0011: the execution-attempt claim does not depend on the repository lease

Status: Accepted for `VOYN-W0-AICC-SRV-04b-DEPENDENCY-FIRST`.

## Question

`SRV-04-SCOPE` names two entities that look alike and are not: **repository
lease** ("who may mutate repository R", AIOS platform, SRV-04a) and
**execution-attempt claim** ("who is executing attempt N of work item W",
AICC, SRV-04b). Before designing any compensation for platform-lease outages
in the claim path, this records whether that dependency exists at all. A
compensation mechanism for a dependency that isn't there is waste at best and
a second, weaker authority at worst.

## Proof of no dependency

Verified against the checked-out code, not assumed:

- **Different databases, no read path between them.** `repo_lease` lives in
  the AIOS platform database (`aios/migrations/versions/0010_repo_lease.py`).
  The claim protocol (`command_center/db/sql/0002_queue_claim.up.sql`) lives
  in AICC's single database. `grep -rn repo_lease command_center/` returns
  only comments (this file's own header, and two unrelated design-rationale
  references in `0005_backlog_store.up.sql` / `0006_backlog_planner.up.sql`
  that cite AIOS's lease-table review as prior art) — no table, view,
  function or query in AICC reads it.
- **`repository_id` is provenance, not a gate.** `work_item.repository_id` is
  documented as "OPAQUE PROVENANCE ONLY, read by NO decision in this file"
  and enforced by
  `tests/db/test_queue_claim.py::test_repository_id_is_provenance_and_gates_nothing`:
  two work items sharing a `repository_id` are claimed by two different
  workers concurrently and the test asserts **both** claims succeed
  (`first[0] and second[0]`). If claim gated on a shared repository, the
  second claim would have to wait or fail; it does neither.
- **Claimant identity needs no second authority.** The claimant is
  `session_user`, fixed by SCRAM at connect time. The protocol does not call
  `identity_assert()` and does not read `principal`, so it has no route to
  AIOS's identity/session state either — the same property that would have to
  exist before a lease check could even be attributed to a caller.
- **A claim-time lease check would be provably wrong, not just absent.** Even
  if wired up, a lease check at claim time is stale by the time the git
  command that needed it actually runs (the claim happens in one SQL
  transaction; the mutation happens in a later, separate subprocess). It
  would not prevent the corruption it's meant to prevent — it would just add
  latency and a second place that can be wrong.

One correction to the 0002 header while verifying this: it also says
`principal` "exists in NO database today... a design proposal, not a
deployed table." That's no longer true — `0003_worker_enrollment.up.sql`
creates `principal`, `principal_credential`, and `principal_event`. This
doesn't change the conclusion above: `0002_queue_claim.up.sql` still contains
no reference to `principal` outside its own comments (confirmed by grep), and
`work_attempt.claimed_by_role` — the column the header names as the future
join point — has no join to `principal` anywhere in the codebase today. The
staleness is noted here rather than silently left for the next reader to
trip over.

**Conclusion: no dependency exists.** No compensation for AIOS/repo_lease
outages is needed in the claim protocol. Claim exclusivity, stale-owner
redelivery, SIGKILL/partition recovery, durable-ack, bounded retry and DLQ
are entirely decided by facts local to the AICC database (visibility timeout
+ the `current_attempt_id` fence) and are unaffected by whether the lease
authority is reachable.

## Where a real dependency exists, and its explicit behavior

Repository *mutation* — not claim — does depend on the platform lease, and
that dependency lives at the mutation site, in the worker, not in the SQL
claim protocol:

- `command_center/worker/worktree_lease.py::blocking_lease` — a read-only
  preflight checked before a mutating dispatch writes into an isolated
  workspace.
- `command_center/worker/writer_lease.py::hold` — the full-lifecycle writer
  lease held from provisioning through `publish_run`.

Both are **fail-closed** when the lease authority is unreachable, and this is
deliberate, not incidental:

- `blocking_lease` treats every failure to reach `voyn-lease list` (missing
  tool, non-zero exit, unparseable output, timeout) as a blocking condition —
  its own docstring: "A guard that opens when it cannot see is not a guard."
- `writer_lease.hold` raises `WriterLeaseUnavailable` on an acquire failure;
  the caller must not provision a workspace or run an agent without a held
  lease.
- Both gate only on `VOYN_LEASE_DSN` being configured at all: a host with no
  lease authority configured has no lease to violate and is not blocked by a
  tool that was never wired up for it. Fail-closed applies exactly where a
  lease authority is expected to exist, never as a universal block.

**Why fail-closed and not fail-open:** the resource being protected — a live
git worktree — is not recoverable by retrying. `0002_queue_claim.up.sql`'s
own header states the asymmetry plainly: "A second writer on a live worktree
corrupts it irreversibly," versus queue redelivery, which "requires ONLY an
elapsed visibility timeout" because a stuck retry is cheap and a corrupted
worktree is not. When the lease authority cannot be reached, the safe default
is to refuse the mutating dispatch and let the queue's own bounded retry
redeliver the item later; nothing about a delayed dispatch is expensive
compared to an unrecoverable corruption.

## Consequences

- No new compensation layer is warranted in the claim protocol for
  platform/lease unavailability — one would duplicate `repo_lease` in a
  weaker, staler form exactly where `0002`'s header already warns against it.
- Platform-outage handling correctly lives at the mutation site
  (`worktree_lease` / `writer_lease`), which already fails closed with a
  bounded, surfaced reason and a retryable outcome, rather than failing the
  task outright.
- Any future claim-time visibility into lease state (e.g. scheduling hints)
  must be added as a join onto `work_attempt.claimed_by_role`, per `0002`'s
  own extension contract — a join, never a gate.

## Rejected alternatives

- **A claim-time `repo_lease` check.** Rejected: stale by the time the git
  command it's meant to protect actually runs, and it would create a second,
  weaker authority over repository mutation that duplicates SRV-04a.
- **Compensating for AIOS/lease-authority outages inside the claim protocol**
  (e.g. treating an unreachable lease authority as a reason to fail or delay
  a claim). Rejected: there is no dependency to compensate for. Claim
  correctness does not reference lease state in any form.
