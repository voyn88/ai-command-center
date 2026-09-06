# Agent/publisher principal-isolation rollout

This is a fail-closed deployment gate. Do not set
`AICC_AGENT_PRINCIPAL_ISOLATION=required` until the launcher canary passes.

1. Drain one worker lane: stop new claims, wait for its active attempt to reach
   a terminal state, and preserve its lease evidence. Do not restart both lanes.
   Inventory legacy `<repo>-worktrees` and `<repo>-task-clones` first. Do not
   move their Git metadata in place. The task-local clone dependency must
   create/reconcile each active task under `/srv/aicc-workspaces`; archive a
   legacy clone only after its branch/HEAD is durable and exact-SHA matched.
2. Nothing to do: the provider CLIs install themselves. The installer runs
   `ops/aicc_toolchain_install.py`, which downloads the artifact pinned by
   `deploy/agent-toolchain.lock.json`, proves its sha256, extracts it root-owned
   and selects it at `/opt/aicc/toolchains/current`
   (VOYN-W0-AICC-TOOLCHAIN-CONTENT-ADDRESSED, merged `91c7718`).
   **Do not install the CLIs by hand**, and in particular never with
   `npm install --global`: that is the finding this gate exists to close --
   it resolves packages online and runs their lifecycle scripts as root. An
   executable under `/usr/local/bin` or an operator's home is now ignored;
   the installer refuses any executor that resolves outside the selected
   release. To change a CLI version, edit the lock, run the
   `build-agent-toolchain` workflow, and record the digest it reports -- a
   reviewed change, never an ambient `latest`.
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
5. Review `/etc/aicc/agent-workspace-roots`, then run only the root-owned
   exact-SHA bootstrap. **Never execute**
   `deploy/install-agent-principal-isolation.sh` from an operator's home
   directory or any other operator/agent-writable checkout: that would execute
   mutable Python and shell as root before the immutable release exists. For the first
   installation, do not execute any file from a checkout. From the Hetzner
   root console, set `expected_sha` to the independently accepted merged SHA,
   then use only host binaries to fetch `main`, prove that it still equals that
   SHA, and extract the bootstrap blob directly from the authenticated Git
   object into a new private root-owned file:

   ```sh
   expected_sha=<40-hex-merged-main-sha>
   umask 077
   install -d -m 0700 -o root -g root /var/lib/aicc-stage0
   rm -rf /var/lib/aicc-stage0/repo
   /usr/bin/env -i HOME=/var/lib/aicc-stage0 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
     GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
     GIT_NO_REPLACE_OBJECTS=1 /usr/bin/git init --initial-branch=bootstrap \
     /var/lib/aicc-stage0/repo
   /usr/bin/env -i HOME=/var/lib/aicc-stage0 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
     GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
     GIT_NO_REPLACE_OBJECTS=1 /usr/bin/git -C /var/lib/aicc-stage0/repo remote \
     add origin https://github.com/voyn88/ai-command-center.git
   /usr/bin/env -i HOME=/var/lib/aicc-stage0 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
     GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
     GIT_NO_REPLACE_OBJECTS=1 /usr/bin/git -C /var/lib/aicc-stage0/repo \
     -c protocol.file.allow=never fetch --no-tags origin \
     refs/heads/main:refs/remotes/origin/main
   test "$(/usr/bin/env -i HOME=/var/lib/aicc-stage0 \
     PATH=/usr/sbin:/usr/bin:/sbin:/bin GIT_CONFIG_NOSYSTEM=1 \
     GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
     /usr/bin/git -C /var/lib/aicc-stage0/repo rev-parse \
     refs/remotes/origin/main^{commit})" = "$expected_sha"
   /usr/bin/env -i HOME=/var/lib/aicc-stage0 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
     GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
     /usr/bin/git -C /var/lib/aicc-stage0/repo cat-file blob \
     "$expected_sha:ops/aicc_exact_sha_bootstrap.py" \
     >/var/lib/aicc-stage0/voyn-aicc-bootstrap
   chown root:root /var/lib/aicc-stage0/voyn-aicc-bootstrap
   chmod 0700 /var/lib/aicc-stage0/voyn-aicc-bootstrap
   /usr/bin/python3 /var/lib/aicc-stage0/voyn-aicc-bootstrap \
     --expected-sha "$expected_sha"
   ```

   The bootstrap fetches the fixed remote again under a scrubbed environment,
   requires remote `main` to equal the supplied SHA, verifies every checked-out
   blob and executable mode, writes a root-owned attestation, creates the
   dedicated workspace-authority key when absent, and only then runs the
   principal installer. The root-owned provider toolchain is a separate
   integrity-pinned generation and must already pass the installer boundary
   checks; the bootstrap never runs an online global package installation. A
   successful generation installs the
   same verifier as `/usr/local/sbin/voyn-aicc-bootstrap`; use that immutable
   command for later exact-SHA upgrades. The installer refuses direct use
   without a matching attestation. It installs the versioned
   `voyn-aicc-worker@.service` and boundary files atomically, and restores the
   previous files/service enablement if any later verification fails. Use the
   installed command with `uninstall --expected-sha <merged-main-sha>` for the
   recorded reversible uninstall.
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
