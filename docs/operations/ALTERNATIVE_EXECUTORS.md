# Alternative executors and quota cooldown

`AICC_EXECUTOR_PROFILE=owner-alternatives` selects Cursor (`auto`), then Grok
(`grok-4.3`), including jobs queued before the profile changed. The original
capability class is preserved. Independent model-only reviews use Grok alone;
Cursor Ask mode is not a zero-tool reviewer.

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
is checked before executing more tools after a response; it is not a hard
provider billing cap. The enclosing broker owns run timeout and descendant
cleanup. Model-only requests send an empty tool list and reject tool calls.
Provider HTTP diagnostics omit response bodies. Successful reports and quoted
stdout quota messages do not open a cooldown.

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
