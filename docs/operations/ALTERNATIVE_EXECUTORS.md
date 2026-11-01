# Alternative executors and quota cooldown

`AICC_EXECUTOR_PROFILE=owner-alternatives` selects Cursor (`auto`), then Grok
(`grok-4.6`), including jobs queued before the profile changed. The original
capability class is preserved. Independent model-only reviews use Grok alone;
Cursor Ask mode is not a zero-tool reviewer. The review enqueue filters
(`_model_only_review_cascade` / `_verification_review_cascade`) therefore keep
Grok and exclude Cursor so the owner-alternatives route is not emptied into
`no_review_executor_route` before the worker rewrite.

Both providers require the principal-isolation broker. The Grok driver is
installed from the selected release as root-owned
`/usr/libexec/aicc-grok-agent`, before the broker is refreshed. Cursor uses the
root-pinned `/opt/aicc/providers/cursor/agent`. Credentials remain in protected
provider-specific `/etc/aicc/agent-{cursor,grok}.env` files. Never put publisher
credentials into these files. Read-only and model-only jobs mount the workspace
read-only; development jobs retain their dedicated writable workspace.

Recognized provider quota refusals open a worker-process-local cooldown:
30 minutes for subscription executors, 60 seconds for Grok HTTP 429. A later
job skips that executor before making a model call. When no candidate is
available, the existing infrastructure-wait path applies. The circuit expires
on time or worker restart; it is not shared between lanes. It does not replace
persistent scheduling or the telemetry work remaining in PR #1002.

Grok has at most 12 response turns, 4 tools per response, 4096 output tokens per
response and 45 seconds per shell invocation. Its cumulative 60000-token guard
is checked before executing more tools after a response; it is a post-response
driver accounting guard, not a claim of a strict API monetary or pre-call
billing cap. Oversized batches execute the first 4 shell calls and append
protocol-valid refusals for the rest. Budget, no-progress, incomplete-provider,
and post-progress transport failures emit a deterministic `AICC_PARTIAL_RESULT`
on stdout with a distinct `AICC_STOP` reason on stderr and exit 1 — never a
successful completion and never an ACCEPT. There is no optional extra model
summary call after a budget/guard stop; the durable local partial is the
authoritative incomplete report. Pending function calls are refused before that
partial so the protocol stays valid. For `grok-4.6` API requests the driver sets
`reasoning.effort=low` (official default is high) as the economy/latency
default; other models are unchanged. The enclosing broker owns run timeout and
descendant cleanup. Model-only requests send an empty tool list and reject tool
calls. Provider HTTP diagnostics omit response bodies. Successful reports and
quoted stdout quota messages do not open a cooldown.

### Worker outcome limitation (P2, existing contract)

When a Grok run executes and exits non-zero with a partial on stdout (budget /
guard stop), the worker still returns `ok=True` for that executed nonzero-budget
outcome — the longstanding handler contract for launched work that produced an
accountable result. That is **not** an ACCEPT of the independent review, and it
does not rewrite ordinary bounded remediation semantics: the partial is an
incomplete task outcome (`AICC_PARTIAL_RESULT`, exit 1), not a successful
completion. Do not treat worker `ok=True` on a partial as acceptance evidence.

## Rollout verification

Merge and deploy through the normal reviewed release path. Keep worker lanes
paused until isolated Cursor and Grok canaries prove the installed artifacts:
a read-only job cannot write, a development job can make a local commit, and a
Grok model-only review completes without tools. Verify release and helper
hashes, then apply the executor profile using the approved administrative path
and resume lanes through staged rollout. A CLI API response or worker readiness
alone does not establish that isolation or execution works.

Track useful completed tasks, provider calls, quota skips, elapsed time and
actual token usage. Do not retry unchanged provider failures blindly, weaken
acceptance gates, or mark a task complete merely because a model returned text.
