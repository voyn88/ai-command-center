#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "migrate-agent-model-auth.sh must run as root" >&2
  exit 1
fi

publisher_home=${1:-/home/voynadmin}

copy_model_auth() {
  source_path=$1
  target_path=$2
  if [ ! -f "$source_path" ]; then
    echo "model auth source missing: $source_path" >&2
    return 1
  fi
  install -D -o root -g root -m 0600 "$source_path" "$target_path"
}

copy_model_auth \
  "$publisher_home/.claude/.credentials.json" \
  /var/lib/aicc-agent/claude/.claude/.credentials.json
copy_model_auth \
  "$publisher_home/.codex/auth.json" \
  /var/lib/aicc-agent/codex/.codex/auth.json
echo "AICC_AGENT_MODEL_AUTH_MIGRATED"
