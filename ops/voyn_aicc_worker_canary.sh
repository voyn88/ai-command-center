#!/usr/bin/env bash
set -euo pipefail

readonly STATE_DIR=${STATE_DIRECTORY:-/var/lib/voyn-aicc-canary}
readonly STATE_FILE="$STATE_DIR/state"
readonly SAMPLES_FILE="$STATE_DIR/samples.jsonl"
readonly EVIDENCE_FILE="$STATE_DIR/evidence.json"
readonly HEALTH_PROBE=${HEALTH_PROBE:-/usr/local/sbin/voyn-worker-health}
readonly DEPLOYED_SHA_FILE=${VOYN_MONITOR_DEPLOYED_SHA_FILE:-/var/lib/voyn-worker-monitor/deployed-sha}
readonly SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-/bin/systemctl}
readonly PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
readonly DESIRED_STATE_READER=${DESIRED_STATE_READER:-/usr/local/libexec/aicc-desired-state}
readonly DESIRED_STATE_FILE=${AICC_DESIRED_STATE_FILE:-/etc/voyn/aicc-desired-state.json}
readonly SHA256SUM_BIN=${SHA256SUM_BIN:-/usr/bin/sha256sum}
readonly SAMPLE_SECONDS=${VOYN_CANARY_SAMPLE_SECONDS:-60}
readonly DURATION_SECONDS=${VOYN_CANARY_DURATION_SECONDS:-86400}
readonly MIN_SAMPLES=${VOYN_CANARY_MIN_SAMPLES:-1300}
WORKER_UNITS=()
while IFS= read -r unit; do
  WORKER_UNITS+=("$unit")
