#!/usr/bin/env bash
# Pre-push leak guard (VOYN-OPS-PUBLIC-REPO-CLAUDE-MD-LEAK).
#
# This is a public repository, and the incident class is real and repeated:
# `git add -A` swept an untracked agent-instruction file (CLAUDE.md, carrying
# absolute local paths and internal programme names) into a pushed branch
# twice (#259 on 2026-08-12, again in aios on a closed-PR branch whose diff
# stays publicly visible forever). "Delete it afterwards" does not work for
# public history; the only working fix is "never let it in". The .gitignore
# rule prevents the sweep; this guard is the machine check behind it, per the
# repository invariant "Pre-push checks must block secrets, credentials,
# personal data, internal identifiers in public repositories".
#
# Two deterministic checks over what is about to leave this machine
# (committed range vs the base, plus anything staged):
#   1. No file named CLAUDE.md, at any depth. Agent-instruction files are
#      per-machine context, never repository content. (This is the by-name
#      slice of the record's "internal programme names" criterion: the name
#      VOYN itself appears legitimately in thousands of task ids in this
#      repository, so a global name scan cannot work -- the instruction FILE
#      is the reproducible leak vector, and it is blocked by name.)
#   2. No ADDED line containing an absolute home path (/Users/... or
#      /home/voynadmin). Added lines only: tracked files already contain
#      historical, legitimate /Users/ examples (ROADMAP_STATE.md, UI panel
#      docstrings), and flagging context lines would make every adjacent
#      edit a false positive. This guard's own file is excluded -- it must
#      name the patterns it hunts.
#
# VOYN_LEAK_GUARD=off bypasses (printed, never silent), mirroring
# quality_band.sh. VOYN_LEAK_GUARD_BASE overrides the diff base.
set -uo pipefail
cd "$(dirname "$0")/../../.."

say() { echo "LEAK_GUARD: $*"; }

if [ "${VOYN_LEAK_GUARD:-on}" = "off" ]; then
    say "bypassed (VOYN_LEAK_GUARD=off)"
    exit 0
fi

BASE="${VOYN_LEAK_GUARD_BASE:-origin/main}"
# The guard and its test must name the very patterns they hunt; nothing else
# is exempt.
SELF="scripts/ci/prepush/leak_guard.sh"
SELF_TEST="tests/test_leak_guard.py"

merge_base="$(git merge-base "$BASE" HEAD 2>/dev/null)" || merge_base=""
range_files() {
    if [ -n "$merge_base" ]; then
        git diff --name-only "$merge_base"...HEAD -- .
    fi
    git diff --cached --name-only -- .
}

fail=0

while IFS= read -r path; do
    [ -n "$path" ] || continue
    case "$(basename "$path")" in
        CLAUDE.md | CLAUDE.*.md)  # CLAUDE.local.md and friends, any depth
            say "refused: agent-instruction file '$path' must never be committed"
            fail=1
            ;;
    esac
done < <(range_files | sort -u)

added_lines() {
    if [ -n "$merge_base" ]; then
        git diff --unified=0 "$merge_base"...HEAD -- . \
            ":(exclude)$SELF" ":(exclude)$SELF_TEST"
    fi
    git diff --cached --unified=0 -- . \
        ":(exclude)$SELF" ":(exclude)$SELF_TEST"
}

hits="$(added_lines | grep -nE '^\+' | grep -vE '^\+\+\+' \
    | grep -E '/Users/|/home/voynadmin' | head -5 || true)"
if [ -n "$hits" ]; then
    say "refused: added line(s) carry absolute home paths:"
    printf '%s\n' "$hits"
    fail=1
fi

if [ "$fail" -ne 0 ]; then
    say "fail"
    exit 1
fi
say "pass"
