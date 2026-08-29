# Agent/publisher principal-isolation rollout

This is a fail-closed deployment gate. Do not set
`AICC_AGENT_PRINCIPAL_ISOLATION=required` until the launcher canary passes.

1. Drain one worker lane: stop new claims, wait for its active attempt to reach
   a terminal state, and preserve its lease evidence. Do not restart both lanes.
   Inventory legacy `<repo>-worktrees` and `<repo>-task-clones` first. Do not
   move their Git metadata in place. The task-local clone dependency must
   create/reconcile each active task under `/srv/aicc-workspaces`; archive a
   legacy clone only after its branch/HEAD is durable and exact-SHA matched.
2. Install root-owned provider CLIs at `/usr/local/bin/{claude,codex,copilot}`.
   Every resolved executable and its package files must be owned by root and not
   group/world-writable. Never point at `/home/voynadmin/.local`.
3. Put only model credentials in `/etc/aicc/agent-claude.env` and
   `/etc/aicc/agent-codex.env` (root:`aicc-agent`, `0640`), or provider config
   below `/var/lib/aicc-agent` (`root:root`, `0600`). The broker uses only an
   ephemeral per-run copy; the agent never gets a persistent writable home.
   Generic `GH_TOKEN`, Git/SSH helpers,
   lease variables, publish variables and workspace-HMAC authority are refused.
   Copilot stays out of routing until its auth is proven model-only and carries
   no GitHub repository authority.
4. Create `/etc/aicc/workspace-authority.env` as root:`aicc-publisher` `0640`
   with exactly one dedicated stable `AICC_WORKSPACE_AUTHORITY_KEY`. Use
   `hex:` or `base64:` explicitly; the decoded key must be at least 32 bytes.
   Publisher/gh/SSH state remains below `/var/lib/aicc-worker` `0700`.
   Never place the key in the rotator-managed DSN file or lane environments.
5. Review `/etc/aicc/agent-workspace-roots`, then run
   `deploy/install-agent-principal-isolation.sh` from the exact merged SHA.
   The installer validates all inputs before mutation, installs the versioned
   `voyn-aicc-worker@.service` and boundary files atomically, and restores the
   previous files/service enablement if any later verification fails. Use the
   same script with `--uninstall` for the recorded reversible uninstall.
   The production allowlist contains only `/srv/aicc-workspaces`; do not add
   the publisher checkout or a home directory. The task-local Git metadata
   dependency must be deployed first.
   Before any templated lane starts, the installer snapshots, drains and
   disables both legacy `voyn-aicc-worker.service` units and proves they are
   inactive, disabled and have `MainPID=0`; rollback restores the snapshot.
6. Run the OS-boundary test and a real Codex `workspace-write` commit preflight.
   Both must run under per-run systemd `DynamicUser` identities; a shared
   `aicc-agent` execution UID or direct worker-UID fallback is forbidden. Run
   two units concurrently and prove their kernel UIDs differ.
7. Only now, with the launcher preflight proven, enable the fail-closed flag:
   confirm `voyn-aicc-worker-principal-isolation.conf` (which sets
   `AICC_AGENT_PRINCIPAL_ISOLATION=required`) is installed on both the
   templated and legacy drop-in paths and run `systemctl daemon-reload`. Start
   every worker lane exclusively through `aicc_staged_worker_rollout.py
   rollout` — never a manual `systemctl start` — because its `verify_unit`
   step reads the *running* MainPID's actual environment after each start and
   refuses to proceed unless `AICC_AGENT_PRINCIPAL_ISOLATION=required` is
   present there. This is the explicit enablement/verification gate: no
   worker may process a canary task, in this step or step 8 below, while that
   check has not yet passed for its unit, closing the gap where a worker
   could otherwise fall back to optional/direct-worker mode during rollout.
8. Start the first configured `voyn-aicc-worker@<lane>.service` as the canary and require readiness plus one controlled
   task -> local commit -> guarded publish/PR cycle. Verify the agent could not
   read sentinel publisher secrets and no process remains in its transient
   cgroup.
9. Drain and roll every remaining discovered lane, one at a time, only after the previous lane stays ready. Record
   exact deployed SHA and unit hashes. Roll back the unit/code to the previous
   merged SHA if any boundary or readiness check fails; do not disable isolation.
