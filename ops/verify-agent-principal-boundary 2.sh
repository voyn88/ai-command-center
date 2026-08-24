#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "verify-agent-principal-boundary.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
secret_manifest=/etc/aicc/publisher-secret-paths
worker_template=/etc/systemd/system/voyn-aicc-worker@.service
worker_dropin=/etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf

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
  --uid=aicc-agent --gid=aicc-agent \
  --property=SupplementaryGroups=aicc-workspace \
  --property=NoNewPrivileges=yes \
  --property=ProtectSystem=strict \
  --property="InaccessiblePaths=/srv/aicc-workspaces" \
  --property="BindPaths=$canary_workspace:/workspace" \
  -- /bin/sh -c \
  'test -r /workspace/visible && ! test -r /srv/aicc-workspaces/.principal-boundary.*/sibling/must-not-read' || \
  fail "exact-workspace sibling isolation canary failed"
cleanup_canary
trap - EXIT HUP INT TERM

[ -r "$secret_manifest" ] || fail "publisher secret manifest is missing"
while IFS= read -r secret_path; do
  case "$secret_path" in ''|'#'*) continue ;; esac
  if [ -e "$secret_path" ] && runuser -u aicc-agent -- test -r "$secret_path"; then
    fail "agent can read publisher authority path: $secret_path"
  fi
done < "$secret_manifest"

python3 "$repo_root/ops/aicc_staged_worker_rollout.py" verify \
  --lanes /etc/aicc/worker-lanes || fail "worker lane readiness or UID isolation failed"

for tool in /usr/local/bin/claude /usr/local/bin/codex /usr/local/bin/copilot; do
  resolved=$(readlink -f -- "$tool")
  [ -x "$resolved" ] || fail "executor is missing: $tool"
  [ "$(stat -c %u "$resolved")" -eq 0 ] || fail "executor is not root-owned: $tool"
  if find "$resolved" -maxdepth 0 -perm /022 -print -quit | grep -q .; then
    fail "executor is group/world-writable: $tool"
  fi
done

echo "AICC_AGENT_PRINCIPAL_BOUNDARY_OK"
