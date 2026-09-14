# Decision Record — GitHub Branch Protection Tier Gap

- **Record id**: DR-GITHUB-TIER-ENFORCEMENT-001
- **Status**: **Pending founder decision.** This record cannot be closed by an agent — the choice
  below is a commercial/GitHub-plan decision, which sits in the founder-reserved decision set
  alongside "commercial model" (`projects/AIOS.md` §13). It is drafted here so the founder can
  decide by picking Option A or Option B; nothing is authorized yet.
- **Date**: 2026-08-26 (finding); updated 2026-09-14 with the re-check tooling below.
- **Task**: `VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP` (Wave 0, P0)
- **Scope**: Decision escalation, plus the tooling that makes the finding re-checkable. No GitHub
  settings changed, no repository/org plan changed, no runtime code changed.

## Finding

`gh api repos/voyn88/ai-command-center/branches/main/protection` was checked and confirms:

- `required_approving_review_count` = `0`
- `required_status_checks` — absent
- `enforce_admins` = `false`

Branch protection on `main` currently enforces **nothing**. It is not a required-reviews gate and
not a required-checks gate, regardless of what CI reports or what `merge_once`
(`command_center/orchestrator/review_merge.py`) decides. This matches what the codebase already
says about itself — README.md, `CURRENT_STATE.md` §"Current limitations", and ARCHITECTURE.md §13
already state that the workflow/CI does not itself configure or enforce branch protection, and that
the current private-repo plan does not expose branch protection/rulesets. No *document* audited
here overstates branch protection as a working control — but one piece of tooling did, and is fixed
below ("Tooling defect found and fixed while writing this").

The only actual gate standing between an ACCEPT-marked PR and a merge to `main` is the application
layer: `merge_once` requires an ACCEPT verdict and green required checks *as GitHub reports them to
that code path*, then calls `gh pr merge`. There is no GitHub-side backstop if that application
logic has a bug, is bypassed, or is run against a misconfigured check set.

## Options

**Option A — Change plan/org.** Move the repository to a GitHub plan or organization that exposes
real enforcement (rulesets or classic branch protection with `required_status_checks` and
`required_approving_review_count` ≥ 1, `enforce_admins` = `true`). This closes the gap at the
platform level and makes `merge_once` a second, redundant gate instead of the only one. This is a
recurring commercial cost and an account/org change — a commercial decision, not an engineering one.

**Option B — Accept the gap as a documented risk, with a hard scaling constraint.** Explicitly
accept that until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships, the application-level `merge_once`
gate is the *only* line of defense against a bad merge to `main`, and commit to **not increasing
the number of concurrent task agents/branches working against this repository** beyond the current
level while that is true — because every added concurrent writer increases the blast radius of a
`merge_once` bug or bypass with no GitHub-side backstop to catch it.

## Recommendation

Option B is the lower-friction default: it costs nothing and matches the actual current operating
pattern (one agent = one task = one branch = one worktree per `docs/roadmap/MASTER_PRODUCT_ROADMAP.md`
line 199). But "accept this risk" is itself a founder call, not something an agent should decide on
the business's behalf — so this record stays **Pending** rather than marking Option B accepted.

## What closes this record

The founder picks Option A or Option B (or a variant). Once chosen:

- If **A**: record the target plan/org and the date the change lands; re-verify with
  `python3 scripts/verify_branch_protection.py --repo voyn88/ai-command-center --require-check
  'Quality gates (whitespace · Ruff · compile · pytest)' --require-admins` and paste its exit-0
  output here before marking this Accepted. An exit 1 or 2 does not close this record.
- If **B**: record the explicit concurrency cap (a number or rule, not just "be careful") that
  stays in force until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships, then mark this Accepted.

Until either happens, no document or audit in this repository should describe branch protection as
an enforced control without re-checking the API first — the finding above can go stale the moment
someone changes the GitHub setting by hand.

## How to re-check (2026-09-14)

The rule above only works if re-checking is cheap, so the check is now a script rather than an
instruction:

```bash
python3 scripts/verify_branch_protection.py --repo voyn88/ai-command-center --branch main
# or, offline / from a captured payload:
gh api repos/voyn88/ai-command-center/branches/main/protection | \
  python3 scripts/verify_branch_protection.py --json -
```

It prints what the API reports — required checks, `required_approving_review_count`,
`enforce_admins`, force-push and deletion flags — and separates three outcomes that this record
depends on keeping apart:

- **exit 0** — every stated requirement is backed by the API;
- **exit 1** — the API was read and the requirement is unmet, or (with no `--require-…` flag)
  protection enforces no merge gate at all. This is the state recorded above;
- **exit 2** — the state could **not** be read (no token, API error, unparseable body). Unverified
  is not the same as unprotected and must never be written up as either.

A 403 (`Upgrade to GitHub Pro …`) and a 404 (`Branch not protected`) are both reported as *not
enforced*, quoting GitHub's own message: for a merge into `main` the two are the same fact, and
which one it is, is precisely what Option A versus Option B is about.

`tests/test_branch_protection_verifier.py` pins the payload measured above as the "enforces
nothing" case, and adds a second guard: no Markdown document in this repository may assert branch
protection as a working control without a qualifier or a pointer to this record. That is the second
half of the task's acceptance criterion, held by a test rather than by vigilance.

## Tooling defect found and fixed while writing this

`scripts/enable-branch-protection.sh` issued the protection `PUT` and then unconditionally printed:

```
✓ Branch 'main' is now protected.
  - Required check: Quality gates (whitespace · Ruff · compile · pytest)
  ...
  - Admins enforced: yes
```

— on the strength of its own write, never reading the setting back. On this plan that is a false
statement produced by the repository's own tooling, and it is a plausible origin for anyone
believing the control exists. The script now reads the protection endpoint back through the
verifier and prints a success line only for what the API confirms; when it cannot confirm, it fails
loudly and names this record. **No claim in this record rests on that script's output.**

The re-check could not be re-run at the time of this update: the agent environment has no
authenticated `gh` and no network to the GitHub API, so exit 2 (unverified) is all it could
produce. The finding therefore stands **as measured on 2026-08-26**, and the founder (or any agent
with API access) should re-run the command above before acting on it — including before choosing
Option A or Option B.
