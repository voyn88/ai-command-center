#!/usr/bin/env bash
# Local preflight: mirrors the fast part of the CI "Quality gates" job
# (whitespace -> ruff -> byte-compile) plus the public-repo leak guard, so
# obvious failures are caught before pushing. The full pytest run is
# intentionally NOT part of preflight.
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

echo "== 1/4 whitespace (git diff --check) =="
git diff --check
git diff --cached --check

echo "== 2/4 ruff check =="
if [ -x ".venv/bin/ruff" ]; then
    .venv/bin/ruff check .
else
    "$PY" -m ruff check .
fi

echo "== 3/4 byte-compile (python -m compileall) =="
"$PY" -m compileall -q command_center scripts tests app.py

echo "== 4/4 public-repo leak guard =="
./scripts/ci/prepush/leak_guard.sh

echo "preflight OK"
