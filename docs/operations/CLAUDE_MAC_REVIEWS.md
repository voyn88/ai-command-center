# Claude subscription reviews from the owner's Mac

The opt-in Mac path supplies **model-only independent reviews** to the existing
AICC queue. It does not supply repository tools, implementation, publishing,
acceptance authority, task scheduling or a second writer. Implementation and
verification with file inspection retain their existing server executors.

The Mac runs `ops/aicc_claude_mac_server.py` as an owner LaunchAgent. Each request
uses the signed installed Claude Code CLI, verifies `authMethod=claude.ai` and a
Pro/Max subscription, and supplies a minimal environment excluding API keys,
provider overrides, SSH authority and alternate credential directories. The CLI
uses `--safe-mode --restricted --tools '' --strict-mcp-config` with an empty MCP
configuration, no Chrome, no slash commands, and no session persistence. An
admin-managed customization policy causes a refusal requiring capability review.
Every successful response must contain an initialization receipt with empty tool
and MCP lists; a tool-use event invalidates the response.

The endpoint binds only `127.0.0.1:17443`. A dedicated owner LaunchAgent maintains
an SSH reverse forward, also bound to worker loopback, through the existing
control jump host. TLS authenticates the Mac before any prompt or bearer token
is sent: this prevents another local principal from impersonating the endpoint
when the SSH forward is down. The worker pins the Mac's self-signed server
certificate; hostname checking and certificate verification remain enabled.
The private TLS key and Claude OAuth credentials remain on the Mac. A separate
inference-only token authenticates the worker. Neither transport accepts a
command, working directory, repository path, tools, executable or model override
from its caller.

Runtime files on the Mac live under
`~/Library/Application Support/VOYN/AICC-Claude-Review/`, with owner-private token,
TLS key, scratch directory and LaunchAgent configuration. The installed CLI path
is pinned to the verified bundled version; updating it requires another
capability check. The certificate needs ordinary coordinated rotation before its
expiry. The service runs only when the Mac user's login context is available.

The worker's root-owned mode-0600 `/etc/aicc/agent-claude-mac.env` contains only
`AICC_CLAUDE_MAC_TOKEN` and `AICC_CLAUDE_MAC_CA_B64` (public certificate). The
privileged broker loads those two keys only for Claude's `model_only` profile;
it stages no local Claude OAuth file, uses the immutable
`/usr/libexec/aicc-claude-mac-agent` helper and preserves read-only mounts and
cgroup teardown. Other Claude profiles retain their existing credentials and
server command. Set `AICC_CLAUDE_MAC_ENABLED=1` on the control review producer and
existing workers only after testing the private transport. The independent-review
cascade then uses Mac Claude before Grok. Disconnects, resource limits and
unattested results are provider failures handled by the existing cascade.
One request at a time runs on the Mac, with bounded input/output and a 180-second
inference deadline. Cancellation terminates only that request's own process group.

## Recovering the review backlog

`AICC_REVIEW_STALE_RECOVERY=1` enables a bounded entry into model review for PRs
whose blocked window would otherwise depend on their own missing acceptance.
Each review stage may inspect at most three such PR snapshots and recover one
PR per tick, within the existing active-review backpressure. A fresh open PR and
exact SHA, known author, successful Final merge gate, green other code/security
checks, no conflicts and no standing independent rejection are required. The
acceptance job itself may await that model review. Publication still revalidates
the full diff/head and validated verdict. Recovery grants no merge slot: normal
window selection, independent acceptance, required CI and merge-queue policy
continue to gate merging. The feature defaults off.

Closed-unmerged PRs, already-merged deliveries lacking acceptance evidence, red
code checks and missing PR evidence are separate triage findings. They are not
silently marked DONE. Preserve the original evidence and use verified outcomes
and the existing revision-checked lifecycle when resolving them.
