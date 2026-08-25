#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "verify-agent-principal-boundary.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
secret_manifest=/etc/aicc/publisher-secret-paths
lane_registry=/etc/aicc/worker-lanes
worker_template=/etc/systemd/system/voyn-aicc-worker@.service
worker_dropin=/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf
principal_inaccessible_paths="/etc/aicc /etc/voyn /home /root /var/lib/aicc-worker /var/lib/aicc-agent /var/lib/voyn-aicc-credential-rotation /run/aicc-agent-launcher /run/credentials /run/voyn-aicc-worker /srv/aicc-quarantine"

fail() {
  echo "AICC_AGENT_PRINCIPAL_BOUNDARY_FAIL: $*" >&2
  exit 1
}

agent_uid=$(id -u aicc-agent 2>/dev/null) || fail "aicc-agent is missing"
publisher_uid=$(id -u voynadmin 2>/dev/null) || fail "publisher user is missing"
[ "$agent_uid" != "$publisher_uid" ] || fail "agent and publisher share a UID"
[ "$(getent passwd aicc-agent | cut -d: -f7)" = /usr/sbin/nologin ] || \
  fail "aicc-agent has a login shell"

id -nG aicc-agent | tr ' ' '\n' | grep -qx aicc-workspace || \
  fail "agent lacks the task-workspace group"
if id -nG aicc-agent | tr ' ' '\n' | grep -qx aicc-publisher; then
  fail "agent is a member of the publisher authority group"
fi
id -nG voynadmin | tr ' ' '\n' | grep -qx aicc-publisher || \
  fail "publisher lacks launcher access group"
getent group aicc-agent-auth >/dev/null || fail "aicc-agent-auth group is missing"
auth_members=$(getent group aicc-agent-auth | cut -d: -f4) || \
  fail "aicc-agent-auth group is missing"
[ -z "$auth_members" ] || fail "model-auth group has static members"
for static_principal in aicc-agent aicc-worker voynadmin; do
  if id -nG "$static_principal" | tr ' ' '\n' | grep -qx aicc-agent-auth; then
    fail "$static_principal is a static member of model-auth group"
  fi
done

systemctl is-active --quiet aicc-agent-launcher.socket || \
  fail "launcher socket is not active"
[ "$(stat -c %U:%G:%a /run/aicc-agent-launcher/control.sock)" = \
  "root:aicc-publisher:660" ] || fail "launcher socket permissions drifted"
if runuser -u aicc-agent -- test -w /run/aicc-agent-launcher/control.sock; then
  fail "agent can invoke its own privileged launcher"
fi

launcher=/usr/libexec/aicc-agent-launcher
[ "$(stat -Lc %U:%G:%a "$launcher")" = root:root:755 ] || \
  fail "launcher is not immutable root-owned"
expected_hash=$(sha256sum "$repo_root/ops/aicc_agent_launcher.py" | cut -d' ' -f1)
installed_hash=$(sha256sum "$launcher" | cut -d' ' -f1)
[ "$installed_hash" = "$expected_hash" ] || fail "installed launcher SHA drifted"

expected_template_hash=$(sha256sum "$repo_root/deploy/systemd/voyn-aicc-worker@.service" | cut -d' ' -f1)
installed_template_hash=$(sha256sum "$worker_template" | cut -d' ' -f1) || \
  fail "versioned worker template is not installed"
[ "$installed_template_hash" = "$expected_template_hash" ] || \
  fail "installed worker template SHA drifted"
expected_dropin_hash=$(sha256sum "$repo_root/deploy/systemd/voyn-aicc-worker-principal-isolation.conf" | cut -d' ' -f1)
installed_dropin_hash=$(sha256sum "$worker_dropin" | cut -d' ' -f1) || \
  fail "principal worker drop-in is not installed"
[ "$installed_dropin_hash" = "$expected_dropin_hash" ] || \
  fail "installed worker drop-in SHA drifted"
[ "$(stat -c %U:%G:%a /etc/aicc/workspace-authority.env)" = \
  "root:aicc-publisher:640" ] || fail "workspace authority ownership drifted"
PYTHONPATH="$repo_root" python3 - <<'PY' || fail "workspace authority encoding is invalid"
from pathlib import Path

from command_center.workspace_authority import load_workspace_authority_environment

load_workspace_authority_environment(Path("/etc/aicc/workspace-authority.env"))
PY

