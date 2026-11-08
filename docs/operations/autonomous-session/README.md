# AICC — independent engineering session

Checkpoint: 2026-11-09 (Europe/Moscow). This is a session handoff, not a runtime health attestation or a second backlog.

## Verified baseline

- Repository: `voyn88/ai-command-center`; audited main: `99095570ea11cb2292598015f5ef47cdacd411ec`.
- GitHub connector reports pull/push rights. A shallow main checkout succeeded in the attached Linux environment.
- Main CI [42463842707](https://github.com/voyn88/ai-command-center/actions/runs/42463842707), security [42463842747](https://github.com/voyn88/ai-command-center/actions/runs/42463842747), boundary fitness [42463842726](https://github.com/voyn88/ai-command-center/actions/runs/42463842726), installer [42467182352](https://github.com/voyn88/ai-command-center/actions/runs/42467182352) reported success.
- API main protection requires `Final merge gate` and `Acceptance gate (independent verdict on exact SHA)`.
- Search found 115 open PRs before this checkpoint PR. This is inventory, not the active review-window occupancy.
- No SSH identity, Mac attachment, control-plane connection or worker runtime access was available here. Installed server version, current leases, provider authentication and remaining quotas are UNKNOWN.
- Issue [#1078](https://github.com/voyn88/ai-command-center/issues/1078) contains historical remote-session PONGs from November 4 and an earlier report of quota/dead-letter stalls. Neither proves current access or authorizes a new command consumer. No bridge command was sent.

## Reuse and authority

Use the existing PostgreSQL backlog → planner → queue → worker → review/publish → GitHub gates → release → evidence chain. Retain queue lease heartbeat, idempotency keys, worktree leases, writer fencing and principal isolation. Session notes cannot grant ACCEPT or mark canonical tasks DONE.

Keep AICC and CRM in separate worktrees, branches, prompts and evidence. Do not start a second planner, promoter, backlog store or queue consumer. Two existing worker lanes do not establish spare capacity. Preserve the deployed single-writer limit until live ownership and capacity are verified; independent project sessions do not require simultaneous writes.

Relevant implementation: `command_center/orchestrator/planner.py`, `routing.py`, `command_center/worker/{daemon,handlers,payloads,writer_lease}.py`; deployment and controls: `deploy/systemd/voyn-aicc-worker@.service`, `docs/operations/{ALTERNATIVE_EXECUTORS,FLEET_STATUS,CLAUDE_MAC_REVIEWS}.md`.

## Additional spend policy

No new paid provider calls or fallback are authorized by this checkpoint. Preserve the existing billing classification: Claude/Codex = subscription/quota; Cursor/Grok/openai_http = metered; unrecognized executors = unknown. Do not infer billing from a purchased subscription.

The current planner's `_metered_spend_24h_usd` returns unknown. Existing FinOps filtering must keep metered links excluded and propagate `finops_excluded_executors` so worker profile rewriting cannot restore them. Do not use a zero numeric limit as a hard stop: current limit semantics treat non-positive values as disabling that ceiling. B2/B3 ledger/reservation and authoritative monetary reconciliation remain incomplete. Old gap-matrix text describing all controls as future work predates B1.

Subscription exhaustion must wait under the existing bounded retry/cooldown rules, without enabling paid fallback. Preserve a held independent review if no compatible subscription reviewer is available. Local structured inference is advisory and cannot replace code acceptance. Do not activate model downloads or new runtime profiles.

## First executable work unit — NOT DISPATCHED

Scope: read-only runtime admission audit, followed by one existing eligible AICC task. Reconcile with the canonical backlog before creating/importing any task.

1. On the already authorized control/worker connection, identify the active release SHA, service owner, lane state, current queue claims and writer leases. Record timestamp and release SHA privately; never copy environment values, tokens or raw provider logs into this public repository.
2. Under an already provisioned least-privilege DB identity, reuse:
   - `python -m command_center.db status`
   - `python -m command_center.db fleet-status`
   - `python -m command_center.db backlog-status`
   - `python -m command_center.db queue-dlq --limit 10`
   These are observations; do not bootstrap/upgrade/reap/redrive/import as part of this audit. DLQ payloads may be private; publish only sanitized counts and reason codes.
3. Verify installed B1 code matches the reviewed release and metered exclusions survive profile rewriting. Inspect auth/quota status without an inference call first.
4. Classify the actual active PR window before increasing dispatch. Reconcile [#642](https://github.com/voyn88/ai-command-center/issues/642) and PR [#930](https://github.com/voyn88/ai-command-center/pull/930); do not assume an open issue is unimplemented. Review recent candidates [#1124](https://github.com/voyn88/ai-command-center/pull/1124), [#1122](https://github.com/voyn88/ai-command-center/pull/1122) and [#1110](https://github.com/voyn88/ai-command-center/pull/1110) against fresh head/checks/reviews. At observation, #1122 and #1110 conflicted.
5. Only after live admission is proved, run one already eligible task through the existing planner with no extra paid route. Require a terminal result, exact-head checks and independent acceptance. Do not bulk-redrive parked work.

Admission DoD: active owner and immutable runtime revision known; leases and DB reachable; subscription route proven; paid routes excluded; one canonical task selected without duplicate dispatch. If any prerequisite is unknown, remain BLOCKED_RUNTIME_ACCESS and retain existing queue state.

## Validation, reporting and release boundary

For each task report task ID, project, base/head SHA, claim/run/attempt ID, changed paths, exact test commands and exit codes, PR URL, required checks, independent verdict, merged SHA, deployed SHA and acceptance separately. Record known spend and unknown coverage; never label missing cost telemetry as zero.

Use current selectors and pinned dependencies; run targeted tests before required gates. Reuse successful evidence only for the same revision and relevant environment. Read the failing job before one bounded retry of a proven transport failure. No weakened checks, fake statuses, force pushes, foreign-worktree resets or admin merges.

This checkpoint changes documentation only. Local product tests were not run: the cloud Python lacked pytest, Ruff and psycopg. Existing GitHub results above are historical evidence on the stated SHA, not validation of a new runtime. Draft PR review is the next repository gate. Do not enable auto-merge or trigger release from this setup task: normal merges can start automatic deployment.

Rollback of this handoff: close its draft PR; no service, queue, credential, production setting or application code changed. Resume by refreshing main, PR state and live ownership before acting.
