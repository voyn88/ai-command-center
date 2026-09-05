#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.venv/bin/activate"
fi

if ! command -v streamlit >/dev/null 2>&1; then
  echo "Error: streamlit is not available on PATH." >&2
  echo "Create a virtual environment and run: pip install -r requirements.txt" >&2
  exit 1
fi

# Bind to localhost by default: the application has no authentication layer,
# so it must not be reachable from the local network unless the operator
# explicitly opts in. Pass an explicit --server.address to override locally;
# external deployment is not authorized without the identity-aware reverse
# proxy required by docs/adr/0011-streamlit-console-identity-boundary.md.
if [[ $# -eq 0 ]] || ! grep -q -- "--server.address" <<<"$*"; then
  set -- --server.address localhost "$@"
fi

exec streamlit run "$ROOT_DIR/app.py" "$@"
