#!/usr/bin/env bash
# Allowlisted quality-band executor for the privileged principal-isolation
# launcher (VOYN-W0-AICC-SANDBOX-PREPUSH-TESTS).
#
# Installed root-owned at /usr/libexec/aicc-quality-band. The broker bind-mounts
# the candidate worktree at /workspace and launches this file with a fixed
# argv — never a path from the candidate tree (verification finding on the
# REM round: a candidate-controlled scripts/ci/prepush/quality_band.sh could
# otherwise emit the broker's infra-failure envelope and force a deferral).
#
# Operator bypass (VOYN_QUALITY_BAND=off) is enforced by publish_run BEFORE
# this unit is started; it is deliberately not honoured here so a candidate
# cannot inject the bypass into the sandboxed environment.
set -uo pipefail

cd /workspace || {
    echo "QUALITY_BAND: fail (workspace mount missing)" >&2
    exit 1
}

say() { echo "QUALITY_BAND: $*"; }

# Map any accidental 125 from a child into an ordinary failure. Exit 125 is
# reserved for the broker's LaunchRefused transport envelope; a candidate
# .venv interpreter or test that exits 125 must not be indistinguishable
# from infrastructure failure (adversarial review of the REM round).
finish() {
    local rc=$1
    if [ "$rc" -eq 125 ]; then
        say "fail (reserved exit 125 remapped)"
        exit 1
    fi
    exit "$rc"
}

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    say "skipped (no .venv; CI remains authoritative)"
    finish 0
fi

if ! ./scripts/preflight.sh; then
    say "fail phase=preflight"
    finish 1
fi

BASE="${VOYN_QUALITY_BAND_BASE:-origin/main}"
sel="$(mktemp)"
trap 'rm -f "$sel"' EXIT
if ! "$PY" scripts/ci/test_impact/select_tests.py \
        --base "$BASE" --format pytest --output "$sel" >/dev/null; then
    say "fail phase=select"
    finish 1
fi

if [ ! -s "$sel" ]; then
    say "pass (no impacted tests)"
    finish 0
fi
if [ "$(cat "$sel")" = "tests" ]; then
    say "deferred-to-CI (mode=all)"
    finish 0
fi

targets=()
while IFS= read -r line; do
    [ -n "$line" ] && targets+=("$line")
done < "$sel"

run_phase() {
    "$PY" -m pytest "$@"
    local rc=$?
    if [ "$rc" -eq 5 ]; then
        return 0
    fi
    if [ "$rc" -eq 125 ]; then
        return 1
    fi
    return "$rc"
}

if ! run_phase -q -m "not serial" -n auto --dist loadscope "${targets[@]}"; then
    say "fail phase=tests"
    finish 1
fi
if ! run_phase -q -m serial -p no:xdist "${targets[@]}"; then
    say "fail phase=serial-tests"
    finish 1
fi

say "pass (${#targets[@]} impacted test file(s))"
finish 0
