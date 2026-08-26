#!/usr/bin/env bash
# Local preflight: mirrors the fast part of the CI "Quality gates" job
# (whitespace -> ruff -> byte-compile) so obvious failures are caught before
# pushing. The full pytest run is intentionally NOT part of preflight.
#
# Usage: ./scripts/preflight.sh
# Requires: the repo venv (`.venv`) with dev requirements installed.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "error: .venv/bin/python not found (create the venv and install requirements-dev.txt first)" >&2
    exit 1
fi

echo "== 1/3 whitespace (git diff --check) =="
git diff --check
git diff --cached --check

echo "== 2/3 ruff check =="
if [ -x ".venv/bin/ruff" ]; then
    .venv/bin/ruff check .
else
    "$PY" -m ruff check .
fi

echo "== 3/3 byte-compile (python -m compileall) =="
"$PY" -m compileall -q command_center scripts tests app.py

echo "preflight OK"
