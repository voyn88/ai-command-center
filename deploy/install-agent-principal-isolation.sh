#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-principal-isolation.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
worker_env=/home/voynadmin/aicc-preprod/worker.env
if [ ! -f "$worker_env" ]; then
  worker_env=/etc/aicc/worker.env
fi

if [ ! -f "$worker_env" ]; then
  echo "worker environment is missing; cannot prove workspace authority separation" >&2
  exit 1
fi

# Parse, but never print, the stable checkpoint authority. EnvironmentFile is
# not a shell program and must not be sourced by this privileged installer.
python3 - "$worker_env" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
values = []
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() == "AICC_WORKSPACE_AUTHORITY_KEY":
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.append(value)
if len(values) != 1 or len(values[0].encode("utf-8")) < 32:
    raise SystemExit(
        "exactly one dedicated AICC_WORKSPACE_AUTHORITY_KEY of at least 32 bytes is required"
    )
PY

for tool in /usr/local/bin/claude /usr/local/bin/codex /usr/local/bin/copilot; do
  resolved=$(readlink -f -- "$tool" 2>/dev/null || true)
  if [ -z "$resolved" ] || [ ! -x "$resolved" ] || \
     [ "$(stat -c %u -- "$resolved" 2>/dev/null || echo -1)" -ne 0 ]; then
    echo "root-owned executor missing: $tool; run deploy/install-agent-toolchain.sh" >&2
    exit 1
  fi
done

install -D -o root -g root -m 0644 \
  "$repo_root/deploy/sysusers.d/aicc-agent.conf" \
  /usr/lib/sysusers.d/aicc-agent.conf
systemd-sysusers /usr/lib/sysusers.d/aicc-agent.conf

install -D -o root -g root -m 0644 \
  "$repo_root/deploy/tmpfiles.d/aicc-agent.conf" \
  /usr/lib/tmpfiles.d/aicc-agent.conf
systemd-tmpfiles --create /usr/lib/tmpfiles.d/aicc-agent.conf
"$repo_root/deploy/migrate-agent-model-auth.sh"

# The publisher can read this through its aicc-publisher supplementary group;
# aicc-agent is deliberately not a member. This removes same-UID ownership of
# the lease/HMAC environment before isolated execution is enabled.
chown root:aicc-publisher "$worker_env"
chmod 0640 "$worker_env"

install -D -o root -g root -m 0755 \
  "$repo_root/ops/aicc_agent_launcher.py" \
  /usr/libexec/aicc-agent-launcher
install -D -o root -g root -m 0644 \
  "$repo_root/deploy/systemd/aicc-agent-launcher.socket" \
  /etc/systemd/system/aicc-agent-launcher.socket
install -D -o root -g root -m 0644 \
  "$repo_root/deploy/systemd/aicc-agent-launcher@.service" \
  /etc/systemd/system/aicc-agent-launcher@.service

if [ ! -e /etc/aicc/agent-workspace-roots ]; then
  install -D -o root -g root -m 0644 \
    "$repo_root/deploy/aicc/agent-workspace-roots" \
    /etc/aicc/agent-workspace-roots
fi
if [ ! -e /etc/aicc/agent.env ]; then
  install -D -o root -g aicc-agent -m 0640 \
    "$repo_root/deploy/aicc/agent.env" /etc/aicc/agent.env
fi

install -D -o root -g root -m 0644 \
  "$repo_root/deploy/aicc/publisher-secret-paths" \
  /etc/aicc/publisher-secret-paths
for unit in voyn-aicc-worker.service voyn-aicc-worker-2.service; do
  install -D -o root -g root -m 0644 \
    "$repo_root/deploy/systemd/voyn-aicc-worker-principal-isolation.conf" \
    "/etc/systemd/system/$unit.d/20-principal-isolation.conf"
done

# Authority separation is invalid if the agent can read even one publisher
# path. Missing optional paths are ignored; every path that exists is tested
# as the real execution UID, not inferred only from mode bits.
while IFS= read -r secret_path; do
  case "$secret_path" in ''|'#'*) continue ;; esac
  if [ -e "$secret_path" ] && runuser -u aicc-agent -- test -r "$secret_path"; then
    echo "aicc-agent can read publisher authority path: $secret_path" >&2
    exit 1
  fi
done < /etc/aicc/publisher-secret-paths

systemctl daemon-reload
systemctl enable --now aicc-agent-launcher.socket
systemctl is-active --quiet aicc-agent-launcher.socket

echo "AICC_AGENT_PRINCIPAL_ISOLATION_INSTALLED"
