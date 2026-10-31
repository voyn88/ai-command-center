# ADR 0011 — Governed autonomy: chain-of-responsibility for 3 critical zones

Status: **Accepted.** Documentation-and-fitness-test addendum to
[ADR 0005](0005-autonomy-proposal-foundation.md); introduces no new runtime
code. It names, as an explicit contract, a chain-of-responsibility rule that
the proposal lifecycle already enforces mechanically but that no document
previously stated as a governance rule in its own right
(`VOYN-MIN-WOW-3`, governed-autonomy).

## Context

ADR 0005 gives every autonomy proposal a deterministic risk classification and
a state machine, and its addenda (F1/F2, INTEGRATION-REMEDIATION-002) close
specific policy-authority and atomicity gaps. What none of that prose states
directly is a **chain-of-responsibility rule**: for the highest-consequence
actions the system can take, who — or what — is accountable at each point the
action could still be stopped, and what happens if that party is silent,
wrong, or absent. "The code enforces it" is not the same as "the rule is
written down and named," and a rule that is only implicit in five call sites
cannot be reviewed, audited, or taught to a new contributor in one read.

This ADR names exactly **3 critical zones** — the only points in the system
where an autonomous decision can produce an effect that a human cannot
trivially undo by rejecting a proposal — and states the chain-of-responsibility
rule for each. It changes no code; §"Verification" adds a fitness test that
fails if the code and this document drift apart.

## Decision

### Why these 3 zones, and not others

`autonomy.classify_risk` only ever assigns `CRITICAL` to `MERGE`, and only
`TASK_EXECUTION` starts at `HIGH` (`TASK_CREATION`, `PRIORITY_CHANGE`,
`DEPENDENCY_LINK` start `LOW` and mutate metadata, not a repository or a
running process). Between them, `MERGE` and `TASK_EXECUTION` are the only
proposal *kinds* whose default risk crosses the human-gate line
(`RiskLevel.HIGH`/`CRITICAL`, ADR 0005 §4). A third zone cuts across both: the
**policy that decides whether zone 1 or 2 ever reaches a human or an
auto-approval at all**. A correct execution boundary guarding a
maliciously-widened or corrupted policy is no boundary. These are the 3
critical zones:

| # | Zone | Why it is critical | Governing code |
|---|---|---|---|
| 1 | **Execution** — launching a run that mutates a working tree or repository state | Once a process starts it can write, install, or call out; a rejected proposal cannot un-run it | `ProposalKind.TASK_EXECUTION` (`RiskLevel.HIGH`), `ExecutionCenterAPI.start_run` |
| 2 | **Merge / publish** — moving a branch into a protected target | Irreversible-by-default; the one action every other safety layer in this repo (ADR 0004, ADR 0010) treats as the point of no return | `ProposalKind.MERGE` (`RiskLevel.CRITICAL`), the completion orchestrator / guarded publisher |
| 3 | **Policy authority** — the `AutonomyPolicy` that decides what zone 1 and 2 are even eligible to do | A widened or forged policy silently defeats zones 1 and 2 without touching either's code path | `AutonomyPolicy.intersect`, `AutonomyEngine._resolve_policy` |

### The chain-of-responsibility rule, per zone

Each zone is a **chain of handlers**, in the Gang-of-Four sense: a proposal
passes through an ordered sequence of parties, any one of which may block it,
and none of which may be skipped, reordered, or silently bypassed by the one
after it. "Confirmed" means: named below, and checked by
`tests/architecture/test_governed_autonomy_chain_of_responsibility.py`
(§"Verification") against the real constants and guard clauses, not just
prose.

**Zone 1 — Execution.**

1. *Proposer* (evidence source) — supplies non-fabricated evidence; a proposal
   with no evidence, or a stale one, is blocked before risk is even relevant
   (`evaluate_eligibility`, checks 3–4).
2. *Approver* — for `HIGH` risk, either an explicit human `approve(actor=...)`
   (non-empty actor enforced), or an auto-approval strictly within the
   *persisted* proposal's own policy ceiling — a runtime-supplied policy may
   only restrict that ceiling, never raise it (`AutonomyPolicy.intersect`,
   the F1 invariant). The approver of record for an auto-approval is
   `"policy:auto"`, itself an audited actor value, not a silent default.
