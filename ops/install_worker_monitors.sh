#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:-/home/voynadmin/aicc-preprod/repo}
readonly EXPECTED_SHA=${2:-}
readonly UNIT_TARGET=/etc/systemd/system
readonly CONFIG_TARGET=/etc/voyn
readonly EVIDENCE_TARGET=/var/lib/voyn-worker-monitor
readonly UNITS=(
  voyn-worker-health.service
  voyn-worker-health.timer
  voyn-worker-reconciler.service
  voyn-worker-reconciler.timer
  voyn-findings-sync.service
  voyn-findings-sync.timer
  voyn-canary.service
)

if [[ $(id -u) -ne 0 ]]; then
  echo "refusing: install_worker_monitors.sh must run as root" >&2
  exit 2
fi
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "refusing: pass the exact 40-character merged SHA as argument 2" >&2
  exit 2
fi
if [[ $(/usr/bin/git -C "$SOURCE_ROOT" rev-parse HEAD) != "$EXPECTED_SHA" ]]; then
  echo "refusing: worker checkout is not the expected merged SHA" >&2
  exit 2
fi
if [[ -n $(/usr/bin/git -C "$SOURCE_ROOT" status --porcelain) ]]; then
  echo "refusing: worker checkout is not clean" >&2
  exit 2
fi

/usr/bin/bash -n \
  "$SOURCE_ROOT/ops/voyn_worker_health.sh" \
  "$SOURCE_ROOT/ops/voyn_worker_reconcile.sh" \
  "$SOURCE_ROOT/ops/voyn_sync_findings.sh" \
  "$SOURCE_ROOT/ops/voyn_aicc_worker_canary.sh"
/usr/bin/install -d -o root -g root -m 0755 \
  /usr/local/sbin /usr/local/libexec /opt/voyn-worker/bin "$CONFIG_TARGET"
/usr/bin/install -o root -g root -m 0555 \
  "$SOURCE_ROOT/command_center/orchestrator/desired_state.py" \
  /usr/local/libexec/aicc-desired-state
test ! -L "$CONFIG_TARGET/aicc-desired-state.json"
/usr/bin/install -o root -g root -m 0444 \
  "$SOURCE_ROOT/deploy/config/aicc-desired-state.json" \
  "$CONFIG_TARGET/aicc-desired-state.json"
test ! -L "$CONFIG_TARGET/aicc-desired-state.json"
[[ $(/usr/bin/stat -c '%U:%a' "$CONFIG_TARGET/aicc-desired-state.json") == root:444 ]]
/usr/bin/python3 /usr/local/libexec/aicc-desired-state \
  "$CONFIG_TARGET/aicc-desired-state.json" worker-units >/dev/null
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/voyn_worker_health.sh" /usr/local/sbin/voyn-worker-health
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/voyn_worker_reconcile.sh" /usr/local/sbin/voyn-worker-reconcile
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/voyn_aicc_worker_canary.sh" /usr/local/sbin/voyn-aicc-worker-canary
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/voyn_sync_findings.sh" /opt/voyn-worker/bin/voyn-sync-findings

/usr/bin/install -o root -g root -m 0644 \
  "$SOURCE_ROOT/deploy/config/voyn-findings-known-hosts" \
  "$CONFIG_TARGET/findings-known-hosts"
test ! -L "$CONFIG_TARGET/findings-sync.env"
/usr/bin/install -o root -g root -m 0644 \
  "$SOURCE_ROOT/deploy/config/voyn-findings-sync.env" \
  "$CONFIG_TARGET/findings-sync.env"
unit_sources=()
for unit in "${UNITS[@]}"; do
  unit_sources+=("$SOURCE_ROOT/deploy/systemd/$unit")
done
/usr/bin/systemd-analyze verify "${unit_sources[@]}" >/dev/null
for unit in "${UNITS[@]}"; do
  /usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_ROOT/deploy/systemd/$unit" "$UNIT_TARGET/$unit"
done

