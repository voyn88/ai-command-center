#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-principal-isolation.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
: "${AICC_BOOTSTRAP_ATTESTATION:?exact-SHA bootstrap attestation is required}"
: "${AICC_EXPECTED_RELEASE_SHA:?exact-SHA release identity is required}"
: "${AICC_INSTALL_LOCK_FD:?inherited host install lock is required}"
case "$AICC_INSTALL_LOCK_FD" in
  *[!0-9]*|'') echo "invalid inherited host install lock descriptor" >&2; exit 1 ;;
esac
[ -e "/proc/self/fd/$AICC_INSTALL_LOCK_FD" ] || {
  echo "inherited host install lock descriptor is closed" >&2
  exit 1
}
case "$AICC_EXPECTED_RELEASE_SHA" in
  *[!0-9a-f]*|'') echo "invalid expected release commit" >&2; exit 1 ;;
esac
[ "${#AICC_EXPECTED_RELEASE_SHA}" -eq 40 ] || {
  echo "invalid expected release commit length" >&2
  exit 1
}
/usr/bin/python3 "$repo_root/ops/aicc_exact_sha_bootstrap.py" \
  --expected-sha "$AICC_EXPECTED_RELEASE_SHA" \
  --verify-attestation "$AICC_BOOTSTRAP_ATTESTATION" \
  --repo-root "$repo_root"
# The verifier proves this descriptor is the fixed named root-owned inode and
# re-flocks the inherited open-file-description. The shell keeps that same OFD
# open, so the lock spans rollout/systemctl gaps between transaction children.
/usr/bin/flock -n "$AICC_INSTALL_LOCK_FD"
workspace_authority_env=/etc/aicc/workspace-authority.env
state_dir=/var/lib/aicc-principal-isolation
baseline_units="$state_dir/baseline-units.json"
baseline_release="$state_dir/baseline-release"
attempt_units="$state_dir/attempt-units.json"
uninstall_units="$state_dir/uninstall-units.json"
release_manifest_dir="$state_dir/releases"
pending_release_manifest=
transaction="$repo_root/ops/aicc_install_transaction.py"
rollout="$repo_root/ops/aicc_staged_worker_rollout.py"
release_root=/opt/aicc/releases
toolchain_root=/opt/aicc/toolchains
current_release=/opt/aicc/current
release_staging=

path_present() {
  [ -e "$1" ] || [ -L "$1" ]
}

# Every privileged Git read here must be config-free AND must refuse
# replacement refs: `git archive HEAD` would otherwise stage a planted
# `refs/replace/<sha>` tree while the SHA comparison above still passed
# (independent review on aaf1a502). Kept in lockstep with `_git_argv` in
# ops/aicc_exact_sha_bootstrap.py.
git_trusted() {
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
  GIT_OPTIONAL_LOCKS=0 GIT_NO_REPLACE_OBJECTS=1 \
  /usr/bin/git --no-replace-objects \
    -c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.pager=cat \
    -c core.sshCommand=/bin/false -c core.gitProxy= -c core.symlinks=false \
    -c protocol.ext.allow=never -c protocol.file.allow=never \
    -c credential.helper= -c diff.external= \
    -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= \
    -c uploadpack.packObjectsHook= \
    "$@"
}

# Which host role is being installed. The bootstrap sets it; an unset value
# means "worker", so a caller that predates profiles installs what it always
# did. Anything else is refused here rather than passed down, so a typo can
# never silently install a narrower set of files than intended.
install_profile="${AICC_INSTALL_PROFILE:-worker}"
case "$install_profile" in
  worker|control) ;;
  *) echo "unknown AICC_INSTALL_PROFILE: $install_profile" >&2; exit 1 ;;
esac

run_transaction() {
  action=$1
  shift
  /usr/bin/python3 "$transaction" "$action" \
    --repo-root "$repo_root" \
    --state-dir "$state_dir" \
    --authority-env "$workspace_authority_env" \
    --lock-fd "$AICC_INSTALL_LOCK_FD" \
    --profile "$install_profile" \
    "$@"
}

run_rollout() {
  /usr/bin/python3 "$rollout" "$@"
}

run_release() {
  /usr/bin/python3 "$transaction" "$@" \
    --repo-root "$repo_root" \
    --state-dir "$state_dir" \
    --authority-env "$workspace_authority_env" \
    --lock-fd "$AICC_INSTALL_LOCK_FD"
}