3. *Execution machinery* — dispatch only occurs from `APPROVED`, only if the
   effective policy's `allow_execution_dispatch` is explicitly true, and only
   after the actor, action digest, and evidence digest are re-verified against
   what was approved (`AutonomyEngine.dispatch`). It hands the caller a
   dry-run plan; it never launches anything itself. The actual run is then
   started through `ExecutionCenterAPI.start_run(confirmed=True)`, under the
   isolated per-run `aicc-agent` UID (ADR 0010) — never as the proposing
   party's own privileges.
4. *Confirmer* — `confirm_execution` accepts only a real, existing run whose
   `repository_path`/`project`/`task_type`/`expected_branch`/`prompt` match the
   authorised payload and that was created after dispatch; a foreign or
   pre-existing run is refused and audited, and the proposal is left
   `DISPATCHED` rather than laundered into `EXECUTED`.

**Zone 2 — Merge / publish.**

1. *Proposer* — same evidence discipline as zone 1.
2. *Approver* — `CRITICAL` risk is never auto-approvable under any policy
   (`AutonomyPolicy.__post_init__` clamps a policy that tries;
   `may_auto_approve` refuses `CRITICAL` outright). A human `actor` is
   structurally mandatory, not merely the default.
3. *Execution machinery* — the proposal can reach `DISPATCHED`, but the agent
   principal that produced the proposal is never the principal that performs
   a merge: commit/push/merge authority belongs exclusively to the completion
   orchestrator / guarded publisher (ADR 0005 §6, ADR 0010's publisher/agent
   Unix-principal split). No dispatch of a `MERGE` proposal grants an agent
   push authority it did not already have.
4. *Confirmer* — a durable merge-result evidence route does not exist yet
   (`confirm_execution` refuses `MERGE` with `EXECUTION_MISMATCH` today, ADR
   0005 "Known limitations"). Until it does, a dispatched `MERGE` proposal
   cannot self-report success; the completion pipeline's own state machine
   (ADR 0004) remains the sole source of "merged," which is a stricter rule
   than the other kinds get, not a gap — it is documented here so it is not
   mistaken for an oversight in a future change.

**Zone 3 — Policy authority.**

1. *Policy author* — whoever supplies `policy` at `create_proposal` time sets
   the ceiling that governs that proposal's entire life; it is persisted at
   creation (`proposal.policy_json`) and is what every later step is measured
   against.
2. *Every later caller* (`assess`, `dispatch`) — may supply its own `policy`,
   but `_resolve_policy`/`dispatch` always compute `stored.intersect(runtime)`:
   the persisted policy is authoritative and a later caller can only shrink
   it, never widen it. A missing or malformed stored policy resolves to the
   fully-closed default (`AutonomyPolicy()`), so silence fails closed, not
   open.
3. *Auditor* — every dispatch records non-sensitive **fingerprints** of the
   persisted, runtime, and effective policy (`policy_fingerprint`), so a
   reviewer can later prove which policy actually gated a given action without
   the audit log itself becoming a place to leak policy contents.

## Consequences

**Positive.** The 3 zones and their per-role handlers are now a named,
citable contract instead of an inference a reader has to reconstruct from five
files. A future change that lets an agent merge directly, or lets a runtime
policy widen a persisted one, is now a legible violation of a stated rule, not
just a diff that happens to touch guarded code.

**Trade-off accepted.** This ADR intentionally does not introduce role-based
identities (e.g. a typed `Approver` distinct from a free-text `actor` string).
`actor` remains an audited but unauthenticated string, as in ADR 0005; adding
real principal/role enforcement is future work and would be its own ADR, not a
documentation addendum.

## Verification

`tests/architecture/test_governed_autonomy_chain_of_responsibility.py` checks
that this document and the code cannot silently drift apart:

* this file names all 3 zones and cites `TASK_EXECUTION`, `MERGE`, and
  `AutonomyPolicy.intersect`;
* `RiskLevel` classifies `MERGE` as `CRITICAL` and `TASK_EXECUTION` as `HIGH`
  (`autonomy._BASE_RISK`);
* `AutonomyPolicy(auto_approve_max_risk=RiskLevel.CRITICAL).may_auto_approve
  (RiskLevel.CRITICAL)` is `False` — critical risk is never auto-approvable
  under any configuration;
* `AutonomyPolicy.intersect` never grants a capability absent from either
  input (property-checked over enabled/allowed_kinds/dispatch/ceiling);
* `approve`, `dispatch`, and `confirm_execution` on `AutonomyEngine` all raise
  on an empty actor.

Together with the existing `tests/test_autonomy_domain.py` and
`tests/test_autonomy_service.py` suites (unchanged by this ADR), this closes
the loop from "written down" to "confirmed."
