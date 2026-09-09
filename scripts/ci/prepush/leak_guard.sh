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
#   2. No ADDED line containing an absolute macOS or worker home path.
#      (The literals are never written out in this file -- see the
#      HOME_ROOT_* halves --
#      so the guard can scan itself.) Added lines only: tracked files
#      already contain historical, legitimate home-path examples (
#      ROADMAP_STATE.md, UI panel
#      docstrings), and flagging context lines would make every adjacent
#      edit a false positive. This guard's own file is excluded -- it must
#      name the patterns it hunts.
#
# Check 2 has two narrow exemptions
# (VOYN-W0-AICC-LEAK-GUARD-BLOCKS-FILES-THAT-ALREADY-CARRY-HOME-PATHS: some
# repository files carry a home path BY DESIGN -- the installer-integration
# fixture builds a clone at the worker's real path, the systemd units bind
# and WorkingDirectory it -- and a guard that refuses every added line with
# one made those files uneditable by the fleet: on 2026-09-09 the installer
# task died on `publish failed: leak_guard_failed` on every attempt and
# parked). An occurrence is exempt only when:
#   A. The SAME absolute path already occurs in the BASE version of the SAME
#      file. Re-adding what the file already publishes discloses nothing new;
#      a path the base file does not carry is still refused, so a fresh leak
#      in a file that happens to carry one elsewhere does not ride in.
#   B. The file matches an entry of the in-repo allowlist
#      (scripts/ci/prepush/leak_guard_allowlist) whose token covers the path's
#      home root. The allowlist is read from the BASE commit, never from the
#      working tree: a branch cannot widen the guard that is judging it -- the
#      entry has to land on the base branch through review first. The only
#      token is WORKER_HOME (the deployment account root, already published by
#      design in deploy/ and ops/ci/); personal machine home roots are never
#      allowlistable, in any file.
# Exemptions are per-OCCURRENCE, not per line and not per file: a line is
# refused unless every home path on it is exempt. Everything else -- code,
# docs, tests -- keeps refusing on the first hit.
#
# VOYN_LEAK_GUARD=off bypasses (printed, never silent), mirroring
# quality_band.sh. VOYN_LEAK_GUARD_BASE overrides the diff base.
set -uo pipefail
# $1 (optional): the repository to scan. Defaults to this script's own repo
# for interactive/preflight use; publish_run passes the candidate worktree
# while executing THIS trusted copy, never the candidate's.
cd "${1:-$(dirname "$0")/../../..}"

say() { echo "LEAK_GUARD: $*"; }

if [ "${VOYN_LEAK_GUARD:-on}" = "off" ]; then
    say "bypassed (VOYN_LEAK_GUARD=off)"
    exit 0
fi

BASE="${VOYN_LEAK_GUARD_BASE:-origin/main}"
# NOTHING is exempt from the added-line scan (verification finding on
# f4616fd: whole-file exclusions for the guard and its test let a private
# home path ride in through exactly those files). This file and the test
# therefore never contain the hunted literals -- the pattern is assembled
# from halves at runtime, so the guard can scan its own diffs too.
HOME_ROOT_USER="/Use""rs/"
HOME_ROOT_WORK="/home/voyn""admin"
# One occurrence = a home root plus the path characters that follow it.
# Quotes, spaces, commas, colons and parens end the token, so a path quoted
# or embedded in prose is extracted as itself and nothing more.
HOME_TOKEN_PAT="($HOME_ROOT_USER|$HOME_ROOT_WORK)[A-Za-z0-9._/@+-]*"
if [ -z "$HOME_ROOT_USER" ] || [ -z "$HOME_ROOT_WORK" ]; then
    say "refused: internal error (empty scan pattern)"
    exit 1
fi

# Fail CLOSED on an unresolvable base (verification finding 1 on f24d081:
# an empty merge_base silently skipped both committed-range scans and a
# committed leak reached "pass"). A guard that cannot see the range must
# refuse; VOYN_LEAK_GUARD=off stays the printed escape hatch.
merge_base="$(git merge-base "$BASE" HEAD 2>/dev/null)" || merge_base=""
if [ -z "$merge_base" ]; then
    say "refused: cannot resolve base '$BASE' (set VOYN_LEAK_GUARD_BASE, or VOYN_LEAK_GUARD=off to bypass)"
    exit 1
fi

