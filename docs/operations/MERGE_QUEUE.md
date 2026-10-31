# Merge queue on `main`

Serialized integration for a fleet that develops in parallel
(VOYN-W0-AICC-MERGE-QUEUE-ENABLE). Owner-enabled 2026-08-23, confirmed through
the GitHub API.

## What is switched on

Branch protection on `main`:

- required checks: `Final merge gate`, `Acceptance gate (independent verdict on
  exact SHA)`;
- `strict: true` — a branch must be current with `main` before it can be
  queued;
- required approvals: **0**, deliberately. Authors and review agents run under
  one account, so a GitHub-approval requirement is structurally unsatisfiable
  here; acceptance is the independent-review marker keyed to the exact head SHA
  (`review:<task>:<pr>:<head_sha>:<policy>`), enforced by the acceptance-gate
  check above;
- force-push and branch deletion: denied; admins enforced.

Merge queue on `main`: method `SQUASH`, `maximumEntriesToBuild: 5`.

`.github/workflows/ci.yml` carries the `merge_group` trigger the queue needs;
without it every queue entry would sit until it timed out. Secrets are
reachable on that trigger, so every step that receives one first asserts the
queued code was authored here (`scripts/assert_trusted_head_repository.py`,
pinned by `tests/test_release_gate_policy.py`).

`scripts/enable-branch-protection.sh` re-asserts the protection rule only. The
queue toggle itself lives in Settings → Branches and is owner-only; the script
cannot turn it on or off.

## What it buys, and why not a manual batch

The concern it answers: N branches cut from different states of `main` are
individually green and still break `main` when they land on top of each other.
Half of that concern was already closed — a stale review cannot pass changed
code, because the review key contains the exact head SHA and `_pr_is_mergeable`
requires the marker on the *current* head. The other half is real, and the
queue is the barrier: entries are tested as the **prospective merged result**
(this PR on top of everything ahead of it), landed one at a time, and an entry
that fails that combined build is dropped without stopping the entries around
it.

The alternative — accumulate non-overlapping tasks, land them by hand, start
nothing new until the batch is in — gives the same protection and costs the
fleet its parallelism. The queue barriers only the *entrance* to `main`;
development keeps running.

## What the fleet does about it

`command_center/orchestrator/review_merge.py` (`merge_once`):

- an entry already in the queue is a **free wait** (`awaiting_merge_queue:...`):
  no second `gh pr merge`, and no merge action out of `max_per_tick`, so
  queued entries cannot starve the accepted PRs behind them;
- a queued entry is **never branch-updated**, even when GitHub reports it
  BEHIND. `gh pr update-branch` pushes a new head, which would evict it from
  the queue and invalidate its head-keyed review — a full re-review and re-run
  to get back to where it already was. The queue, not the branch, is where a
  queued PR is tested against what is ahead of it;
- DONE is only ever claimed from the **target-branch merge commit**
  (`_merged_target_sha`). `gh pr merge` exiting 0 on a queue-protected repo
  means *enqueued*, not merged;
- the ejected half of a conflicting pair falls out of the queue and shows up as
  BEHIND (branch-updated, then re-reviewed and re-queued on its new head) or
  DIRTY (`branch_dirty_needs_rebase` — a real conflict, left for a rebase). It
  never reaches DONE and never lands.

Nothing waits on the queue but the queue. A task's repo writer lease — the
thing the planner's WIP limit actually counts (`backlog_dispatch` counts live
`repo:%` leases, `command_center/db/sql/0006_backlog_planner.up.sql`) — is
released at ingest, when the PR is published and the task becomes
READY_TO_REVIEW (`0011_backlog_ingest_requires_pr.up.sql`). A PR sitting in the
merge queue therefore holds no WIP slot and no repo lease: the planner keeps
dispatching and the workers keep building while `main` is entered one entry at
a time.

Queue membership is read with the same `repository.mergeQueue(branch:).entries`
query `scripts/assert_independent_acceptance.py` already runs on every
`merge_group` event — a surface proven against the live repository — and the
snapshot is cached per branch for the tick, so a batch of queued entries costs
one read, not one per PR. Every inconclusive answer — lookup failure,
unparseable body, GraphQL `errors`, a branch with no queue — is treated as "not
queued", i.e. exactly the pre-queue behaviour. Queue awareness can only remove
redundant actions; it can never block a merge that would otherwise happen.

## Verification status

Executable, in `tests/db/test_review_merge.py` (all four fail if the queue
check is removed from `merge_once`):

- `test_a_pr_the_queue_is_already_building_is_a_free_wait`
- `test_a_queued_entry_is_never_torn_out_of_the_queue_by_a_branch_update`
- `test_a_queued_entry_leaves_the_merge_budget_to_the_next_task`
- `test_the_ejected_half_of_a_conflicting_pair_neither_lands_nor_completes` —
  the acceptance scenario: two accepted PRs enter together, the queue lands one
  and ejects the other; the winner completes DONE with the target-branch merge
  commit, the ejected one stays READY_TO_REVIEW with no sha evidence.

Plus the fail-open and cost properties:
`test_an_inconclusive_queue_lookup_merges_exactly_as_before`,
`test_an_unreadable_base_branch_asks_the_queue_nothing`,
`test_one_tick_reads_each_branch_queue_once`.

Live, on the repository: the queue GraphQL surface is already exercised by
`scripts/assert_independent_acceptance.py`, which binds every `merge_group` run
to the exact live queue entries.

Not executed by an agent: the live conflicting-pair drill. Task agents have no
push capability, so the two colliding PRs cannot be created from here. The
drill, for an operator:

1. From the same base, open two PRs that edit the same lines of one file;
   take both through review to an ACCEPT marker and green checks.
2. Queue both (`gh pr merge <url> --squash` twice, or let the merge tick do
   it) and watch the queue:
   `gh api graphql -f query='query($o:String!,$n:String!,$b:String!){repository(owner:$o,name:$n){mergeQueue(branch:$b){entries(first:10){nodes{position state pullRequest{number}}}}}}' -f o=<owner> -f n=<repo> -f b=main`
3. Expect: the first entry lands, the second disappears from the entries above
   (GitHub annotates the PR itself when it drops an entry), `main` stays green,
   and the ejected PR reports DIRTY. Its task stays READY_TO_REVIEW with no
   `sha` evidence row.

## When it looks stuck

- `awaiting_merge_queue:...` that never clears → read the live queue with the
  query above; an entry in `AWAITING_CHECKS` is waiting on the `merge_group`
  run, not on the fleet.
- No `merge_group` runs at all → the trigger in `.github/workflows/ci.yml` was
  removed; every entry will time out until it is back.
- A required check renamed in CI but not in branch protection → entries can
  never satisfy the rule. Re-run `scripts/enable-branch-protection.sh` with the
  current context names.
