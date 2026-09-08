#!/usr/bin/env bash
# Install this repository's git hooks (currently: the pre-commit audit gate,
# deploy/git-hooks/pre-commit -> scripts/precommit_findings_gate.py).
# Idempotent -- safe to re-run after a hook template changes; `git rev-parse
# --git-path hooks` resolves the real hooks directory even from a worktree.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

HOOKS_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS_DIR"
install -m 0755 deploy/git-hooks/pre-commit "$HOOKS_DIR/pre-commit"
echo "installed pre-commit hook -> $HOOKS_DIR/pre-commit"
