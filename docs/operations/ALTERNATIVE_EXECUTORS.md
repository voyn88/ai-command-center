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


## Capability-ranked full cascade

`AICC_EXECUTOR_PROFILE=owner-cascade` keeps every currently isolated coding
provider in the roster. Implementation/remediation order is Claude, Codex,
Cursor (`auto`), Grok (`grok-4.6`). Read-only review order is Codex, Claude,
Cursor, Grok. Availability and quota circuits select the first runnable entry;
provider refusal walks the remaining entries, including entries before the
initial queue-attempt position, without retrying the same route entry within
one delivery. Provider-specific model selection is reset at each switch.

The same profile rewrites already queued payloads while retaining task type,
provenance, prompt, lease and idempotency. A mutating provider may hand off only
when the existing workspace-integrity check proves it made no changes. If all
providers are unavailable, the existing bounded infrastructure-wait path keeps
the task pending; task failures do not trigger blind provider retries.

All means all **compatible** providers. The principal-isolation broker currently
supports a zero-tool model-only profile only for Grok. Independent model-only
reviews therefore retain Grok alone in this profile; Codex/Cursor read-only
sandboxes must not be relabelled as zero-tool execution. Copilot remains excluded
under ADR-0010, and Ollama is not a distributed-worker executor merely because
the separate runtime provider registry supports it.

Local-first execution belongs to the AICC planner, never to a second autonomous
scheduler. Before enabling it in the distributed queue, add an explicit local
capability adapter and validated task classes (for example structured log
classification and field extraction). A local text completion cannot satisfy a
repository-editing task or replace a required independent acceptance review.
Validate output schemas and task-specific checks, then escalate failures through
the compatible remote roster. Limit context, concurrency and CPU/memory usage;
keep inference private to the host. Downloading a model alone enables no routing.

Roll out the new profile only after the normal release review and isolated
canaries. Applying it to live queues can immediately make mutating tasks runnable;
do not activate it while writer execution is paused by the owner.


## Local structured tasks

With `AICC_LOCAL_SIMPLE_ENABLED=1` on both control and worker, the existing
asynchronous coordination producer emits `simple_task` advisory ordering for
up to eight eligible candidates. Bodies and prompts are excluded, the response
must be an exact permutation of supplied IDs, and the planner retains every
lease, WIP, wave and acceptance decision. Route-versioned fingerprints preserve
deduplication without inheriting a stale remote-provider blocker.

The same queue handler supports `classify_incident` (closed category enum) and
`extract_log_fields` (grounded service/state fields). Payload version is 1;
`operation`, `input`, configured `project_id` and `repository_path` are required.
Repository editing and acceptance are not simple-task operations. The local
model is pinned to the already-installed `qwen2.5-coder:14b`; no downloads,
model-selected tools, shell commands or external URLs are accepted.

Inference goes directly to 127.0.0.1:11434 with environment proxies disabled,
2 CPU threads, 8192 context tokens, at most 512 generated tokens and a 120-second
HTTP timeout. A worker-owned shared file lock serializes both lanes. A busy
local model waits without paid fallback; unavailable, malformed or partial
output falls through the compatible read-only remote roster under the same
queue lease. Results are validated again after every fallback. Exhaustion
returns infrastructure-wait rather than a false successful completion. Model
residency expires after two minutes. The native service remains private.


Mutation admission flags apply after the actual selected task type is resolved,
before any task workspace is provisioned. `AICC_SINGLE_MUTATING_WRITER=1`
serializes all native mutating tasks across the existing lanes using a shared
worker-owned lock held through publication and cleanup. Read-only work remains
concurrent. `AICC_MUTATING_EXECUTION_PAUSED=1` closes admission for rollout;
`AICC_MUTATING_PROJECT_ALLOWLIST=AICC` excludes CRM and other project writers.
These checks supplement the existing task and publisher leases; they do not
replace them or create a second authority. Contention uses lease-wait, while
an operator pause or infrastructure refusal uses infrastructure-wait.

The owner-cascade profile accepts `AICC_CURSOR_MODEL` (default `auto`). Set it
to an available model returned by the exact server account's CLI, for example
`composer-2.5`; subscription changes can remove `auto`. A failed Cursor model
refusal and the xAI driver's exact HTTP 402 diagnostic advance the bounded
cascade, subject to the unchanged-workspace fence. They are not task success.
