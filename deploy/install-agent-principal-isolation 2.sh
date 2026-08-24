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
attempt_units="$state_dir/attempt-units.json"
transaction="$repo_root/ops/aicc_install_transaction.py"
rollout="$repo_root/ops/aicc_staged_worker_rollout.py"
repo_lanes="$repo_root/deploy/aicc/worker-lanes"

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

if [ "${1:-}" = "--uninstall" ]; then
  [ -f "$baseline_units" ] || {
    echo "principal-isolation baseline state is missing" >&2
    exit 1
  }
  systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
  systemctl disable aicc-principal-recovery.service >/dev/null 2>&1 || true
  run_transaction uninstall
  systemctl daemon-reload
  run_rollout restore --state "$baseline_units"
  rm -f -- "$baseline_units" "$attempt_units"
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
run_rollout snapshot --lanes "$repo_lanes" --state "$attempt_units" \
  --include-unit aicc-agent-launcher.socket \
  --include-unit aicc-principal-recovery.service
transaction_active=0
baseline_created=0

if [ ! -f "$baseline_units" ]; then
  baseline_tmp="$state_dir/.baseline-units.$$"
  install -m 0600 "$attempt_units" "$baseline_tmp"
  mv -f -- "$baseline_tmp" "$baseline_units"
  sync -f "$state_dir"
  baseline_created=1
fi

rollback() {
  result=$?
  trap - EXIT HUP INT TERM
  rollback_complete=1
  if [ "$transaction_active" -eq 1 ] && [ -f "$state_dir/pending.json" ]; then
    systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
    if ! run_transaction recover; then
      # Keep pending.json, its generation, and attempt-units.json intact.
      # The boot recovery unit retries the same compare-and-restore plus
      # service snapshot instead of silently discarding failed stop/disable.
      rollback_complete=0
      echo "principal-isolation rollback incomplete; durable WAL retained" >&2
    fi
  fi
  if [ "$rollback_complete" -eq 1 ] && [ "$baseline_created" -eq 1 ]; then
    rm -f -- "$baseline_units"
  fi
  if [ "$rollback_complete" -eq 1 ]; then
    rm -f -- "$attempt_units"
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

systemctl daemon-reload
systemctl enable aicc-principal-recovery.service
systemctl enable --now aicc-agent-launcher.socket

# The orchestrator discovers configured plus already-instantiated lanes, then
# drains, starts and proves each lane before advancing. Any failure restores
# the attempt snapshot and the outer trap restores the file generation.
run_rollout rollout --lanes /etc/aicc/worker-lanes
"$repo_root/ops/verify-agent-principal-boundary.sh"

run_transaction commit
transaction_active=0
rm -f -- "$attempt_units"
trap - EXIT HUP INT TERM
echo "AICC_AGENT_PRINCIPAL_ISOLATION_INSTALLED"
