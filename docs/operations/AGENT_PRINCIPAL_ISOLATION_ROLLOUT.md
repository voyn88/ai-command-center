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
   The release venv also needs the accepted AIOS wheels (`aios-sdk.lock.json`,
   `aios-db.lock.json`), which CI fetches with a read-only token a root
   installer must not hold. Stage them once per digest, root-owned and
   immutable, at `/var/lib/aicc-artifacts/<wheel_sha256>/<wheel_filename>`
   (`install -D -m 0644 -o root -g root`); the installer verifies each wheel
   against the release's lock file and refuses a missing, writable or
   mismatched artifact.
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

## Declaring a file absent (desired-absent targets)

The installer's generations describe files that must exist. A path that must
*not* exist is declared in `DESIRED_ABSENT_TARGETS`
(`ops/aicc_install_transaction.py`) and removed by whichever generation
installs next — worker or control, one `prepare`/`apply`/`commit`, the same
rollback boundary as every install beside it. Nothing about `voynadmin`'s
rights changes: the removal runs as root inside the transaction, from
repository state that was reviewed, which is the point. `NOPASSWD` covers
only `systemctl` and `apt-get`, so an operator cannot `rm` these paths at
all — and should not be able to.

What the transaction guarantees for a declared-absent path:

* **Snapshot first.** The target's bytes, mode and owner — or, for a symlink,
  its literal text — go into the generation WAL before anything is touched.
* **Atomic removal.** The object is renamed into an unpredictable quarantine
  entry under a pinned parent descriptor, proven against the snapshot there,
  and only then unlinked. There is no window in which a pathname is resolved
  twice.
* **Byte-for-byte rollback.** Any failure later in the same generation puts
  the object back exactly as it was — a symlink comes back as a symlink, with
  the same target. A crash between the quarantine rename and the unlink is
  reclaimed by `recover` (the original inode, not a copy).
* **One exact path.** Globs, braces, `..`, `.`, empty components and control
  characters are refused before the generation is staged, exactly as they are
  for sudoers, unit and repository rules.
* **Already absent is a no-op**, not a refusal: a converged host removes
  nothing.

### Adding a target

Each entry carries a *staleness proof*, because reversible is not the same as
safe — destroying the unit file underneath a loaded unit would break the host
and then correctly restore it only if something else also failed. The proof
this build checks is `dangling-symlink`: the path is a symlink whose target
does not exist, so it cannot be backing a loaded unit (`systemctl` reports
such a fragment as `not-found`). It is evaluated against the live host twice:
before the generation is staged, and again immediately before `apply` mutates
anything. Record the evidence in the entry itself.

### If an install refuses

```
declared-absent symlink resolves and may back a loaded unit: <path> -> <target>
declared-absent target is not the dangling symlink it was declared as: <path>
```

Both are fail-closed refusals that destroy nothing, and both mean the same
thing: the path is no longer the inert leftover somebody proved stale, so the
installer will not touch it and will not proceed around it. Find out what
created the object. Either the declaration is now wrong (remove the entry) or
the host is, and either answer is a reviewed change, not a `rm`.

### The first target

`/etc/systemd/system/aicc-systemd-voyn-aicc-self-deploy.service`: a symlink
whose target does not exist, `is-enabled` = `not-found`, referenced by no
unit, drop-in or timer, and a name this repository has never installed (the
real one is `voyn-aicc-self-deploy.service`). It could not be deleted on
2026-08-30 — `sudo rm` asked for a password — which is what made the gap
concrete.

It is removed by the next principal-isolation install on that host: the
root-owned exact-SHA bootstrap of step 5
(`/usr/local/sbin/voyn-aicc-bootstrap --expected-sha <merged-main-sha>`),
which is what runs `deploy/install-agent-principal-isolation.sh`. The
per-host self-deploy tick does not run the installer, so merging this change
alone does not delete anything. Confirm the contract around that install:

```sh
# Before: the dangling link, and the proof it backs nothing.
ls -l /etc/systemd/system/aicc-systemd-voyn-aicc-self-deploy.service
systemctl is-enabled aicc-systemd-voyn-aicc-self-deploy.service   # not-found

# After the install commits (the installer runs `systemctl daemon-reload`
# between apply and commit):
test ! -e /etc/systemd/system/aicc-systemd-voyn-aicc-self-deploy.service \
  && echo REMOVED
systemctl is-enabled voyn-aicc-self-deploy.timer                  # untouched
systemctl list-units --failed
```
