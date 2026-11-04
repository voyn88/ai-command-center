# shellcheck shell=bash
# Shared PostgreSQL server-binary lookup (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Sourced by select_postgres_host_route.sh and aicc_pg_harness.sh so the two
# scripts can never disagree about what counts as "server binaries present".
#
# Usage:
#   . "$(dirname "$0")/lib/aicc_pg_bindir.sh"
#   bindir="$(aicc_pg_find_bindir)" || { echo "no server binaries" >&2; exit 1; }

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