stage_immutable_release() {
  release_id=$(git_trusted -C "$repo_root" rev-parse --verify HEAD)
  case "$release_id" in
    *[!0-9a-f]*|'') echo "invalid release commit" >&2; exit 1 ;;
  esac
  # The previous/baseline selectors only accept ^releases/[0-9a-f]{40}$; a
  # non-40 hex id written here would self-lockout a later run or uninstall
  # (review on 6e22b93).
  [ "${#release_id}" -eq 40 ] || { echo "invalid release commit length" >&2; exit 1; }
  [ "$release_id" = "$AICC_EXPECTED_RELEASE_SHA" ] || {
    echo "release commit differs from exact-SHA bootstrap attestation" >&2
    exit 1
  }
  release_dir="$release_root/$release_id"
  pending_release_manifest="$release_manifest_dir/$release_id.json"
  install -d -m 0755 -o root -g root "$release_root"
  # Reconcile a prior SIGKILL before building anything. A digest-matching
  # staging generation is published; an unattested incomplete stage is
  # discarded; ambiguous or unsafe state fails closed under the host lock.
  run_release release-reconcile \
    --release-root "$release_root" \
    --manifest "$pending_release_manifest" \
    --release-id "$release_id"
  if [ ! -d "$release_dir" ]; then
    release_staging=$(mktemp -d "$release_root/.stage-$release_id.XXXXXX")
    # Archive the committed tree, never the operator worktree. This makes the
    # release identity equal to committed content even when the checkout has
    # unrelated untracked files.
    git_trusted -C "$repo_root" archive --format=tar "$release_id" \
      | tar -xf - -C "$release_staging"
    /usr/bin/python3 -m venv "$release_staging/.venv"
    "$release_staging/.venv/bin/python" -m pip install \
      --disable-pip-version-check --require-hashes \
      -r "$release_staging/requirements-ci-linux.lock"
    PYTHONPATH="$release_staging" "$release_staging/.venv/bin/python" - <<'PY'
from command_center import worker
assert worker is not None
PY
    chown -R root:root "$release_staging"
    chmod -R a-w "$release_staging"
    # Record the root-owned content manifest from the staging tree BEFORE the
    # rename, so a release directory can never exist without the manifest that
    # authorises its later reuse. A crash between the two leaves only staging,
    # which the rollback trap removes.
    install -d -m 0700 -o root -g root "$release_manifest_dir"
    run_release release-record \
      --release-tree "$release_staging" \
      --manifest "$pending_release_manifest" \
      --release-id "$release_id"
    sync -f "$state_dir"
    run_release release-publish \
      --release-tree "$release_staging" \
      --release-root "$release_root" \
      --manifest "$pending_release_manifest" \
      --release-id "$release_id"
    release_staging=
  fi
  # Every generation -- freshly built or pre-existing -- must prove itself
  # against the root-owned manifest and the committed Git tree before it may
  # be selected. A same-name/wrong-tree, partial, hardlinked, symlink-swapped
  # or group-writable release is refused here rather than executed.
  run_release release-verify \
    --release-tree "$release_dir" \
    --manifest "$release_manifest_dir/$release_id.json" \
    --release-id "$release_id" \
    --verify-against-git
  run_release release-select \
    --release-id "$release_id" \
    --repo-root "$repo_root"
}

# The permanent boot-recovery anchor must precede both a fresh install WAL and
# a fresh uninstall WAL. An already-journalled uninstall is resumed only by
# its digest-bound capsule, so it cannot swap recovery code mid-transaction.
if ! path_present "$state_dir/uninstall.json"; then
  # Recover an existing install WAL with the exact anchor/capsule that wrote
  # it before replacing that anchor with code from this release.
  if path_present "$state_dir/pending.json"; then
    installed_anchor=/usr/lib/systemd/system-generators/aicc-principal-recovery
    [ -f "$installed_anchor" ] && [ -x "$installed_anchor" ] || {
      echo "unfinished install journal has no installed recovery anchor" >&2
      exit 1
    }
    "$installed_anchor" --recover "$state_dir"
    ! path_present "$state_dir/pending.json" || {
      echo "installed recovery anchor left the install journal unresolved" >&2
      exit 1
    }
  fi
  run_transaction recovery-anchor-install
  # Resolve any earlier install WAL under the already-held OFD, then load and
  # activate the permanent no-op barrier while NO journal exists. If activation
  # waited until after prepare(), its ExecStart capsule would contend on this
  # same host lock and make first install fail deterministically.
  run_transaction recover
  systemctl daemon-reload
  systemctl start aicc-principal-recovery.service