# --diff-filter=d everywhere: deletions (and the rename-FROM side) must not
# be refused -- deleting a leaked CLAUDE.md is exactly the remediation this
# guard exists to force (verification finding 2 on f24d081). A rename-TO
# still shows under the new name and is refused.
range_files() {
    git diff --name-only --diff-filter=d "$merge_base"...HEAD -- .
    git diff --cached --name-only --diff-filter=d -- .
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

# ---- allowlist (rule B), read from the BASE commit only ----------------
ALLOWLIST_PATH="scripts/ci/prepush/leak_guard_allowlist"
ALLOW_GLOB=()
ALLOW_TOKEN=()
while read -r glob token extra; do
    case "$glob" in ''|'#'*) continue ;; esac
    if [ -z "$token" ] || [ -n "$extra" ]; then
        say "refused: malformed $ALLOWLIST_PATH entry: $glob $token $extra"
        say "fail"
        exit 1
    fi
    case "$token" in
        WORKER_HOME) ;;
        *)  # Fail closed on a token this guard does not implement, rather
            # than silently ignoring an entry an operator believes is live.
            say "refused: unknown $ALLOWLIST_PATH token '$token' (only WORKER_HOME)"
            say "fail"
            exit 1
            ;;
    esac
    ALLOW_GLOB+=("$glob")
    ALLOW_TOKEN+=("$token")
done < <(git show "$merge_base:$ALLOWLIST_PATH" 2>/dev/null || true)

token_home_root() {
    case "$1" in
        "$HOME_ROOT_WORK"*) echo "WORKER_HOME" ;;
        *) echo "USER_HOME" ;;  # personal machine root: never allowlistable
    esac
}

# Single-entry cache: the diff arrives grouped by file, so one `git show`
# per file is enough even for a diff that adds many home-path lines.
blob_key=""
blob_text=""
base_carries() {  # <base-ref> <path> <token>
    local key="$1:$2"
    if [ "$blob_key" != "$key" ]; then
        blob_key="$key"
        blob_text="$(git show "$1:$2" 2>/dev/null || true)"
    fi
    case "$blob_text" in *"$3"*) return 0 ;; esac
    return 1
}

occurrence_is_exempt() {  # <base-ref> <path> <token>
    local ref="$1" path="$2" token="$3" root i
    base_carries "$ref" "$path" "$token" && return 0   # rule A
    root="$(token_home_root "$token")"
    for ((i = 0; i < ${#ALLOW_GLOB[@]}; i++)); do      # rule B
        [ "${ALLOW_TOKEN[i]}" = "$root" ] || continue
        # shellcheck disable=SC2254 — the entry IS a glob, matched unquoted
        # on purpose; `*` spans directory separators, so `ops/ci/**` covers
        # every depth under it.
        case "$path" in ${ALLOW_GLOB[i]}) return 0 ;; esac
    done
    return 1
}

# Added lines with the file they land in. `+++ b/<path>` opens each file's
# hunks; everything else starting with '+' is added content.
added_lines_with_path() {
    # Prefixes pinned and quotepath off: the `+++ b/<path>` header is how the
    # file is identified, and diff.mnemonicPrefix / diff.noprefix /
    # core.quotepath in an operator's config would otherwise reshape it.
    git -c core.quotepath=false diff --unified=0 \
        --src-prefix=a/ --dst-prefix=b/ "$@" -- . | awk '
        /^\+\+\+ /{
            p = substr($0, 5)
            if (p ~ /^".*"$/) p = substr(p, 2, length(p) - 2)
            sub(/^b\//, "", p)
            if (p == "/dev/null") p = ""
            next
        }
        /^\+/{ if (p != "") print p "\t" substr($0, 2) }
    '
}

hit_count=0
hit_report=""
scan_added() {  # <base-ref-for-same-file-lookups> <git diff args...>
    local ref="$1" path line token clean
    shift
    while IFS=$'\t' read -r path line; do
        case "$line" in
            *"$HOME_ROOT_USER"*|*"$HOME_ROOT_WORK"*) ;;
            *) continue ;;
        esac
        clean=1
        while IFS= read -r token; do
            [ -n "$token" ] || continue
            occurrence_is_exempt "$ref" "$path" "$token" || { clean=0; break; }
        done < <(printf '%s\n' "$line" | grep -oE "$HOME_TOKEN_PAT")
        [ "$clean" -eq 1 ] && continue
        hit_count=$((hit_count + 1))
        if [ "$hit_count" -le 5 ]; then
            hit_report="$hit_report$path: ${line:0:160}"$'\n'
        fi
    done < <(added_lines_with_path "$@")
}

# The committed range is judged against the merge base; staged-but-uncommitted
# changes against HEAD -- in both cases "the version this edit starts from".
scan_added "$merge_base" "$merge_base"...HEAD
scan_added HEAD --cached

if [ "$hit_count" -ne 0 ]; then
    say "refused: added line(s) carry absolute home paths:"
    printf '%s' "$hit_report"
    [ "$hit_count" -gt 5 ] && say "... and $((hit_count - 5)) more"
    fail=1
fi

if [ "$fail" -ne 0 ]; then
    say "fail"
    exit 1
fi
say "pass"
