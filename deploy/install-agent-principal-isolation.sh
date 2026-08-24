#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-principal-isolation.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
workspace_authority_env=/etc/aicc/workspace-authority.env
state_dir=/var/lib/aicc-principal-isolation
service_state="$state_dir/service-state"
transaction="$repo_root/ops/aicc_install_transaction.py"

run_transaction() {
  action=$1
  python3 "$transaction" "$action" \
    --repo-root "$repo_root" \
    --state-dir "$state_dir" \
    --authority-env "$workspace_authority_env"
}

read_state_value() {
  key=$1
  sed -n "s/^$key=//p" "$service_state" | tail -n 1
}

if [ "${1:-}" = "--uninstall" ]; then
  [ -f "$service_state" ] || {
    echo "principal-isolation service state is missing" >&2
    exit 1
  }
  socket_was_enabled=$(read_state_value socket_enabled)
  socket_was_active=$(read_state_value socket_active)
  lane1_was_enabled=$(read_state_value lane1_enabled)
  lane2_was_enabled=$(read_state_value lane2_enabled)
  systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
  run_transaction uninstall
  systemctl daemon-reload
  if [ "$lane1_was_enabled" != enabled ]; then
    systemctl disable voyn-aicc-worker@1.service >/dev/null 2>&1 || true
  fi
  if [ "$lane2_was_enabled" != enabled ]; then
    systemctl disable voyn-aicc-worker@2.service >/dev/null 2>&1 || true
  fi
  if [ "$socket_was_enabled" = enabled ]; then
    systemctl enable aicc-agent-launcher.socket >/dev/null 2>&1 || true
  fi
  if [ "$socket_was_active" = active ]; then
    systemctl start aicc-agent-launcher.socket >/dev/null 2>&1 || true
  fi
  rm -f -- "$service_state"
  echo "AICC_AGENT_PRINCIPAL_ISOLATION_UNINSTALLED"
  exit 0
fi

# Validate every secret and versioned input before the first mutation. The
# installer and runtime call the same decoder, so encoded length cannot drift.
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

socket_was_enabled=$(systemctl is-enabled aicc-agent-launcher.socket 2>/dev/null || true)
socket_was_active=$(systemctl is-active aicc-agent-launcher.socket 2>/dev/null || true)
lane1_was_enabled=$(systemctl is-enabled voyn-aicc-worker@1.service 2>/dev/null || true)
lane2_was_enabled=$(systemctl is-enabled voyn-aicc-worker@2.service 2>/dev/null || true)
transaction_active=0

restore_service_state() {
  if [ "$lane1_was_enabled" != enabled ]; then
    systemctl disable voyn-aicc-worker@1.service >/dev/null 2>&1 || true
  fi
  if [ "$lane2_was_enabled" != enabled ]; then
    systemctl disable voyn-aicc-worker@2.service >/dev/null 2>&1 || true
  fi
  if [ "$socket_was_enabled" != enabled ]; then
    systemctl disable aicc-agent-launcher.socket >/dev/null 2>&1 || true
  fi
  if [ "$socket_was_active" = active ]; then
    systemctl start aicc-agent-launcher.socket >/dev/null 2>&1 || true
  else
    systemctl stop aicc-agent-launcher.socket >/dev/null 2>&1 || true
  fi
}

rollback() {
  result=$?
  trap - EXIT HUP INT TERM
  if [ "$transaction_active" -eq 1 ]; then
    systemctl disable --now aicc-agent-launcher.socket >/dev/null 2>&1 || true
    run_transaction rollback || true
    systemctl daemon-reload || true
    restore_service_state
    rm -f -- "$service_state"
  fi
  exit "$result"
}
trap rollback EXIT HUP INT TERM

# Identity and directory creation are additive/idempotent prerequisites. All
# replaceable files and migrated model credentials are installed only by the
# reversible transaction below.
systemd-sysusers "$repo_root/deploy/sysusers.d/aicc-agent.conf"
systemd-tmpfiles --create "$repo_root/deploy/tmpfiles.d/aicc-agent.conf"
run_transaction install
transaction_active=1

umask 077
service_state_tmp="$state_dir/.service-state.$$"
{
  printf 'socket_enabled=%s\n' "$socket_was_enabled"
  printf 'socket_active=%s\n' "$socket_was_active"
  printf 'lane1_enabled=%s\n' "$lane1_was_enabled"
  printf 'lane2_enabled=%s\n' "$lane2_was_enabled"
} > "$service_state_tmp"
chmod 0600 "$service_state_tmp"
mv -f -- "$service_state_tmp" "$service_state"

systemctl daemon-reload
systemctl enable voyn-aicc-worker@1.service voyn-aicc-worker@2.service
systemctl enable --now aicc-agent-launcher.socket
"$repo_root/ops/verify-agent-principal-boundary.sh"

transaction_active=0
trap - EXIT HUP INT TERM
echo "AICC_AGENT_PRINCIPAL_ISOLATION_INSTALLED"