for provider_env in /etc/aicc/agent.env /etc/aicc/agent-claude.env /etc/aicc/agent-codex.env; do
  [ -e "$provider_env" ] || continue
  [ ! -L "$provider_env" ] || fail "provider environment is a symlink: $provider_env"
  [ "$(stat -c %U:%G:%a "$provider_env")" = root:aicc-agent:640 ] || \
    fail "provider environment ownership or mode drifted: $provider_env"
done

for model_auth in \
  /var/lib/aicc-agent/claude/.claude/.credentials.json \
  /var/lib/aicc-agent/codex/.codex/auth.json; do
  [ ! -L "$model_auth" ] || fail "model auth target is a symlink: $model_auth"
  [ "$(stat -c %U:%G:%a "$model_auth")" = root:root:600 ] || \
    fail "model auth target ownership or mode drifted: $model_auth"
done

# Measured from inside the same mount/capability envelope: the exact workspace
# is visible, while an equally group-readable sibling under the canonical root
# is EACCES because the original root is hidden before the exact bind.
canary_root=$(mktemp -d /srv/aicc-workspaces/.principal-boundary.XXXXXX)
canary_workspace=$canary_root/workspace
canary_sibling=$canary_root/sibling
mkdir "$canary_workspace" "$canary_sibling"
chown -R root:aicc-workspace "$canary_root"
chmod 2770 "$canary_root" "$canary_workspace" "$canary_sibling"
: > "$canary_workspace/visible"
: > "$canary_sibling/must-not-read"
chown root:aicc-workspace "$canary_workspace/visible" "$canary_sibling/must-not-read"
chmod 0660 "$canary_workspace/visible" "$canary_sibling/must-not-read"
cleanup_canary() {
  rm -f "$canary_workspace/visible" "$canary_sibling/must-not-read"
  rmdir "$canary_workspace" "$canary_sibling" "$canary_root"
}
trap cleanup_canary EXIT HUP INT TERM
systemd-run --quiet --wait --pipe --collect \
  --property=DynamicUser=yes \
  --property=SupplementaryGroups=aicc-workspace\ aicc-agent-auth \
  --setenv=AICC_AGENT_PRINCIPAL_ISOLATION=required \
  --property=NoNewPrivileges=yes \
  --property=ProtectSystem=strict \
  --property="InaccessiblePaths=$principal_inaccessible_paths /srv/aicc-workspaces" \
  --property="BindPaths=$canary_workspace:/workspace" \
  -- /bin/sh -c \
  'test -r /workspace/visible && ! test -r /srv/aicc-workspaces/.principal-boundary.*/sibling/must-not-read' || \
  fail "exact-workspace sibling isolation canary failed"
cleanup_canary
trap - EXIT HUP INT TERM

# Two simultaneously active units must receive different kernel UIDs.  This
# is the measured boundary that prevents same-UID /proc access and same-UID
# signalling across concurrent task runs; configuration text alone is not
# accepted as evidence.
principal_unit_a="aicc-principal-canary-a-$$.service"
principal_unit_b="aicc-principal-canary-b-$$.service"
principal_secret=$(mktemp /etc/aicc/.principal-boundary-secret.XXXXXX)
chown root:aicc-agent-auth "$principal_secret"
chmod 0640 "$principal_secret"
cleanup_principal_units() {
  systemctl stop "$principal_unit_a" "$principal_unit_b" >/dev/null 2>&1 || true
  systemctl reset-failed "$principal_unit_a" "$principal_unit_b" >/dev/null 2>&1 || true
  rm -f -- "$principal_secret"
}
trap cleanup_principal_units EXIT HUP INT TERM
for principal_unit in "$principal_unit_a" "$principal_unit_b"; do
  systemd-run --quiet --unit="$principal_unit" \
    --property=DynamicUser=yes \
    --property=SupplementaryGroups=aicc-workspace\ aicc-agent-auth \
    --setenv=AICC_AGENT_PRINCIPAL_ISOLATION=required \
    --property="InaccessiblePaths=$principal_inaccessible_paths" \
    --property=ProtectProc=invisible \
    --property=ProcSubset=pid \
    --property=NoNewPrivileges=yes \
    --property=CapabilityBoundingSet= \
    -- /bin/sh -c "test ! -r '$principal_secret' && sleep 30" || \
    fail "cannot start sealed dynamic-principal canary"