fi

if [ "${1:-}" = "--uninstall" ]; then
  if path_present "$state_dir/uninstall.json"; then
    uninstall_phase=$(run_transaction uninstall-status)
    run_transaction recover-uninstall-safe
    if [ "$uninstall_phase" != INTENT ]; then
      echo "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED"
      exit 0
    fi
    # INTENT precedes every uninstall mutation, so recovery safely aborted it.
    # Reactivate the no-op barrier before creating the replacement WAL.
    systemctl reset-failed aicc-principal-recovery.service >/dev/null 2>&1 || true
    systemctl start aicc-principal-recovery.service
  fi
  [ -f "$baseline_units" ] && [ -f "$baseline_release" ] || {
    echo "principal-isolation baseline state is missing" >&2
    exit 1
  }
  baseline_release_value=$(cat "$baseline_release")
  if [ "$baseline_release_value" != ABSENT ] && \
     ! printf '%s\n' "$baseline_release_value" | grep -Eq '^releases/[0-9a-f]{40}$'; then
    echo "baseline release selector is invalid" >&2
    exit 1
  fi
  # Prove the baseline release BEFORE any privileged mutation. Uninstall is
  # not a weaker moment than install -- the baseline is still code every
  # worker ExecStart will run -- and this branch has no rollback trap, so a
  # check that runs after `run_transaction uninstall` and the service disables
  # can only report a partial uninstall it cannot undo (independent review on
  # 25eb0a0c). No Git cross-check here: uninstall must work without a
  # repository checkout, so the root-owned manifest is the authority.
  if [ "$baseline_release_value" != ABSENT ]; then
    run_release release-verify \
      --release-tree "/opt/aicc/$baseline_release_value" \
      --manifest "$release_manifest_dir/${baseline_release_value#releases/}.json" \
      --release-id "${baseline_release_value#releases/}"
  fi
  # Write durable INTENT before snapshot creation, then bind ARMED to the
  # exact snapshot digest. A normal install refuses while this journal exists;
  # a retry can continue after the lane registry itself has been removed.
  uninstall_phase=$(run_transaction uninstall-begin \
    --baseline-selector "$baseline_release_value" \
    --current-selector "$current_release" \
    --lane-registry /etc/aicc/worker-lanes)
  if [ "$uninstall_phase" = INTENT ]; then
    run_rollout snapshot --lanes /etc/aicc/worker-lanes --state "$uninstall_units" \
      --include-unit aicc-agent-launcher.socket \
      --include-unit aicc-principal-recovery.service \
      --include-unit voyn-aicc-worker.service \
      --include-unit voyn-aicc-worker-2.service \
      --include-unit aicc-worker.service
    sync -f "$state_dir"
  fi
  run_transaction uninstall-arm --service-snapshot "$uninstall_units"
  run_rollout verify-snapshot-closure --state "$uninstall_units"
  run_transaction quiesce --service-snapshot "$uninstall_units"
  run_rollout verify-snapshot-closure --state "$uninstall_units"
  systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
  # See the rollback trap: the socket's already-accepted sessions are separate
  # units and outlive it.
  systemctl stop 'aicc-agent-launcher@*.service' >/dev/null 2>&1 || true
  run_transaction uninstall
  systemctl daemon-reload
  run_transaction uninstall-select-baseline \
    --baseline-selector "$baseline_release_value"
  run_rollout restore --state "$baseline_units"
  run_rollout verify-snapshot-closure --state "$uninstall_units"
  run_transaction uninstall-complete --service-snapshot "$uninstall_units"
  echo "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED"
  exit 0
fi

# Validate the stable authority using the exact runtime decoder before any
# replaceable target is mutated.
PYTHONPATH="$repo_root" /usr/bin/python3 - "$workspace_authority_env" <<'PY'
import pathlib
import sys

from command_center.workspace_authority import load_workspace_authority_environment

load_workspace_authority_environment(pathlib.Path(sys.argv[1]))
PY

# Install the content-addressed provider toolchain before anything requires it.
# This replaces the previous `npm install --global` as root: the artifact is
# pinned by sha256 in a reviewed lock, downloaded, proven, extracted under a
# root-owned tree and selected atomically -- production resolves no package and
# executes no package lifecycle script
# (VOYN-W0-AICC-TOOLCHAIN-CONTENT-ADDRESSED).
/usr/bin/python3 "$repo_root/ops/aicc_toolchain_install.py" \
  --lock "$repo_root/deploy/agent-toolchain.lock.json"

