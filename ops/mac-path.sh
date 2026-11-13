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
FILES="/etc/aicc/executor-routing.env /etc/aicc/executors.env"
# the override exists only for the test suite; ignored on a host
[ "${AICC_OPS_ALLOW_NONROOT:-}" != 1 ] || FILES="${AICC_MAC_PATH_FILES:-$FILES}"
action="${1:-status}"
failed=0

say() { printf '%s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

if [ "${AICC_OPS_ALLOW_NONROOT:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  die "run as root (the provider web console is root already) or start the command with sudo"
fi

key_value() { # FILE -> 0 or 1 or empty (unset), or "odd" when any line naming the key is not exactly KEY=0/KEY=1
  local lines last
  lines="$({ grep -E "^[[:space:]]*(export[[:space:]]+)?$KEY" "$1" 2>/dev/null || true; })"
  [ -n "$lines" ] || return 0
  if printf '%s\n' "$lines" | grep -qvE "^$KEY=[01]$"; then
    printf 'odd'
    return 0
  fi
  last="$(printf '%s\n' "$lines" | tail -n 1)"
  printf '%s' "${last#*=}"
}

TMP_FILES=""
cleanup() { for t in $TMP_FILES; do rm -f "$t"; done; }
trap cleanup EXIT

rewrite_key() { # FILE VALUE: same file, only the key line differs
  local file="$1" value="$2" tmp line
  tmp="$(mktemp "$file.tmp.XXXXXX")"
  TMP_FILES="$TMP_FILES $tmp"   # holds a copy of the file: never left behind
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
        0) say "$file: $KEY is already 0, nothing to do" ;;
        '') say "$file: $KEY is not set, nothing to do" ;;
        *)
          # quotes, export, a trailing comment: systemd may still read it as 1
          say "WARN $file: a line naming $KEY is not exactly $KEY=0 or $KEY=1 (quotes, export, a comment?); NOT switched off, edit it by hand"
          failed=1
          ;;
      esac
    done
    # read every file again: the report is what is on disk now
    for file in $FILES; do
      [ -f "$file" ] || continue
      value="$(key_value "$file")"
      case "$value" in
        0|'') ;;
        *) say "WARN $file: $KEY is not 0 after the run"; failed=1 ;;
      esac
    done
    say "changed files: $changed"
    [ "$failed" -eq 0 ] || die "the Mac path is not switched off everywhere (see WARN lines)"
    say "control services read the file at their next run; running workers keep the old value until they restart at their own pace (this script never restarts a worker)"
    ;;
  on)
    restored=0
    for file in $FILES; do
      [ -f "$file" ] || continue
      backup="$(newest_backup "$file")"
      [ -n "$backup" ] || { say "$file: no saved copy, nothing to restore"; continue; }
      [ "$(key_value "$backup")" = 1 ] || { say "$file: the saved copy does not hold $KEY=1, left alone"; continue; }
      [ -n "$(key_value "$file")" ] || { say "$file: no $KEY line now, left alone"; continue; }
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
