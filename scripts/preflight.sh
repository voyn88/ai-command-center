#!/usr/bin/env bash
# Local preflight: every cheap CI gate, run before a PR is published.
#
# The point is arithmetic. A PR that fails one of these costs ~22 runner
# minutes and 4-5 minutes of waiting to learn it; the same failure surfaces
# here in well under two minutes, on a machine that is already warm. So this
# mirrors the gates that are cheap to reproduce -- whitespace, ruff,
# byte-compile, the public-repo leak guard, the secret baseline, and the tests
# the change can actually affect -- and stops before the ones that are not
# (the full matrix, Windows, the AIOS SDK integration suite, security
# provenance). Those stay in CI, where they belong.
#
# Impact-selected tests are the one addition that needs care: the selector
# (scripts/ci/test_impact/select_tests.py) is advisory in CI precisely because
# selecting too few is possible, and the full suite is what catches that. The
# same holds here. Passing preflight means "no cheap gate is broken", never
# "this is proven"; the required gates still decide.
#
# Usage: ./scripts/preflight.sh            # impact-selected tests
#        AICC_PREFLIGHT_TESTS=none ./scripts/preflight.sh    # skip tests
#        AICC_PREFLIGHT_BASE=origin/main ./scripts/preflight.sh
# Requires: the repo venv (`.venv`) with dev requirements installed.
set -euo pipefail
cd "$(dirname "$0")/.."

# Prefer the repo venv when it carries the dev tools. A worktree created for
# one branch usually does not -- `uv sync` installs the dependency groups from
# pyproject, and ruff/detect-secrets live in requirements-dev.txt -- and a
# preflight that only runs in one checkout is a preflight nobody runs. So fall
# back to uv with the requirement files, which is how the same commands are
# invoked everywhere else in this repo.
PY=".venv/bin/python"
RUN=""
if [ -x "$PY" ] && "$PY" -c "import ruff" >/dev/null 2>&1; then
    RUN=""
elif command -v uv >/dev/null 2>&1; then
    RUN="uv run --quiet --with-requirements requirements-dev.txt --with-requirements requirements-web.txt"
    PY="python"
    echo "note: using uv with requirements-dev.txt (the venv lacks the dev tools)"
elif [ -x "$PY" ]; then
    echo "note: venv lacks ruff; install requirements-dev.txt for the full preflight" >&2
else
    echo "error: neither .venv/bin/python nor uv is available" >&2
    exit 1
fi
run_py() { if [ -n "$RUN" ]; then $RUN python "$@"; else "$PY" "$@"; fi; }

echo "== 1/6 whitespace (git diff --check) =="
git diff --check
git diff --cached --check

echo "== 2/6 ruff check =="
if [ -x ".venv/bin/ruff" ]; then
    .venv/bin/ruff check .
else
    run_py -m ruff check .
fi

echo "== 3/6 byte-compile (python -m compileall) =="
run_py -m compileall -q command_center scripts tests app.py

echo "== 4/6 public-repo leak guard =="
./scripts/ci/prepush/leak_guard.sh

echo "== 5/6 secret baseline unchanged =="
# Exactly the CI comparison: the *inventory* of findings, not the file, since
# `detect-secrets scan` rewrites `generated_at` on every run. A new finding
# here is either a real secret or fixture data that should not be in the
# baseline at all -- both are worth knowing before 22 runner-minutes.
# Pinned to the version CI uses: detect-secrets' plugin set changes between
# minors, so a different version would disagree about what is a finding.
DS_VERSION="1.5.0"
if [ -x ".venv/bin/detect-secrets" ]; then
    DS=".venv/bin/detect-secrets"
elif command -v detect-secrets >/dev/null 2>&1; then
    DS="detect-secrets"
elif command -v uvx >/dev/null 2>&1; then
    DS="uvx --quiet --from detect-secrets==${DS_VERSION} detect-secrets"
else
    DS=""
fi
if [ -n "$DS" ]; then
    SECRETS_BEFORE="${TMPDIR:-/tmp}/aicc-secrets-before.json"
    cp .secrets.baseline "$SECRETS_BEFORE"
    # `detect-secrets scan --baseline` rewrites the file in place, including
    # `generated_at`, so the working tree must be restored however this block
    # ends. Without the trap a *failing* preflight left the baseline modified
    # -- the one outcome where the developer is least likely to notice.
    trap 'cp "$SECRETS_BEFORE" .secrets.baseline 2>/dev/null; rm -f "$SECRETS_BEFORE"' EXIT
    # shellcheck disable=SC2086
    $DS scan --baseline .secrets.baseline
    run_py - "${TMPDIR:-/tmp}/aicc-secrets-before.json" <<'PY'
import collections, json, sys

def inventory(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return collections.Counter(
        (filename, finding["type"])
        for filename, findings in data.get("results", {}).items()
        for finding in findings
    )

before = inventory(sys.argv[1])
after = inventory(".secrets.baseline")
added = sum((after - before).values())
if added:
    for key in (after - before):
        print(f"  new finding: {key[0]} :: {key[1]}")
    raise SystemExit(f"secret baseline changed: {added} added")
PY
    cp "$SECRETS_BEFORE" .secrets.baseline
    rm -f "$SECRETS_BEFORE"
    trap - EXIT
else
    echo "  no detect-secrets and no uvx; skipped (CI still runs it)"
fi

echo "== 6/6 impact-selected tests =="
if [ "${AICC_PREFLIGHT_TESTS:-impact}" = "none" ]; then
    echo "  skipped by AICC_PREFLIGHT_TESTS=none"
else
    base="${AICC_PREFLIGHT_BASE:-origin/main}"
    selected=$(run_py scripts/ci/test_impact/select_tests.py --base "$base" --format pytest)
    if [ -z "$selected" ]; then
        echo "  no test file is reachable from the change"
    elif [ "$selected" = "ALL" ]; then
        # The selector says the change is global (CI config, conftest, lockfile).
        # Preflight does not run the full suite -- that is CI's job, and doing
        # it here would trade the speed this exists for.
        echo "  change is global: the full suite is required, and CI will run it"
    else
        echo "  $(printf '%s' "$selected" | wc -w | tr -d ' ') test file(s) selected"
        # shellcheck disable=SC2086
        run_py -m pytest -q -p no:randomly $selected
    fi
fi

echo "preflight OK"
