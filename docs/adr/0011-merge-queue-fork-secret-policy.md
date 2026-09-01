# ADR-0011: merge-queue fork policy for repository secrets

Status: accepted for `VOYN-W0-AICC-MERGE-QUEUE-FORK-POLICY`.

## Context

`ci.yml` added a `merge_group` trigger so the queue's checks actually run
(without it every queue entry times out). That trigger was first justified on
the claim that the queue "never runs a fork's code with secrets" — wrong, and
caught by independent acceptance on PR #302. The queue ref
(`gh-readonly-queue/<base>/pr-N-<sha>`) is created *in this repository*, so
`merge_group` is a base-repository event and **is** handed repository
secrets, unlike `pull_request`, which withholds them from forks. That ref
nevertheless carries the pull request's own head commits — a fork's included.
`ci.yml`'s `prepare` job runs `scripts/fetch_aios_sdk_artifact.py`, a script
read out of that same checked-out tree, with `AIOS_ARTIFACT_READ_TOKEN` (a
read credential for the private `dimastov-lab/aios` repository) in its
environment. Enabling the queue therefore makes that standing credential
reachable from fork-authored code unless something stops it, and this
repository is public with forking allowed.

The `merge_group` trigger itself (PR #312) is safe in isolation: it does
nothing until the merge queue feature is turned on in repository settings.
This ADR is the decision that gates turning it on.

## Options considered

1. **Ban forks from the queue outright**, by refusing at admission time.
   GitHub's merge queue has no such per-repository toggle — anyone with merge
   permission can enqueue any mergeable, approved pull request, fork-authored
   or not. There is nothing to configure here; the only enforceable version
   of "ban" is a runtime refusal once the run starts (option 3).
2. **Move the secret-bearing step out of the queue path entirely** (e.g. only
   fetch the AIOS SDK/DB artifacts on `push`/`pull_request`, skip it on
   `merge_group`). Rejected: the queue exists specifically to test the
   *prospective merged result*, not the branch in isolation, and the AIOS
   wheels are a hard dependency of the test suite (`AICC_AIOS_SDK_WHEEL`,
   `AICC_AIOS_DB_WHEEL` — most of `quality-gates` fails to import without
   them). Skipping the fetch on `merge_group` would mean the queue's
   required checks either don't run or run against a broken environment,
   defeating the reason the trigger exists.
3. **Fail closed at the point of use**: every step that receives a repository
   secret first proves the checked-out code was authored in this repository,
   and refuses otherwise. **This is the chosen option**, and it is already
   implemented and merged (`scripts/assert_trusted_head_repository.py`,
   PR #312, pinned by
   `tests/test_release_gate_policy.py::test_every_step_holding_a_repository_secret_first_proves_its_code_is_ours`
   and `tests/test_trusted_head_repository.py`). The guard resolves the
   queued pull request via the GitHub API (the `merge_group` payload omits
   the head repository; only the queue ref's embedded PR number lets the
   guard ask) and refuses to let the step continue if the head repository is
   not this one. A fork's queue entry therefore always fails the required
   check before the secret is ever exposed to fork code — the practical
   effect of a ban, enforced where GitHub gives no admission-time hook.
4. **Require approval before enqueueing** (e.g. a human review/approve gate
   ahead of the queue). Kept as defense-in-depth, not as the primary control:
   approval is a human process step, not a technical boundary, and a
   reviewed PR's queue ref still carries fork commits that reach the guarded
   step. Branch protection already requires review before merge; that
   requirement is unchanged by this ADR and continues to apply on top of
   option 3.

## Decision

Option 3, already merged: every workflow step that holds a repository secret
runs `scripts/assert_trusted_head_repository.py` first, under a shell whose
non-zero exit aborts the step (`bash`/`sh`, never the default `pwsh` on
Windows runners — see
`tests/test_release_gate_policy.py::test_a_refused_guard_actually_stops_the_step`).
Both invariants are swept over every workflow file in `.github/workflows/`,
not a hardcoded pair, so a new workflow that subscribes to `merge_group` with
an unguarded secret fails CI. Standard branch-protection review requirements
(option 4) remain in place as an additional layer.

## Enablement gate

The merge queue stays **disabled** in repository settings until all of the
following hold:

- This ADR is accepted (it is, as of this record).
- `tests/test_release_gate_policy.py` and `tests/test_trusted_head_repository.py`
  are green on `main` (they are — the guard and its invariants are already
  merged).
- The branch protection required status check on `main` names the actual
  aggregate gate, `Final merge gate` (the `final-gate` job), not a stale
  context — see `scripts/enable-branch-protection.sh`.

Only after an operator has confirmed the above does turning on the merge
queue in GitHub's branch-protection settings become safe. That toggle is a
repository-settings action with no representation in this codebase, so it is
not automated here; it is a manual step an operator takes with reference to
this ADR.

## Rejected alternatives

- Trusting `merge_group` the way `push` is trusted (both are base-repository
  events): rejected because `push` to `main` only ever carries commits that
  already passed branch protection review on `main` itself, while a queue
  entry's commits are an unreviewed fork's by construction until the guard
  runs.
- Scoping the AIOS tokens to read-only and calling that sufficient: the
  tokens already are least-privilege (`AIOS_ARTIFACT_READONLY_TOKEN` /
  `AIOS_ARTIFACT_READ_TOKEN`), but read access to a private repository is
  still a credential a public fork must not receive; scope reduction and the
  trust guard are complementary, not substitutes.

## Operational cost and revisit condition

Every workflow step that needs a repository secret must now spend one step on
the guard and must declare `shell: bash` explicitly. Revisit this ADR only if
GitHub ships a native admission-time control for excluding fork-authored pull
requests from a merge queue; until then, fail-closed-at-use is the only
enforceable mechanism.
