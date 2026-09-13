# shellcheck shell=bash
# Shared port-selection and readiness-probing helpers
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# One implementation used by both scripts/provision_postgres_host.sh and
# scripts/aicc_pg_harness.sh, so a fix to either the free-port race or the
# "did the published endpoint actually come up" check cannot end up applied
# to only one of the two routes.
#
# Usage:
#   . "$(dirname "$0")/lib/aicc_pg_net.sh"

# Prints a currently-unbound TCP port on 127.0.0.1 in the ephemeral range.
# This is inherently a check-then-use race -- the caller must treat a bind
# failure on the returned port as "pick another one", not as a hard error.
aicc_pg_free_port() {
    local low=20000 high=29999 port used
    local attempt
    # attempt only bounds the retry count.
    # shellcheck disable=SC2034
    for attempt in $(seq 1 50); do
        port=$((low + RANDOM % (high - low + 1)))
        used="$(ss -ltn 2>/dev/null | awk '{print $4}' | grep -oE '[0-9]+$' | grep -x "${port}" || true)"
        if [ -z "${used}" ]; then
            printf '%s\n' "${port}"
            return 0
        fi
    done
    echo "aicc_pg_free_port: could not find a free port after 50 attempts" >&2
    return 1
}

# True if something on this host is accepting TCP connections on host:port.
# Deliberately a plain TCP connect with no PostgreSQL protocol knowledge: its
# entire job is to answer "does the published endpoint actually work", which
# is exactly the check a container-internal `docker exec ... pg_isready`
# cannot make (it never crosses the published port / host network at all).
aicc_pg_host_reachable() {
    local host="$1" port="$2"
    (exec 3<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

# Polls aicc_pg_host_reachable until it succeeds or timeout_s elapses.
aicc_pg_wait_tcp() {
    local host="$1" port="$2" timeout_s="${3:-60}" elapsed=0
    while [ "${elapsed}" -lt "${timeout_s}" ]; do
        if aicc_pg_host_reachable "${host}" "${port}"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}
