#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-principal-isolation.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
workspace_authority_env=/etc/aicc/workspace-authority.env
state_dir=/var/lib/aicc-principal-isolation
baseline_units="$state_dir/baseline-units.json"
baseline_release="$state_dir/baseline-release"
attempt_units="$state_dir/attempt-units.json"
pending_release="$state_dir/pending-release"
transaction="$repo_root/ops/aicc_install_transaction.py"
rollout="$repo_root/ops/aicc_staged_worker_rollout.py"
repo_lanes="$repo_root/deploy/aicc/worker-lanes"
release_root=/opt/aicc/releases
current_release=/opt/aicc/current
release_staging=
previous_release=
release_selected=0

run_transaction() {
  action=$1
  python3 "$transaction" "$action" \
    --repo-root "$repo_root" \
    --state-dir "$state_dir" \
    --authority-env "$workspace_authority_env"
}

run_rollout() {
  python3 "$rollout" "$@"
}

stage_immutable_release() {
  release_id=$(git -C "$repo_root" rev-parse --verify HEAD)
  case "$release_id" in
    *[!0-9a-f]*|'') echo "invalid release commit" >&2; exit 1 ;;
  esac
  release_dir="$release_root/$release_id"
  install -d -m 0755 -o root -g root "$release_root"
  if [ ! -d "$release_dir" ]; then
    release_staging=$(mktemp -d "$release_root/.stage-$release_id.XXXXXX")
    # Archive the committed tree, never the operator worktree. This makes the
    # release identity equal to committed content even when the checkout has
    # unrelated untracked files.
    git -C "$repo_root" archive --format=tar HEAD | tar -xf - -C "$release_staging"
    python3 -m venv "$release_staging/.venv"
    "$release_staging/.venv/bin/python" -m pip install \
      --disable-pip-version-check --require-hashes \
      -r "$release_staging/requirements-ci-linux.lock"
    PYTHONPATH="$release_staging" "$release_staging/.venv/bin/python" - <<'PY'
from command_center import worker
assert worker is not None
PY
    chown -R root:root "$release_staging"
    chmod -R a-w "$release_staging"
    mv -- "$release_staging" "$release_dir"
    release_staging=
  fi
  [ "$(stat -c %U:%G "$release_dir")" = root:root ] || {
    echo "release ownership drifted" >&2
    exit 1
  }
  previous_release=$(readlink "$current_release" 2>/dev/null || true)
  if [ -n "$previous_release" ] && \
     ! printf '%s\n' "$previous_release" | grep -Eq '^releases/[0-9a-f]{40}$'; then
    echo "previous release selector is invalid" >&2
    exit 1
  fi
  pending_release_tmp="$state_dir/.pending-release.$$"
  if [ -n "$previous_release" ]; then
    printf '%s\n' "$previous_release" >"$pending_release_tmp"
  else
    printf '%s\n' ABSENT >"$pending_release_tmp"
  fi
  chmod 0600 "$pending_release_tmp"
  mv -f -- "$pending_release_tmp" "$pending_release"
  sync -f "$state_dir"
  current_tmp="/opt/aicc/.current.$$"
  ln -s "releases/$release_id" "$current_tmp"
  mv -Tf -- "$current_tmp" "$current_release"
  release_selected=1
}

if [ "${1:-}" = "--uninstall" ]; then
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
  systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
  systemctl disable aicc-principal-recovery.service >/dev/null 2>&1 || true
  run_transaction uninstall
  systemctl daemon-reload
  if [ "$baseline_release_value" = ABSENT ]; then
    rm -f -- "$current_release"
  else
    baseline_tmp="/opt/aicc/.current-uninstall.$$"
    ln -s "$baseline_release_value" "$baseline_tmp"
    mv -Tf -- "$baseline_tmp" "$current_release"
  fi
  run_rollout restore --state "$baseline_units"
  rm -f -- "$baseline_units" "$baseline_release" "$attempt_units"
  echo "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED"
  exit 0
fi

# A prior SIGKILL leaves a durable write-ahead journal. Recover it before
# validating or preparing a new versioned generation.
run_transaction recover

# Validate the stable authority using the exact runtime decoder before any
# replaceable target is mutated.
PYTHONPATH="$repo_root" python3 - "$workspace_authority_env" <<'PY'
import pathlib
import sys

from command_center.workspace_authority import load_workspace_authority_environment

load_workspace_authority_environment(pathlib.Path(sys.argv[1]))
PY

for tool in /usr/local/bin/claude /usr/local/bin/codex /usr/local/bin/copilot; do
  resolved=$(readlink -f -- "$tool" 2>/dev/null || true)
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
# snapshotted even when absent from the repo lane file. The two legacy units
# below mirror LEGACY_WORKER_UNITS in ops/aicc_staged_worker_rollout.py --
# keep the lists in lockstep.
run_rollout snapshot --lanes "$repo_lanes" --state "$attempt_units" \
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
  # Restore the old immutable release selector before the transaction may
  # restart any service from its snapshot. Otherwise an old unit path through
  # /opt/aicc/current could execute the failed generation during recovery.
  if [ "$release_selected" -eq 1 ]; then
    if [ -n "$previous_release" ]; then
      previous_tmp="/opt/aicc/.current-rollback.$$"
      ln -s "$previous_release" "$previous_tmp"
      mv -Tf -- "$previous_tmp" "$current_release"
    elif [ -L "$current_release" ]; then
      rm -f -- "$current_release"
    fi
    release_selected=0
    if [ "$transaction_active" -eq 0 ]; then
      rm -f -- "$pending_release"
      sync -f "$state_dir"
    fi
  fi
  if [ "$transaction_active" -eq 1 ] && [ -f "$state_dir/pending.json" ]; then
    systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
    # The uninstall path disables this too; a rolled-back FIRST install must
    # not leave the enable symlink dangling after the transaction removes the
    # unit file (independent-review finding on d661d8f).
    systemctl disable aicc-principal-recovery.service >/dev/null 2>&1 || true
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
  fi
  exit "$result"
}
trap rollback EXIT HUP INT TERM

# Identity and directory creation are additive/idempotent prerequisites. Every
# replaceable file belongs to the versioned, write-ahead transaction below.
systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-agent.conf"
systemd-tmpfiles --create "$repo_root/deploy/tmpfiles.d/aicc-agent.conf"
run_transaction prepare
transaction_active=1
run_transaction apply
# Build and atomically select the committed, immutable tree + virtualenv only
# after boot recovery itself is installed, but before any new unit can start.
stage_immutable_release

systemctl daemon-reload
systemctl enable aicc-principal-recovery.service
systemctl enable --now aicc-agent-launcher.socket

# The orchestrator discovers configured plus already-instantiated lanes, then
# drains, starts and proves each lane before advancing. Any failure restores
# the attempt snapshot and the outer trap restores the file generation.
# /etc/aicc/worker-lanes is installed by the transaction itself (default_specs
# maps deploy/aicc/worker-lanes onto it) before this rollout runs on
# a fresh host and matches the snapshot origin (reviewed on 8a881d3).
run_rollout rollout --lanes /etc/aicc/worker-lanes
"$repo_root/ops/verify-agent-principal-boundary.sh"

run_transaction commit
transaction_active=0
rm -f -- "$attempt_units"
trap - EXIT HUP INT TERM
echo "AICC_AGENT_PRINCIPAL_ISOLATION_INSTALLED"
