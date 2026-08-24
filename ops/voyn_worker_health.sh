#!/usr/bin/env bash
set -euo pipefail

readonly SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-/bin/systemctl}
readonly UPTIME_FILE=${UPTIME_FILE:-/proc/uptime}
readonly MAX_WATCHDOG_AGE_SECONDS=${VOYN_WORKER_WATCHDOG_MAX_AGE_SECONDS:-180}
readonly WORKER_UNITS=(voyn-aicc-worker@1.service voyn-aicc-worker@2.service)

if [[ ! "$MAX_WATCHDOG_AGE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid VOYN_WORKER_WATCHDOG_MAX_AGE_SECONDS" >&2
  exit 2
fi
read -r uptime_seconds _ <"$UPTIME_FILE"
readonly now_monotonic_us=$(( ${uptime_seconds%.*} * 1000000 ))

for unit in "${WORKER_UNITS[@]}"; do
  load_state=""
  active_state=""
  sub_state=""
  service_type=""
  main_pid=""
  result=""
  watchdog_usec=""
  watchdog_timestamp=""
  restarts=""
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) load_state=$value ;;
      ActiveState) active_state=$value ;;
      SubState) sub_state=$value ;;
      Type) service_type=$value ;;
      MainPID) main_pid=$value ;;
      Result) result=$value ;;
      WatchdogUSec) watchdog_usec=$value ;;
      WatchdogTimestampMonotonic) watchdog_timestamp=$value ;;
      NRestarts) restarts=$value ;;
    esac
  done < <(
    "$SYSTEMCTL_BIN" show "$unit" \
      --property=LoadState --property=ActiveState --property=SubState \
      --property=Type --property=MainPID --property=Result \
      --property=WatchdogUSec --property=WatchdogTimestampMonotonic \
      --property=NRestarts
  )
  [[ "$load_state" == loaded ]] || { echo "$unit load=failed"; exit 1; }
  [[ "$active_state" == active && "$sub_state" == running ]] || {
    echo "$unit state=${active_state:-unknown}/${sub_state:-unknown}"
    exit 1
  }
  [[ "$service_type" == notify && "$main_pid" =~ ^[1-9][0-9]*$ ]] || {
    echo "$unit readiness=unproven"
    exit 1
  }
  [[ -n "$watchdog_usec" && "$watchdog_usec" != 0 && "$watchdog_usec" != 0s \
     && "$watchdog_timestamp" =~ ^[1-9][0-9]*$ ]] || {
    echo "$unit watchdog=missing"
    exit 1
  }
  age_us=$((now_monotonic_us - watchdog_timestamp))
  (( age_us >= 0 && age_us <= MAX_WATCHDOG_AGE_SECONDS * 1000000 )) || {
    echo "$unit watchdog_age_us=$age_us stale"
    exit 1
  }
  printf '%s ready=1 pid=%s watchdog_age_ms=%s restarts=%s result=%s\n' \
    "$unit" "$main_pid" "$((age_us / 1000))" \
    "${restarts:-0}" "${result:-unknown}"
done
