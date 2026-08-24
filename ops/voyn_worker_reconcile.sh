#!/usr/bin/env bash
set -euo pipefail

readonly SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-/bin/systemctl}
readonly HEALTH_PROBE=${HEALTH_PROBE:-/usr/local/sbin/voyn-worker-health}
readonly STATE_DIR=${STATE_DIRECTORY:-/var/lib/voyn-worker-reconciler}
readonly STATE_FILE="$STATE_DIR/circuit"
readonly CIRCUIT_SECONDS=${VOYN_WORKER_RECOVERY_CIRCUIT_SECONDS:-600}
readonly MAX_FAILURES=${VOYN_WORKER_RECOVERY_MAX_FAILURES:-3}
readonly SETTLE_SECONDS=${VOYN_WORKER_RECOVERY_SETTLE_SECONDS:-5}
readonly WORKER_UNITS=(voyn-aicc-worker@1.service voyn-aicc-worker@2.service)

[[ "$CIRCUIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$MAX_FAILURES" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$SETTLE_SECONDS" =~ ^[0-9]+$ ]] || exit 2
/usr/bin/install -d -o root -g root -m 0700 "$STATE_DIR"

failures=0
open_until=0
if [[ -f "$STATE_FILE" && ! -L "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
fi
[[ "$failures" =~ ^[0-9]+$ && "$open_until" =~ ^[0-9]+$ ]] || exit 2
now=$(date -u +%s)

write_state() {
  local new_failures=$1 new_open_until=$2 tmp
  tmp=$(/usr/bin/mktemp "$STATE_DIR/.circuit.XXXXXX")
  printf 'failures=%s\nopen_until=%s\n' "$new_failures" "$new_open_until" >"$tmp"
  /usr/bin/chown root:root "$tmp"
  /usr/bin/chmod 0600 "$tmp"
  /usr/bin/mv -f -- "$tmp" "$STATE_FILE"
}

if "$HEALTH_PROBE" >/dev/null 2>&1; then
  write_state 0 0
  exit 0
fi
if (( now < open_until )); then
  echo "worker recovery circuit open until $open_until" >&2
  exit 1
fi

# Exact compile-time allowlist only; no unit name is read from environment or
# task data. Restart both lanes together so readiness cannot certify a mixed
# generation after one lane fails.
for unit in "${WORKER_UNITS[@]}"; do
  "$SYSTEMCTL_BIN" restart "$unit"
done
(( SETTLE_SECONDS == 0 )) || sleep "$SETTLE_SECONDS"

if "$HEALTH_PROBE"; then
  write_state 0 0
  exit 0
fi
failures=$((failures + 1))
if (( failures >= MAX_FAILURES )); then
  open_until=$((now + CIRCUIT_SECONDS))
fi
write_state "$failures" "$open_until"
echo "worker recovery failed failures=$failures open_until=$open_until" >&2
exit 1
