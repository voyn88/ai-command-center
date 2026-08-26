#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install-agent-toolchain.sh must run as root" >&2
  exit 1
fi

# Exact deployed versions observed and canaried on worker-01. Updating these is
# a reviewed dependency change, not an ambient `latest` upgrade.
npm install --global --omit=dev --no-audit --no-fund \
  @anthropic-ai/claude-code@2.1.231 \
  @openai/codex@0.149.0 \
  @github/copilot@1.0.80

for tool in /usr/local/bin/claude /usr/local/bin/codex /usr/local/bin/copilot; do
  resolved=$(readlink -f -- "$tool")
  if [ ! -x "$resolved" ] || [ "$(stat -c %u -- "$resolved")" -ne 0 ]; then
    echo "executor is not a root-owned executable: $tool -> $resolved" >&2
    exit 1
  fi
  if find "$resolved" -maxdepth 0 -perm /022 -print -quit | grep -q .; then
    echo "executor is group/world writable: $resolved" >&2
    exit 1
  fi
done

npm_root=$(npm root --global)
for package_root in \
  "$npm_root/@anthropic-ai/claude-code" \
  "$npm_root/@openai/codex" \
  "$npm_root/@github/copilot"; do
  if [ ! -d "$package_root" ]; then
    echo "executor package tree is missing: $package_root" >&2
    exit 1
  fi
  if find "$package_root" \( ! -user root -o -perm /022 \) -print -quit | grep -q .; then
    echo "executor package tree is not immutable root-owned: $package_root" >&2
    exit 1
  fi
done

echo "AICC_AGENT_TOOLCHAIN_ROOT_OWNED"