done < <("$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" worker-units)
readonly WORKER_UNITS
DESIRED_STATE_SHA=$(
  "$PYTHON_BIN" "$DESIRED_STATE_READER" "$DESIRED_STATE_FILE" sha256
)
readonly DESIRED_STATE_SHA
(( ${#WORKER_UNITS[@]} > 0 )) || { echo "worker registry is empty" >&2; exit 2; }

state_value() {
  local key=$1
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' "$STATE_FILE"
}

start() {
  [[ ! -e "$STATE_FILE" ]] || return 0
  [[ -f "$DEPLOYED_SHA_FILE" && ! -L "$DEPLOYED_SHA_FILE" ]] || {
    echo "missing monitor deployment evidence" >&2
    return 2
  }
  local deployed_sha
  deployed_sha=$(<"$DEPLOYED_SHA_FILE")
  [[ "$deployed_sha" =~ ^[0-9a-f]{40}$ ]] || {
    echo "invalid monitor deployment evidence" >&2
    return 2
  }
  /usr/bin/install -d -m 0700 "$STATE_DIR"
  local now
  now=$(date -u +%s)
  {
    printf 'started_epoch=%s\n' "$now"
    printf 'expected_end_epoch=%s\n' "$((now + DURATION_SECONDS))"
    printf 'boot_id=%s\n' "$(< /proc/sys/kernel/random/boot_id)"
    printf 'health_probe_sha256=%s\n' "$("$SHA256SUM_BIN" "$HEALTH_PROBE" | cut -d' ' -f1)"
    printf 'deployed_sha=%s\n' "$deployed_sha"
    printf 'desired_state_sha256=%s\n' "$DESIRED_STATE_SHA"
    printf 'failures=0\n'
    local unit restarts
    for unit in "${WORKER_UNITS[@]}"; do
      restarts=$("$SYSTEMCTL_BIN" show "$unit" --property=NRestarts --value)
      [[ "$restarts" =~ ^[0-9]+$ ]] || {
        echo "$unit invalid NRestarts baseline" >&2
        return 2
      }
      printf 'restart_baseline_%s=%s\n' "$unit" "$restarts"
    done
  } >"$STATE_FILE"
  : >"$SAMPLES_FILE"
}

sample() {
  local now output ok unit restarts baseline restart_increased=false
  now=$(date -u +%s)
  if output=$("$HEALTH_PROBE" 2>&1); then ok=true; else ok=false; fi
  local -a restart_values=()
  for unit in "${WORKER_UNITS[@]}"; do
    restarts=$("$SYSTEMCTL_BIN" show "$unit" --property=NRestarts --value)
    [[ "$restarts" =~ ^[0-9]+$ ]] || {
      ok=false
      restarts=0
    }
    baseline=$(state_value "restart_baseline_$unit")
    [[ "$baseline" =~ ^[0-9]+$ ]] || {
      echo "$unit missing restart baseline" >&2
      return 2
    }
    if (( restarts > baseline )); then
      ok=false
      restart_increased=true
    fi
    restart_values+=("$unit=$restarts")
  done
  python3 - "$SAMPLES_FILE" "$now" "$ok" "$output" "${restart_values[@]}" <<'PY'
import json
import pathlib
import sys

target, now, ok, output, *units = sys.argv[1:]
sample = {
    "at": int(now),
    "ok": ok == "true",
    "probe": output[-2000:],
    "restarts": {name: int(value) for name, value in (item.split("=", 1) for item in units)},
}
with pathlib.Path(target).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sample, sort_keys=True) + "\n")
PY
  if [[ "$ok" != true ]]; then
    local failures
    failures=$(state_value failures)
    sed -i "s/^failures=.*/failures=$((failures + 1))/" "$STATE_FILE"
  fi
  [[ "$restart_increased" == false ]] || echo "worker restart increase detected" >&2
}

finish() {
  local now expected count failures result
  now=$(date -u +%s)
  expected=$(state_value expected_end_epoch)
  (( now >= expected )) || return 0
  count=$(wc -l <"$SAMPLES_FILE")
  failures=$(state_value failures)
  result=PASS
  (( failures == 0 && count >= MIN_SAMPLES )) || result=FAIL
  python3 - "$STATE_FILE" "$SAMPLES_FILE" "$EVIDENCE_FILE" "$result" <<'PY'
import hashlib
import json
import pathlib
import sys

state_path, samples_path, evidence_path, result = sys.argv[1:]
state = dict(line.split("=", 1) for line in pathlib.Path(state_path).read_text().splitlines())
samples = [json.loads(line) for line in pathlib.Path(samples_path).read_text().splitlines()]
units = sorted({unit for sample in samples for unit in sample["restarts"]})
baselines = {
    key.removeprefix("restart_baseline_"): int(value)
    for key, value in state.items()
    if key.startswith("restart_baseline_")
}
max_restarts = {
    unit: max(sample["restarts"].get(unit, 0) for sample in samples)
    for unit in units
}
evidence = {
    "schema": "voyn.aicc-worker-canary/1",
    "result": result,
    "started_epoch": int(state["started_epoch"]),
    "expected_end_epoch": int(state["expected_end_epoch"]),
    "boot_id": state["boot_id"],
    "health_probe_sha256": state["health_probe_sha256"],
    "deployed_sha": state["deployed_sha"],
    "desired_state_sha256": state["desired_state_sha256"],
    "sample_count": len(samples),
    "failures": int(state["failures"]),
    "restart_baselines": baselines,
    "max_restarts": max_restarts,
    "restart_deltas": {
        unit: max_restarts[unit] - baselines.get(unit, 0) for unit in units
    },
}
body = json.dumps(evidence, sort_keys=True, indent=2) + "\n"
pathlib.Path(evidence_path).write_text(body)
pathlib.Path(evidence_path + ".sha256").write_text(hashlib.sha256(body.encode()).hexdigest() + "\n")
PY
  [[ "$result" == PASS ]]
}

run() {
  start
  while true; do
    sample
    if (( $(date -u +%s) >= $(state_value expected_end_epoch) )); then
      finish
      return
    fi
    sleep "$SAMPLE_SECONDS"
  done
}

case ${1:-} in
  start|sample|finish|run) "$1" ;;
  *) echo "usage: $0 {start|sample|finish|run}" >&2; exit 2 ;;
esac
