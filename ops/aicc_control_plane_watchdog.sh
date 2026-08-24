#!/usr/bin/env bash
set -euo pipefail

readonly AICC_ROOT=/opt/aicc
readonly PYTHON="$AICC_ROOT/.venv/bin/python"

if "$PYTHON" -m command_center.db control-plane-health --max-age-seconds 180; then
  exit 0
fi

# This monitor is a distinct root-owned timer.  It repairs the reconciler's
# wake-up path once, then proves the repair produced a fresh healthy heartbeat.
/bin/systemctl start aicc-control-plane-reconciler.timer
/bin/systemctl start aicc-control-plane-reconciler.service
"$PYTHON" -m command_center.db control-plane-health --max-age-seconds 180