done
# Prove the exact effective transient executor identity and model-auth group,
# not merely the command text used to request them. The Python fleet verifier
# below separately applies the same fail-closed check to every discovered
# worker lane before rollout and after each start.
for isolated_unit in "$principal_unit_a" "$principal_unit_b"; do
  [ "$(systemctl show "$isolated_unit" --property=DynamicUser --value)" = yes ] || \
    fail "transient executor is not DynamicUser: $isolated_unit"
  unit_groups=$(systemctl show "$isolated_unit" --property=SupplementaryGroups --value)
  printf '%s\n' "$unit_groups" | tr ' ' '\n' | grep -qx aicc-agent-auth || \
    fail "transient executor lacks model-auth group: $isolated_unit"
  if printf '%s\n' "$unit_groups" | tr ' ' '\n' | grep -qx aicc-publisher; then
    fail "transient executor inherited publisher group: $isolated_unit"
  fi
done
# The fail-closed flag travels via the drop-in to the WORKER unit families --
# the legacy single unit and every template lane. Transient canaries never
# carry the drop-in, and demanding the flag there failed a healthy host
# before its measurement ran (review on 9de8193); template lanes went
# unverified for the same reason.
worker_family_units="aicc-worker.service"
# list-unit-files enumerates INSTALLED template instances (list-units shows
# only loaded ones -- empty before rollout or after a failed rollout
# disabled them, degenerating this loop to a vacuous single-unit check;
# review on 27c06df). Read the enabled lanes from the root-owned registry,
# the same authority the rotator uses, and refuse an empty lane set.
[ -r "$lane_registry" ] || fail "worker lane registry is missing: $lane_registry"
[ ! -L "$lane_registry" ] || fail "worker lane registry is a symlink: $lane_registry"
[ -f "$lane_registry" ] || fail "worker lane registry is not a regular file: $lane_registry"
lane_family_units=
while IFS= read -r lane || [ -n "$lane" ]; do
  lane=$(printf '%s' "$lane" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  case "$lane" in
    ''|'#'*) continue ;;
  esac
  case "$lane" in
    voyn-aicc-worker@*.service)
      lane=${lane#voyn-aicc-worker@}
      lane=${lane%.service}
      ;;
  esac
  printf '%s\n' "$lane" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$' || \
    fail "invalid worker lane in registry: $lane"
  family_unit="voyn-aicc-worker@$lane.service"
  case " $lane_family_units " in
    *" $family_unit "*) fail "duplicate worker lane in registry: $lane" ;;
  esac
  lane_family_units="$lane_family_units $family_unit"
done < "$lane_registry"
[ -n "$lane_family_units" ] || fail "no worker lanes found in the registry to verify"
for family_unit in $worker_family_units $lane_family_units; do
  family_env=$(systemctl show "$family_unit" --property=Environment --value)
  family_flag=$(printf '%s\n' "$family_env" | tr ' ' '\n' | \
    grep '^AICC_AGENT_PRINCIPAL_ISOLATION=' || true)
  [ "$family_flag" = 'AICC_AGENT_PRINCIPAL_ISOLATION=required' ] || \
    fail "isolation flag did not reach $family_unit exactly"
done

principal_pid_a=$(systemctl show "$principal_unit_a" --property=MainPID --value)
principal_pid_b=$(systemctl show "$principal_unit_b" --property=MainPID --value)
case "$principal_pid_a:$principal_pid_b" in
  ''|*:|:*|*[!0-9:]*|0:*|*:0) fail "dynamic-principal canary has no live PID" ;;
esac
principal_uid_a=$(stat -c %u "/proc/$principal_pid_a")
principal_uid_b=$(stat -c %u "/proc/$principal_pid_b")
[ "$principal_uid_a" != "$principal_uid_b" ] || \
  fail "concurrent agents share a kernel UID"
cleanup_principal_units
trap - EXIT HUP INT TERM

[ -r "$secret_manifest" ] || fail "publisher secret manifest is missing"
while IFS= read -r secret_path; do
  case "$secret_path" in ''|'#'*) continue ;; esac
  if [ -e "$secret_path" ] && runuser -u aicc-agent -- test -r "$secret_path"; then
    fail "agent can read publisher authority path: $secret_path"
  fi
done < "$secret_manifest"

python3 "$repo_root/ops/aicc_staged_worker_rollout.py" verify \
  --lanes "$lane_registry" || fail "worker lane readiness or UID isolation failed"

for tool in /usr/local/bin/claude /usr/local/bin/codex /usr/local/bin/copilot; do
  resolved=$(readlink -f -- "$tool")
  [ -x "$resolved" ] || fail "executor is missing: $tool"
  [ "$(stat -c %u "$resolved")" -eq 0 ] || fail "executor is not root-owned: $tool"
  if find "$resolved" -maxdepth 0 -perm /022 -print -quit | grep -q .; then
    fail "executor is group/world-writable: $tool"
  fi
done

echo "AICC_AGENT_PRINCIPAL_BOUNDARY_OK"
