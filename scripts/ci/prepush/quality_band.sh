#!/usr/bin/env bash
# Pre-push quality band (VOYN-W0-AICC-PREPUSH-FAST-GATE).
#
# Red PR CI runs are dominated by pytest shard failures (measured 2026-08-26:
# 14 of 50 recent PR CI runs red, 28 shard-job failures across the 12 red runs
# sampled). Each red run costs a full agent round-trip: diagnose -> fix ->
# new SHA -> new CI -> new review. This band runs the cheap part of that
# feedback loop locally, before the push spends the round-trip.
#
# Phases:
#   1. scripts/preflight.sh          (whitespace -> ruff -> byte-compile)
#   2. impacted tests, selected by scripts/ci/test_impact/select_tests.py,
#      run in the same two phases as the CI advisory job: xdist for
#      `-m "not serial"`, then the serial tail without xdist. pytest exit 5
#      (nothing collected) is neutral, mirroring the advisory job.
#
# The band is an economy device, not a gate: the required CI suite is
# unchanged and remains authoritative. mode=all (trigger-all changes) and a
# missing .venv therefore defer to CI instead of blocking the push - the
# band must never be able to reduce coverage, only to fail sooner.
#
# TRUST BOUNDARY (v2): this script is for contexts that already execute the
# tree's own code -- an interactive writer (`make prepush`) or the agent's
# sandboxed run. `publish_run` deliberately does NOT execute it (it is
# candidate content in that credentialed context -- verification finding on
# 254154a); the publish side runs only the non-executing ruff gate from the
# worker's trusted interpreter (`_static_quality_gate` in
# command_center/orchestrator/publish.py).
#
# VOYN_QUALITY_BAND=off bypasses the band; the bypass is printed, never
# silent. VOYN_QUALITY_BAND_BASE overrides the selection base.
set -uo pipefail
cd "$(dirname "$0")/../../.."

say() { echo "QUALITY_BAND: $*"; }

if [ "${VOYN_QUALITY_BAND:-on}" = "off" ]; then
    say "bypassed (VOYN_QUALITY_BAND=off)"
    exit 0
fi

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    say "skipped (no .venv; CI remains authoritative)"
    exit 0
fi

if ! ./scripts/preflight.sh; then
    say "fail phase=preflight"
    exit 1
fi

BASE="${VOYN_QUALITY_BAND_BASE:-origin/main}"
sel="$(mktemp)"
trap 'rm -f "$sel"' EXIT
if ! "$PY" scripts/ci/test_impact/select_tests.py \
        --base "$BASE" --format pytest --output "$sel" >/dev/null; then
    say "fail phase=select"
    exit 1
fi

if [ ! -s "$sel" ]; then
    say "pass (no impacted tests)"
    exit 0
fi
if [ "$(cat "$sel")" = "tests" ]; then
    # Trigger-all change (lockfiles, conftest, CI config, ...). Running the
    # whole suite here would blow the band's latency budget for no coverage
    # gain - the required gate runs it anyway.
    say "deferred-to-CI (mode=all)"
    exit 0
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
    return "$rc"
}

if ! run_phase -q -m "not serial" -n auto --dist loadscope "${targets[@]}"; then
    say "fail phase=tests"
    exit 1
fi
if ! run_phase -q -m serial -p no:xdist "${targets[@]}"; then
    say "fail phase=serial-tests"
    exit 1
fi

say "pass (${#targets[@]} impacted test file(s))"