for tool in "$toolchain_root/current/bin/claude" \
            "$toolchain_root/current/bin/codex" \
            "$toolchain_root/current/bin/copilot"; do
  resolved=$(readlink -f -- "$tool" 2>/dev/null || true)
  # The resolved target must stay inside the selected release: `current` is a
  # symlink and so are the `bin/` entries, so a link out of the proven tree
  # would otherwise pass every check below against some other file.
  case "$resolved" in
    "$toolchain_root"/releases/*) : ;;
    *) echo "executor resolves outside the selected toolchain: $tool" >&2; exit 1 ;;
  esac
  if [ -z "$resolved" ] || [ ! -x "$resolved" ] || \
     [ "$(stat -c %u -- "$resolved" 2>/dev/null || echo -1)" -ne 0 ] || \
     find "$resolved" -maxdepth 0 -perm /022 -print -quit | grep -q .; then
    echo "immutable root-owned executor missing: $tool" >&2
    exit 1
  fi
done

run_transaction validate
sh -n "$repo_root/ops/verify-agent-principal-boundary.sh"

if [ -e "$state_dir" ]; then
  [ ! -L "$state_dir" ] && [ -d "$state_dir" ] && \
    [ "$(stat -c %U:%G:%a "$state_dir")" = root:root:700 ] || {
      echo "principal-isolation state directory drifted" >&2
      exit 1
    }
else
  install -d -m 0700 -o root -g root "$state_dir"
fi
# NOTE: snapshot's own discover_units() additionally folds in every loaded/
# enabled voyn-aicc-worker@* template instance, so runtime-only lanes are
# snapshotted even when absent from the installed lane registry. The two
# legacy units below mirror LEGACY_WORKER_UNITS in
# ops/aicc_staged_worker_rollout.py -- keep the lists in lockstep.
run_rollout snapshot --lanes "$repo_root/deploy/aicc/worker-lanes" \
  --state "$attempt_units" \
  --include-unit aicc-agent-launcher.socket \
  --include-unit aicc-principal-recovery.service \
  --include-unit voyn-aicc-worker.service \
  --include-unit voyn-aicc-worker-2.service \
  --include-unit aicc-worker.service
transaction_active=0
baseline_created=0

if [ ! -f "$baseline_units" ]; then
  baseline_tmp="$state_dir/.baseline-units.$$"
  install -m 0600 "$attempt_units" "$baseline_tmp"
  mv -f -- "$baseline_tmp" "$baseline_units"
  sync -f "$state_dir"
  baseline_created=1
fi
if [ ! -f "$baseline_release" ]; then
  baseline_release_value=$(readlink "$current_release" 2>/dev/null || echo ABSENT)
  baseline_release_tmp="$state_dir/.baseline-release.$$"
  printf '%s\n' "$baseline_release_value" >"$baseline_release_tmp"
  chmod 0600 "$baseline_release_tmp"
  mv -f -- "$baseline_release_tmp" "$baseline_release"
  sync -f "$state_dir"
fi

rollback() {
  result=$?
  trap - EXIT HUP INT TERM
  rollback_complete=1
  if [ "$transaction_active" -eq 1 ] && path_present "$state_dir/pending.json"; then
    systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
    # Never kill accepted launcher sessions from the outer trap.  They carry
    # live client file descriptors which cannot be recreated by rollback.
    # `recover` restores the socket and workers; the transaction layer skips
    # these per-connection instances while restoring their on-disk template.
    if ! run_transaction recover; then
      # Keep pending.json, its generation, and attempt-units.json intact.
      # The boot recovery unit retries the same compare-and-restore plus
      # service snapshot instead of silently discarding failed stop/disable.
      rollback_complete=0
      echo "principal-isolation rollback incomplete; durable WAL retained" >&2
    fi
  fi
  if [ "$rollback_complete" -eq 1 ] && [ "$baseline_created" -eq 1 ]; then
    rm -f -- "$baseline_units" "$baseline_release"
  fi
  if [ "$rollback_complete" -eq 1 ]; then
    rm -f -- "$attempt_units"
  fi
  if [ -n "$release_staging" ] && [ -d "$release_staging" ]; then
    rm -rf -- "$release_staging"
    # Only a manifest recorded for a staging tree this attempt failed to
    # rename is removed. A manifest whose release directory exists stays: it
    # is the authority a later run and the boot recovery path verify against.
    if [ -n "$pending_release_manifest" ] && [ ! -d "$release_dir" ]; then
      rm -f -- "$pending_release_manifest"
      sync -f "$state_dir"
    fi
  fi
  exit "$result"
}
trap rollback EXIT HUP INT TERM

# Identity and directory creation are additive/idempotent prerequisites. Every
# other replaceable file belongs to the versioned transaction below.
#
# Both agent configs are worker-only: sysusers.d/aicc-agent.conf creates the
# `aicc-agent` execution principal and the workspace/credential groups, and
# tmpfiles.d/aicc-agent.conf creates /var/lib/aicc-agent (including the two
# credential homes this transaction purges on a control host), the launcher
# runtime directory and the workspace roots. Running either on a control host
# would build the agent layer this profile exists to keep off the control
# plane -- and would recreate, as an untracked side effect outside the
# transaction, the very directories the generation below is removing. The
# control host provisions only the identities its own specs install against:
# aicc-control-authority, which owns /etc/aicc/workspace-authority.env on this
# profile, and aicc-publisher, which the conversion below has to be able to
# take the worker-era principals out of. No tmpfiles entry is control-plane
# state, so none runs here.
if [ "$install_profile" = "worker" ]; then
  systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-agent.conf"
  systemd-tmpfiles --create "$repo_root/deploy/tmpfiles.d/aicc-agent.conf"
else
  systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-control.conf"
  # A name alone is not an authority boundary: a deleted/recreated group can
  # reuse a numeric gid still held by a worker process. Prove this control-only
  # group is empty, distinct and not held live before any generation is staged.
  run_transaction validate-control-authority
fi
run_transaction prepare
transaction_active=1
# A control host must not run the agent layer at all, and prepare() has just
# staged its removal as part of this same generation (default_specs pairs
# every WORKER_ONLY_TARGETS drop with an explicit removal spec). Stop and
# disable whatever the worker profile left running before apply() removes
# the unit files underneath it -- a host that never ran the worker profile
# has nothing loaded here and this is a no-op. A failure at this point still
# leaves an intact, recoverable pending generation: the rollback trap armed
# above calls `recover`, and no target has been mutated yet.
if [ "$install_profile" = "control" ]; then
  run_transaction quiesce-worker-only
  # deploy/sysusers.d/aicc-agent.conf put `aicc-worker` and `voynadmin` in
  # the publisher group on this host when it was a worker, and sysusers never
  # removes a membership, so without this those principals acquire it again
  # the next time they start. It is deliberately NOT the whole revocation:
  # a process that is already running holds the numeric gid until it exits,
  # so what takes the key away from the worker layer that is running right
  # now is the generation above installing /etc/aicc/workspace-authority.env
  # owned by aicc-control-authority instead. Journalled before it mutates
  # anything, bound to the generation prepare() just wrote, and undone by the
  # same `recover` the trap above runs, so a failure anywhere before commit
  # puts the memberships back.
  run_transaction revoke-worker-authority
fi
run_transaction apply
# Build and atomically select the committed, immutable tree + virtualenv only
# after boot recovery itself is installed, but before any new unit can start.
stage_immutable_release

systemctl daemon-reload
# The agent layer belongs to the worker profile only: the socket brokers agent
# principals, the rollout drives worker lanes, and the boundary verifier
# asserts an agent/publisher separation a control host has no parties for.
if [ "$install_profile" = "worker" ]; then
  systemctl enable --now aicc-agent-launcher.socket
fi

# The orchestrator discovers configured plus already-instantiated lanes, then
# drains, starts and proves each lane before advancing. Any failure restores
# the attempt snapshot and the outer trap restores the file generation.
# /etc/aicc/worker-lanes is installed by the transaction itself (default_specs
# maps deploy/aicc/worker-lanes onto it) before this rollout runs on
# a fresh host and matches the snapshot origin (reviewed on 8a881d3).
if [ "$install_profile" = "worker" ]; then
  run_rollout rollout --lanes /etc/aicc/worker-lanes
  "$repo_root/ops/verify-agent-principal-boundary.sh"
fi

run_transaction commit
transaction_active=0
rm -f -- "$attempt_units"
trap - EXIT HUP INT TERM
echo "AICC_AGENT_PRINCIPAL_ISOLATION_INSTALLED"
