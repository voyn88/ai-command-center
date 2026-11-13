#!/usr/bin/env bash
# Switch the owner-Mac review path off or on (and report its state).
# Typed in the provider web console as root, lowercase only:
#   bash /opt/aicc/current/ops/mac-path.sh status
#   bash /opt/aicc/current/ops/mac-path.sh off
#   bash /opt/aicc/current/ops/mac-path.sh on        (rollback: the one line goes back to 1)
#
# What it changes: ONE line, AICC_CLAUDE_MAC_ENABLED=1 -> =0, in the env files
# that already carry it (control: executor-routing.env, worker: executors.env).
# It never prints a file's content (only the value of that one key), never
# creates the key, never touches agent-claude-mac.env or any other file, and
# never restarts a worker.  Every changed file is first copied byte for byte to
# <file>.before-mac-off-<time>; `on` sets the line back to 1 in the files that have such a copy.
set -euo pipefail

KEY=AICC_CLAUDE_MAC_ENABLED
FILES="${AICC_MAC_PATH_FILES:-/etc/aicc/executor-routing.env /etc/aicc/executors.env}"
action="${1:-status}"

say() { printf '%s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

if [ "${AICC_OPS_ALLOW_NONROOT:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  die "run as root (the provider web console is root already) or start the command with sudo"
fi

key_value() { # FILE -> value of the key (last occurrence), empty when absent
  { grep -E "^$KEY=" "$1" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}

rewrite_key() { # FILE VALUE: same file, only the key line differs
  local file="$1" value="$2" tmp line
  tmp="$(mktemp "$file.tmp.XXXXXX")"
  cp -p "$file" "$tmp"           # mode and owner of the original
  : >"$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in "$KEY="*) line="$KEY=$value" ;; esac
    printf '%s\n' "$line" >>"$tmp"
  done <"$file"
  mv "$tmp" "$file"
}

newest_backup() { # FILE
  # shellcheck disable=SC2012
  ls -1 "$1".before-mac-off-* 2>/dev/null | sort | tail -n 1 || true
}

report() {
  local file value
  for file in $FILES; do
    [ -f "$file" ] || { say "$file: not on this host"; continue; }
    value="$(key_value "$file")"
    say "$file: $KEY=${value:-unset}"
  done
}

case "$action" in
  status)
    report
    ;;
  off)
    changed=0
    for file in $FILES; do
      [ -f "$file" ] || continue
      value="$(key_value "$file")"
      case "$value" in
        1)
          backup="$file.before-mac-off-$(date +%Y%m%d%H%M%S)"
          cp -p "$file" "$backup"
          rewrite_key "$file" 0
          say "$file: $KEY 1 -> 0 (copy saved as $backup)"
          changed=$((changed + 1))
          ;;
        '') say "$file: $KEY is not set, nothing to do" ;;
        *) say "$file: $KEY is already $value, nothing to do" ;;
      esac
    done
    say "changed files: $changed"
    say "control services read the file at their next run; running workers keep the old value until they restart at their own pace (this script never restarts a worker)"
    ;;
  on)
    restored=0
    for file in $FILES; do
      [ -f "$file" ] || continue
      backup="$(newest_backup "$file")"
      [ -n "$backup" ] || { say "$file: no saved copy, nothing to restore"; continue; }
      # only the one line goes back; anything else edited since stays as it is
      rewrite_key "$file" 1
      say "$file: $KEY 0 -> 1 (the copy $backup is the byte-exact original)"
      restored=$((restored + 1))
    done
    say "files set back: $restored"
    ;;
  *)
    die "use: status, off or on"
    ;;
esac
