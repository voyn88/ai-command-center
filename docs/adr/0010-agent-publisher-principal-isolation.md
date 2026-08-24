# ADR-0010: separate agent and publisher Unix principals

Status: accepted for `VOYN-W0-AICC-AGENT-PUBLISHER-PRINCIPAL-ISOLATION`.

## Decision

The queue worker and guarded publisher remain the existing publisher principal
(`voynadmin` on the current preprod host; `aicc-worker` in the canonical unit).
Both are members of the non-secret `aicc-publisher` socket-access group. Every
model CLI, its shell tools and all descendants run in a separate transient
systemd service as the non-login `aicc-agent` user. A root-owned,
socket-activated fixed launcher is the only identity bridge. The worker keeps
`NoNewPrivileges=yes`; it neither receives sudo nor selects a privileged command.

The launcher accepts a closed JSON schema: exact task workspace, provider,
read-only/development profile, prompt, optional model, timeout and nonce. It
accepts no command, path to an executable, or environment value. Provider
commands are reconstructed from a fixed allowlist. Model authentication is
loaded from root-owned per-provider environment files or a root-private model
auth store. The broker copies only that credential into a fresh per-run home
and deletes it after cgroup exit, so one task cannot persist model config or
instructions into another. Publisher/GitHub/SSH credentials and writer-lease authority
remain private. `AICC_WORKSPACE_AUTHORITY_KEY` remains in the dedicated root-owned
`/etc/aicc/workspace-authority.env`, readable by the publisher group only;
the agent UID cannot read it and its mount namespace marks it inaccessible.
Copilot is not in the isolated executor allowlist because its current login is
a GitHub credential. It remains disabled for all agent tasks until a separate
model-only credential with no repository authority is independently proven.

Each run gets a separate cgroup and mount namespace. Only its exact workspace
is bind-mounted read-write at `/workspace`; homes are hidden, `/proc` is
restricted, privilege gain and capabilities are disabled, and process teardown
uses `KillMode=control-group`. The canonical workspace root is inaccessible in
the namespace before the exact bind, so absolute paths and symlinks cannot read
a sibling task. Retry is released only after PID 1 reports the unit inactive
and its cgroup empty; a unit that cannot be sealed causes an atomic workspace
move into a root-only quarantine. The shared `aicc-workspace` group transfers task
files back to the publisher, but sibling task workspaces are not mounted into an
agent unit. Codex keeps its inner `workspace-write` sandbox; no
`danger-full-access` fallback exists.

## Rejected alternatives

- Environment scrubbing alone: disk credentials and `/proc` remain readable by
  a same-UID agent.
- `sudo`/setuid launcher from the worker: incompatible with
  `NoNewPrivileges=yes` and unnecessarily grants an elevation path.
- Running the whole worker as `aicc-agent`: gives the agent the publisher's
  ambient authority again.
- One shared agent daemon/cgroup: a wedged or escaped child can outlive and
  interfere with another task.

## Operational cost and revisit condition

Deployment must install provider CLIs at root-owned, non-writable paths and
migrate model-only auth into `aicc-agent` state. This is intentional cost for a
kernel-enforced boundary. Revisit only if a maintained container/microVM runtime
provides equal exact-workspace, credential and cancellation guarantees with
measured lower lifecycle cost.