/usr/bin/install -d -o voynadmin -g voynadmin -m 0750 \
  /var/spool/voyn-worker/backlog-outbox /var/spool/voyn-worker/backlog-sent
[[ $(/usr/bin/stat -c '%U:%a' "$CONFIG_TARGET/findings-sync.env") == root:644 ]]
test ! -L "$CONFIG_TARGET/findings-sync.env"

set -a
# shellcheck disable=SC1091
source "$CONFIG_TARGET/findings-sync.env"
set +a
test -f "$VOYN_FINDINGS_IDENTITY"
test ! -L "$VOYN_FINDINGS_IDENTITY"
test -f "$VOYN_FINDINGS_KNOWN_HOSTS"
test ! -L "$VOYN_FINDINGS_KNOWN_HOSTS"
[[ $(/usr/bin/stat -c '%U:%a' "$VOYN_FINDINGS_KNOWN_HOSTS") == root:644 ]]
/usr/bin/ssh-keygen -F "$VOYN_FINDINGS_ENDPOINT" \
  -f "$VOYN_FINDINGS_KNOWN_HOSTS" >/dev/null

/bin/systemctl daemon-reload
worker_units=()
while IFS= read -r unit; do
  worker_units+=("$unit")
done < <(
  /usr/bin/python3 /usr/local/libexec/aicc-desired-state \
    "$CONFIG_TARGET/aicc-desired-state.json" worker-units
)
(( ${#worker_units[@]} > 0 ))
minimum_stop_seconds=$(
  /usr/bin/python3 /usr/local/libexec/aicc-desired-state \
    "$CONFIG_TARGET/aicc-desired-state.json" worker-minimum-stop-seconds
)
for unit in "${worker_units[@]}"; do
  rendered=$(/bin/systemctl cat "$unit")
  grep -qx 'Type=notify-reload' <<<"$rendered"
  # shellcheck disable=SC2016
  grep -qx 'ExecReload=/bin/kill -HUP $MAINPID' <<<"$rendered"
  timeout_stop_us=$(/bin/systemctl show "$unit" --property=TimeoutStopUSec --value)
  [[ "$timeout_stop_us" =~ ^[0-9]+$ ]]
  (( timeout_stop_us >= minimum_stop_seconds * 1000000 ))
done
/bin/systemctl enable --now \
  voyn-worker-health.timer voyn-worker-reconciler.timer voyn-findings-sync.timer
/bin/systemctl start voyn-worker-health.service
/usr/sbin/runuser -u voynadmin -- /opt/voyn-worker/bin/voyn-sync-findings check
/usr/local/sbin/voyn-worker-health
/bin/systemctl is-active --quiet \
  voyn-worker-health.timer voyn-worker-reconciler.timer voyn-findings-sync.timer

# Commit deployment evidence only after the reloaded units and both live
# readiness paths pass. A failed reload/fleet/SSH probe leaves the previous
# exact SHA intact and can never start a canary against unproven code.
/usr/bin/install -d -o root -g root -m 0700 "$EVIDENCE_TARGET"
evidence_tmp=$(/usr/bin/mktemp "$EVIDENCE_TARGET/.deployed-sha.XXXXXX")
trap '/usr/bin/rm -f -- "$evidence_tmp"' EXIT
printf '%s\n' "$EXPECTED_SHA" >"$evidence_tmp"
/usr/bin/chown root:root "$evidence_tmp"
/usr/bin/chmod 0444 "$evidence_tmp"
/usr/bin/mv -f -- "$evidence_tmp" "$EVIDENCE_TARGET/deployed-sha"
trap - EXIT
[[ $(<"$EVIDENCE_TARGET/deployed-sha") == "$EXPECTED_SHA" ]]
[[ $(/usr/bin/stat -c '%U:%a' "$EVIDENCE_TARGET/deployed-sha") == root:444 ]]

/bin/systemctl enable voyn-canary.service
/bin/systemctl restart voyn-canary.service
/bin/systemctl is-active --quiet voyn-canary.service
