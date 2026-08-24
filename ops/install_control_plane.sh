#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:-/opt/aicc}
readonly EXPECTED_SHA=${2:-}
readonly TASK_ID=${3:-}
readonly UNIT_SOURCE="$SOURCE_ROOT/deploy/systemd"
readonly DESIRED_STATE_SOURCE="$SOURCE_ROOT/deploy/config/aicc-desired-state.json"
readonly ACCEPTANCE_POLICY_SOURCE="$SOURCE_ROOT/deploy/config/aicc-acceptance-policy.json"
readonly UNIT_TARGET=/etc/systemd/system
readonly EVIDENCE_TARGET=/var/lib/aicc-control-plane

if [[ $(id -u) -ne 0 ]]; then
  echo "refusing: install_control_plane.sh must run as root" >&2
  exit 2
fi
if [[ "$SOURCE_ROOT" != "/opt/aicc-releases/$EXPECTED_SHA" \
      || $(/usr/bin/readlink -f /opt/aicc) != "$SOURCE_ROOT" ]]; then
  echo "refusing: source is not the active immutable release behind the canonical /opt/aicc checkout" >&2
  exit 2
fi
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "refusing: pass the exact 40-character merged SHA as argument 2" >&2
  exit 2
fi
if [[ $(/usr/bin/git -C "$SOURCE_ROOT" rev-parse HEAD) != "$EXPECTED_SHA" ]]; then
  echo "refusing: deployed checkout is not the expected merged SHA" >&2
  exit 2
fi
if [[ -n $(/usr/bin/git -C "$SOURCE_ROOT" status --porcelain) ]]; then
  echo "refusing: deployed checkout is not clean" >&2
  exit 2
fi
if [[ ! -x "$SOURCE_ROOT/.venv/bin/python" ]]; then
  echo "refusing: missing runtime $SOURCE_ROOT/.venv/bin/python" >&2
  exit 2
fi
[[ "$TASK_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$ ]] || {
  echo "refusing: canonical deployment task id required" >&2; exit 2;
}
for env_file in /etc/aicc/app.env /etc/aicc/migrator.env /etc/aicc/deployer.env; do
  [[ -r "$env_file" && ! -L "$env_file" ]] || {
    echo "refusing: missing trusted DB environment $env_file" >&2; exit 2;
  }
  [[ $(/usr/bin/stat -c '%U:%a' "$env_file") =~ ^root:(400|600)$ ]] || {
    echo "refusing: unsafe DB environment permissions: $env_file" >&2; exit 2;
  }
done

run_db_with_env() {
  local env_file=$1
  shift
  (
    set -a
    # shellcheck disable=SC1090
    # The exact root-owned, non-symlink path is checked above.
    source "$env_file"
    set +a
    "$SOURCE_ROOT/.venv/bin/python" -m command_center.db "$@"
  )
}

# Migration 0013 must exist before any reconciler query. It runs under the
# dedicated migrator, then all reads/actions switch back to aicc_app.
run_db_with_env /etc/aicc/migrator.env upgrade >/dev/null
run_db_with_env /etc/aicc/app.env status >/dev/null
[[ ! -L /etc/aicc/desired-state.json ]] || {
  echo "refusing: desired-state target is a symlink" >&2; exit 2;
}
/usr/bin/install -o root -g root -m 0444 \
  "$DESIRED_STATE_SOURCE" /etc/aicc/desired-state.json
/usr/bin/install -o root -g root -m 0444 \
  "$ACCEPTANCE_POLICY_SOURCE" /etc/aicc/acceptance-policy.json
[[ $(/usr/bin/stat -c '%U:%a' /etc/aicc/desired-state.json) == root:444 ]]
"$SOURCE_ROOT/.venv/bin/python" -m command_center.orchestrator.desired_state \
  /etc/aicc/desired-state.json control-units >/dev/null
"$SOURCE_ROOT/.venv/bin/python" -m command_center.orchestrator.acceptance_policy \
  /etc/aicc/acceptance-policy.json >/dev/null

UNITS=()
while IFS= read -r unit; do
  UNITS+=("$unit")
done < <(
  "$SOURCE_ROOT/.venv/bin/python" -m command_center.orchestrator.desired_state \
    /etc/aicc/desired-state.json control-units
)
readonly UNITS
(( ${#UNITS[@]} > 0 ))
unit_sources=()
for unit in "${UNITS[@]}"; do
  test -f "$UNIT_SOURCE/$unit"
  unit_sources+=("$UNIT_SOURCE/$unit")
done
/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/aicc_control_plane_watchdog.sh" \
  /usr/local/libexec/aicc-control-plane-watchdog
/usr/bin/systemd-analyze verify "${unit_sources[@]}" >/dev/null
for unit in "${UNITS[@]}"; do
  /usr/bin/install -o root -g root -m 0644 "$UNIT_SOURCE/$unit" "$UNIT_TARGET/$unit"
done

/bin/systemctl daemon-reload
# A fresh host has loaded-but-inactive units. Dry-run proves every exact unit is
# loadable/activatable without starting it; missing, failed or restart-stormed
# units remain red. Only then are timers enabled.
run_db_with_env /etc/aicc/app.env control-plane-reconcile \
  --repo-path "$SOURCE_ROOT" --dry-run >/dev/null
timer_units=()
for unit in "${UNITS[@]}"; do
  [[ "$unit" == *.timer ]] && timer_units+=("$unit")
done
(( ${#timer_units[@]} > 0 ))
/bin/systemctl enable --now "${timer_units[@]}"
/bin/systemctl start aicc-control-plane-reconciler.service
/bin/systemctl is-active --quiet "${timer_units[@]}"
run_db_with_env /etc/aicc/app.env control-plane-health \
  --max-age-seconds 180
run_db_with_env /etc/aicc/app.env control-plane-notification-health \
  --max-age-seconds 900

# Only the dedicated DB principal can create deployment attestation. The app
# role can neither forge this row nor turn arbitrary ci evidence into deploy.
run_db_with_env /etc/aicc/deployer.env control-plane-record-deployment \
  "$TASK_ID" "$EXPECTED_SHA" --environment preprod

# Durable deployment evidence is written only after the exact merged checkout,
# dry-run, installed units, live tick and readiness probe have all succeeded.
/usr/bin/install -d -o root -g root -m 0700 "$EVIDENCE_TARGET"
evidence_tmp=$(/usr/bin/mktemp "$EVIDENCE_TARGET/.deployed-sha.XXXXXX")
trap '/usr/bin/rm -f -- "$evidence_tmp"' EXIT
printf '%s\n' "$EXPECTED_SHA" >"$evidence_tmp"
/usr/bin/chown root:root "$evidence_tmp"
/usr/bin/chmod 0444 "$evidence_tmp"
/usr/bin/mv -f -- "$evidence_tmp" "$EVIDENCE_TARGET/deployed-sha"
trap - EXIT
[[ $(/usr/bin/stat -c '%U:%a' "$EVIDENCE_TARGET/deployed-sha") == root:444 ]]
