# PR-Agent (Qodo OSS) review companion

`VOYN-W0-AICC-PR-AGENT-INTEGRATION` / `command_center/orchestrator/pr_agent_review.py`
/ `command_center.db pr-agent-review` / `deploy/systemd/aicc-pr-agent-review.{service,timer}`.

## What this is

A self-hosted run of the open-source [PR-Agent](https://github.com/qodo-ai/pr-agent)
CLI (`pr_agent.cli ... review`) against every open, non-draft pull request on
this repository, on a periodic timer. Its only output is a plain PR comment
-- PR-Agent's own review summary, posted under whatever GitHub identity
`GITHUB__USER_TOKEN` belongs to.

## What this is **not**

It is **not** part of the acceptance pipeline. `review_merge.py`'s own
adversarial review is the only process with authority to post the
`ACCEPTANCE: ACCEPT/REJECT <sha>` marker `merge_once` reads before merging
anything. `pr_agent_review.py`:

- never imports `github_app_auth` or `review_merge._post_marker_as_bot`;
- never constructs an `ACCEPTANCE:`/`VERDICT:` line;
- invokes PR-Agent's CLI with the bare `review` command and one explicit
  `--config.enable_auto_approval=false` override -- `auto_approve` is never
  named on the command line, and the override is defense in depth against a
  future PR-Agent release changing that config's default (`_run_pr_agent`'s
  argv is exactly five tokens plus that one override, asserted exactly —
  not by substring — in `tests/orchestrator/test_pr_agent_review.py`).

Its comment is one more finding for a human, or the independent reviewer,
to read -- exactly like any other third-party bot's comment. The review
contour may use it as input; it is never a gate the review contour or the
merge tick has to trust, consult, or even notice.

## Credentials

PR-Agent needs an LLM credential and a GitHub token; this module never
mints or acquires either -- their absence is a clean, repeated skip, not a
failure the tick tries to work around.

- **Exactly one** of `GEMINI_API_KEY` / `GROQ_API_KEY` (both free-tier). Set
  neither and the tick reports `CREDENTIAL none` / `SKIP -: not_configured`
  and exits 0, every tick, forever -- the 2026-09-03 owner decision was
  explicit that a missing key must be *confirmed*, not auto-purchased. Set
  **both** and the tick refuses with `multiple_providers_configured:...`
  rather than silently preferring one (`pr_agent_review.preflight`) -- two
  models silently disagreeing about which one is "the" PR-Agent opinion is
  worse than neither running.
- `GITHUB__USER_TOKEN` -- the token PR-Agent's own GitHub provider posts
  comments with. Distinct from the fleet App `gh` identity `gh_access.py`
  resolves for every other control tick: PR-Agent's CLI talks to GitHub's
  REST API directly, not through the `gh` binary, so it carries its own
  token.

No secret VALUE is ever logged, printed, or embedded in a skip/error
reason -- see `preflight`'s docstring and `_run_pr_agent`'s redaction of
every credential this process holds out of any captured subprocess output
before that output is stored.

## The state file: must resolve outside the checkout

`--state-path` is where this tick remembers, per PR number, the head sha it
last ran PR-Agent against (`already_reviewed_at_head` dedup -- a PR whose
head has not moved since the last successful run is skipped, not re-run).

That path **must** resolve outside `--repo-path`. This is enforced in code
(`pr_agent_review._state_file` raises `ValueError`, which the CLI command
reports as `ERROR -: pr-agent-review`), not merely documented: `self_deploy`
treats *any* untracked or modified path under the checkout as a dirty tree
(`git status --porcelain`, unscoped to any subdirectory) and refuses to
deploy over it. An in-checkout state file would make `voyn-aicc-self-
deploy.timer` refuse every deploy on this host from the first successful
tick onward. The deployed unit uses a systemd `StateDirectory=`
(`/var/lib/aicc-pr-agent-review/state.json`), which by construction never
places anything inside a source checkout.

## Deployment

```
pip install -r requirements-pr-agent.txt   # NOT part of requirements.txt
```

Fill in `/etc/aicc/pr-agent.env` (see `.env.example`) and enable
`aicc-pr-agent-review.timer`. The unit runs as `voynadmin` (the same
control-plane principal as `voyn-aicc-pr-window` and `voyn-aicc-self-
deploy`), opens no database connection, and needs nothing this host does
not already have for those other ticks except the two credentials above.

## Operational semantics

- One subprocess per PR (`_run_pr_agent`), each with its own
  `PrAgentConfig.timeout` (default 600s). A hung or crashing PR-Agent run
  against PR A costs PR A alone and is recorded as `ERROR #A: pr_agent_failed:...`
  -- it never aborts the rest of the tick's PR list.
  `subprocess.TimeoutExpired`, a missing interpreter/module, and any other
  spawn failure are all caught, never raised out of the per-PR loop.
- `report.errors` (pr listing failure, an unusable `--state-path`, a
  per-PR `pr_agent_failed`) makes `command_center.db pr-agent-review` exit
  non-zero, so a timer run goes red for the operator. `report.skipped`
  (no credential, a draft PR, an already-reviewed head, a malformed
  listing entry) never does -- these are expected steady states, not
  defects.
- Bounded, not exhaustive: one REST page (`PrAgentConfig.pr_limit`, default
  20) per tick, no pagination. A PR this tick's page does not reach is
  simply picked up by a later one -- the same tolerance
  `already_reviewed_at_head` already gives a PR whose run failed or timed
  out.
