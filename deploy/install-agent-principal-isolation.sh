#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-principal-isolation.sh must run as root" >&2
  exit 1
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
workspace_authority_env=/etc/aicc/workspace-authority.env

if [ ! -f "$workspace_authority_env" ]; then
  echo "dedicated workspace authority environment is missing" >&2
  exit 1
fi

# Parse, but never print, the stable checkpoint authority. EnvironmentFile is
# not a shell program and must not be sourced by this privileged installer.
python3 - "$workspace_authority_env" <<'PY'
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
info = path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise SystemExit(
        "workspace authority environment must be a root-owned non-writable regular file"
    )
values = []
unexpected = []
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key == "AICC_WORKSPACE_AUTHORITY_KEY":
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.append(value)
    else:
        unexpected.append(key)
if unexpected or len(values) != 1 or len(values[0].encode("utf-8")) < 32:
    raise SystemExit(
        "workspace authority environment must contain only one "
        "AICC_WORKSPACE_AUTHORITY_KEY of at least 32 bytes"
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

# The stable workspace authority has its own file and lifecycle. It must not
# share the rotator-managed DSN file or a lane-specific environment.
chown root:aicc-publisher "$workspace_authority_env"
chmod 0640 "$workspace_authority_env"

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
# Install once on the canonical template so every explicitly enabled lane
# inherits the same principal boundary without copy-pasted instance drift.
install -D -o root -g root -m 0644 \
  "$repo_root/deploy/systemd/voyn-aicc-worker-principal-isolation.conf" \
  /etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf

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
