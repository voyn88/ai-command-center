# PR-Agent (Qodo OSS) review contour wiring

`command_center.orchestrator.pr_agent_review` self-hosts
[PR-Agent](https://github.com/qodo-ai/pr-agent) as a second, independent
reviewer beside `review_merge.py`'s own. It runs PR-Agent's read-only
`review` tool against every open, non-draft pull request and posts its
findings as a plain PR comment. It never decides ACCEPT/REJECT, never
posts the `ACCEPTANCE: ACCEPT <sha>` marker `scripts/
assert_independent_acceptance.py` and `_has_accept_marker` look for, and
never submits a formal GitHub PR review (approve or otherwise) — see the
module's docstring for why that is true by construction, not by
convention.

## Status: BLOCKED-ON-CREDENTIAL

Owner decision 2026-09-03: this integration needs a free-tier LLM
credential the control host does not yet have. Everything below is wired
and safe to enable now — the tick simply skips loudly
(`pr_agent_not_configured`) every time until an operator supplies one.

## Activating it

1. Get a free-tier API key from one provider:
   - [Google AI Studio](https://aistudio.google.com/) → `GEMINI_API_KEY`
   - [Groq](https://console.groq.com/keys) → `GROQ_API_KEY`
2. Add that key, plus `PR_AGENT_MODEL` (the model name is never hard-coded
   in code — check the provider's current free-tier model list), to
   `/etc/ai-command-center.env` on control-01 (the same file
   `aicc-backlog-review.service`/`aicc-backlog-merge.service` already
   read). See `.env.example` for the exact variable names and an example
   model string.
3. Install `requirements-pr-agent.txt` into the control host's venv
   (`/opt/aicc/.venv`).
4. Install and enable the timer:
   ```
   cp deploy/systemd/aicc-pr-agent-review.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now aicc-pr-agent-review.timer
   ```

## Why this is safe to run unattended

- **Comment-only by construction.** The wrapper invokes PR-Agent's CLI with
  the bare `review` tool and no other positional argument. PR-Agent's own
  `auto_approve` feature (the only code path that can submit a formal
  GitHub PR review) is reachable only when that extra argument is passed —
  it never is here. `.pr_agent.toml`'s `enable_auto_approval = false`
  settings are defense in depth on top of that, not the primary control.
- **No collision with the acceptance marker.** `_has_accept_marker` and
  `assert_independent_acceptance.py`'s `evaluate` both read a pull
  request's formal `reviews` API object exclusively. PR-Agent's `review`
  tool publishes via a plain issue comment, a different API object those
  checks never look at — even a diff engineered to provoke the LLM into
  writing marker-shaped text has nothing to attach to.
- **No independent identity needed.** Unlike the acceptance marker (which
  requires a reviewer identity distinct from the PR's own author, per
  `docs/adr` and `github_app_auth.py`), PR-Agent posts under the same
  ambient `gh` credential `aicc-backlog-review.service` already uses — a
  plain comment carries no self-approval risk.
- **Bounded LLM spend.** `max_per_tick` (default 3) and a local JSON
  head-sha cache (`--state-path`, outside the git checkout so it can never
  make `self_deploy.py`'s clean-checkout refusal see a dirty tree) keep an
  unattended free-tier key from being exhausted by repeatedly re-reviewing
  a PR whose head has not moved.

## Testing without the credential

`review_once` is fully exercisable without either key or network access:
absent both, it returns a `LoopReport` whose `skipped` list names the
reason — see `tests/orchestrator/test_pr_agent_review.py`.
