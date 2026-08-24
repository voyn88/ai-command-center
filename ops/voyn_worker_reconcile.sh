#!/usr/bin/env bash
set -euo pipefail

readonly SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-/bin/systemctl}
readonly HEALTH_PROBE=${HEALTH_PROBE:-/usr/local/sbin/voyn-worker-health}
readonly PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
readonly DESIRED_STATE_READER=${DESIRED_STATE_READER:-/usr/local/libexec/aicc-desired-state}
readonly DESIRED_STATE_FILE=${AICC_DESIRED_STATE_FILE:-/etc/voyn/aicc-desired-state.json}
readonly CHOWN_BIN=${CHOWN_BIN:-/usr/bin/chown}
readonly CHMOD_BIN=${CHMOD_BIN:-/bin/chmod}
readonly MV_BIN=${MV_BIN:-/bin/mv}
readonly SYNC_BIN=${SYNC_BIN:-/bin/sync}
readonly STATE_DIR=${STATE_DIRECTORY:-/var/lib/voyn-worker-reconciler}
readonly STATE_FILE="$STATE_DIR/circuit"
CIRCUIT_SECONDS=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-circuit-open-seconds
)
readonly CIRCUIT_SECONDS
MAX_FAILURES=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-circuit-failure-threshold
)
readonly MAX_FAILURES
POLL_SECONDS=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-recovery-poll-seconds
)
readonly POLL_SECONDS
DRAIN_TIMEOUT_SECONDS=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-recovery-timeout-seconds
)
readonly DRAIN_TIMEOUT_SECONDS
MIN_STOP_TIMEOUT_SECONDS=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-minimum-stop-seconds
)
readonly MIN_STOP_TIMEOUT_SECONDS
MINIMUM_READY_LANES=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-minimum-ready
)
readonly MINIMUM_READY_LANES
WORKER_UNITS=()
while IFS= read -r unit; do
  WORKER_UNITS+=("$unit")
done < <("$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-units)
readonly WORKER_UNITS

for value in "$CIRCUIT_SECONDS" "$MAX_FAILURES" "$DRAIN_TIMEOUT_SECONDS" \
  "$MIN_STOP_TIMEOUT_SECONDS" "$MINIMUM_READY_LANES" "$POLL_SECONDS"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || exit 2
done
(( DRAIN_TIMEOUT_SECONDS >= MIN_STOP_TIMEOUT_SECONDS )) || exit 2
(( ${#WORKER_UNITS[@]} > MINIMUM_READY_LANES )) || exit 2
/usr/bin/install -d -m 0700 "$STATE_DIR"

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
  "$CHOWN_BIN" 0:0 "$tmp"
  "$CHMOD_BIN" 0600 "$tmp"
  "$SYNC_BIN" -f "$tmp"
  "$MV_BIN" -f -- "$tmp" "$STATE_FILE"
  "$SYNC_BIN" -f "$STATE_DIR"
}

fail_recovery() {
  local reason=$1
  failures=$((failures + 1))
  if (( failures >= MAX_FAILURES )); then
    open_until=$((now + CIRCUIT_SECONDS))
  fi
  write_state "$failures" "$open_until"
  echo "$reason failures=$failures open_until=$open_until" >&2
  exit 1
}

unit_healthy() {
  "$HEALTH_PROBE" "$1" >/dev/null 2>&1
}

timeout_seconds() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys

raw = sys.argv[1].strip()
if raw == "infinity":
    print(10**12)
    raise SystemExit
if raw.isdigit():
    print(int(raw) // 1_000_000)
    raise SystemExit
raise SystemExit(2)
PY
}

unhealthy=()
for unit in "${WORKER_UNITS[@]}"; do
  unit_healthy "$unit" || unhealthy+=("$unit")
done
if (( ${#unhealthy[@]} == 0 )); then
  write_state 0 0
  exit 0
fi
if (( now < open_until )); then
  echo "worker recovery circuit open until $open_until" >&2
  exit 1
fi
ready_count=$((${#WORKER_UNITS[@]} - ${#unhealthy[@]}))
if (( ready_count < MINIMUM_READY_LANES )); then
  fail_recovery "refusing recovery: ready lane quorum would be violated"
fi

target=${unhealthy[0]}
ready_witnesses=()
for unit in "${WORKER_UNITS[@]}"; do
  [[ "$unit" == "$target" ]] && continue
  unit_healthy "$unit" && ready_witnesses+=("$unit")
done
(( ${#ready_witnesses[@]} >= MINIMUM_READY_LANES )) \
  || fail_recovery "ready lane quorum changed before drain"

if ! stop_value=$($SYSTEMCTL_BIN show "$target" --property=TimeoutStopUSec --value); then
  fail_recovery "$target TimeoutStopUSec probe failed"
fi
stop_seconds=$(timeout_seconds "$stop_value") || {
  fail_recovery "$target has unparseable TimeoutStopUSec=$stop_value"
}
(( stop_seconds >= MIN_STOP_TIMEOUT_SECONDS )) || {
  fail_recovery "$target unsafe stop timeout ${stop_seconds}s < ${MIN_STOP_TIMEOUT_SECONDS}s"
}

if ! active=$($SYSTEMCTL_BIN show "$target" --property=ActiveState --value); then
  fail_recovery "$target ActiveState probe failed"
fi
if [[ "$active" == active || "$active" == activating ]]; then
  # SIGTERM is the worker daemon's versioned claim-gate drain. It stops new
  # claims, lets the current bounded attempt finish, and Restart=always brings
  # back this lane. No child is killed and the declared ready quorum remains.
  "$SYSTEMCTL_BIN" kill --kill-who=main --signal=TERM "$target" \
    || fail_recovery "$target drain signal failed"
else
  "$SYSTEMCTL_BIN" reset-failed "$target" || true
  "$SYSTEMCTL_BIN" start "$target" || fail_recovery "$target start failed"
fi

deadline=$((now + DRAIN_TIMEOUT_SECONDS))
while (( $(date -u +%s) < deadline )); do
  ready_count=0
  for unit in "${ready_witnesses[@]}"; do
    unit_healthy "$unit" && ready_count=$((ready_count + 1))
  done
  (( ready_count >= MINIMUM_READY_LANES )) \
    || fail_recovery "ready lane quorum lost during rolling recovery"
  if unit_healthy "$target"; then
    write_state 0 0
    exit 0
  fi
  sleep "$POLL_SECONDS"
done

fail_recovery "worker rolling recovery timed out"
