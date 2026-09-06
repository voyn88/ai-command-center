# shellcheck shell=bash
# Shared PostgreSQL server-binary lookup (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Sourced by both check_postgres_host.sh and aicc_pg_harness.sh so the two
# scripts can never disagree about what counts as "server binaries present" --
# a prior review rejected a version of this feature where the test suite's
# assumptions about the bindir search (a hardcoded glob) were not backed by
# any code the scripts under test actually ran.
#
# Usage:
#   . "$(dirname "$0")/lib/aicc_pg_bindir.sh"
#   bindir="$(aicc_pg_find_bindir)" || { echo "no server binaries" >&2; exit 1; }

# Prints a directory containing initdb, pg_ctl and postgres and returns 0, or
# prints nothing and returns 1. Honours AICC_PG_BINDIR as an explicit override
# (used by tests to point at stub binaries without touching PATH).
aicc_pg_find_bindir() {
    if [ -n "${AICC_PG_BINDIR:-}" ]; then
        if [ -x "${AICC_PG_BINDIR}/initdb" ] && [ -x "${AICC_PG_BINDIR}/pg_ctl" ] \
            && [ -x "${AICC_PG_BINDIR}/postgres" ]; then
            printf '%s\n' "${AICC_PG_BINDIR}"
            return 0
        fi
        return 1
    fi

    if command -v initdb >/dev/null 2>&1 && command -v pg_ctl >/dev/null 2>&1 \
        && command -v postgres >/dev/null 2>&1; then
        dirname "$(command -v pg_ctl)"
        return 0
    fi

    # Debian/Ubuntu, RHEL/PGDG, and Homebrew (macOS, both Intel and Apple
    # Silicon prefixes) each install versioned, non-PATH server binaries.
    local candidate
    for candidate in \
        /usr/lib/postgresql/*/bin \
        /usr/pgsql-*/bin \
        /opt/homebrew/opt/postgresql*/bin \
        /usr/local/opt/postgresql*/bin \
        ; do
        if [ -x "${candidate}/initdb" ] && [ -x "${candidate}/pg_ctl" ] \
            && [ -x "${candidate}/postgres" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    return 1
}
