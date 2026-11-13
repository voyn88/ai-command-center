#!/usr/bin/env bash
# One-off owner report: what would the disk guard remove if the disk were
# fuller?  Run in the provider web console as root (or with sudo), typed with
# lowercase letters only:
#   bash /opt/aicc/current/ops/disk-guard-probe.sh
#   bash /opt/aicc/current/ops/disk-guard-probe.sh 45 60
# Arguments: low-threshold and target free-space percent (default 45 60).
#
# It runs `disk_guard --probe` as a transient systemd unit that carries the SAME
# sandbox properties as the real timer unit (they are read from the shipped
# unit file, so there is no second copy to drift).  `--probe` is a dry run by
# construction: the code forces it after reading settings and environment, it
# cannot be combined with --live, and it writes no journal file.  This script
# changes no setting, no timer and no unit; the output is the report.
set -euo pipefail

RELEASE_DIR="${AICC_RELEASE_DIR:-/opt/aicc/current}"
UNIT_FILE="$RELEASE_DIR/deploy/systemd/voyn-ops-disk-guard.service"
VENV_PY="${AICC_VENV_PYTHON:-$RELEASE_DIR/.venv/bin/python}"
SYSTEMD_RUN="${AICC_SYSTEMD_RUN:-systemd-run}"
low="${1:-45}"
target="${2:-60}"

die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

case "$low$target" in ''|*[!0-9]*) die "thresholds must be whole numbers, for example: 45 60" ;; esac
[ "$low" -ge 1 ] && [ "$low" -le 100 ] && [ "$target" -ge "$low" ] && [ "$target" -le 100 ] \
  || die "need 1 <= low <= target <= 100"
[ -f "$UNIT_FILE" ] || die "no $UNIT_FILE: this host has not deployed the release with the disk guard"
[ -x "$VENV_PY" ] || die "no interpreter at $VENV_PY"
if [ "${AICC_OPS_ALLOW_NONROOT:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  die "run as root (the provider web console is root already) or start the command with sudo"
fi

props=()
in_service=0
while IFS= read -r line; do
  case "$line" in
    "[Service]") in_service=1; continue ;;
    "["*) in_service=0; continue ;;
  esac
  [ "$in_service" -eq 1 ] || continue
  case "$line" in
    ''|'#'*) continue ;;
    Type=*|User=*|ExecStart=*|TimeoutStartSec=*) continue ;;
    *=*) props+=(-p "$line") ;;
  esac
done <"$UNIT_FILE"

exec "$SYSTEMD_RUN" --unit=voyn-ops-disk-guard-probe --wait --pipe --collect --quiet \
  -p User=root ${props[@]+"${props[@]}"} \
  -- "$VENV_PY" -m command_center.ops.disk_guard --probe \
  --low-free-pct "$low" --target-free-pct "$target"
