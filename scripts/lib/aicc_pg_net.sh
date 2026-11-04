# shellcheck shell=bash
# Shared port-selection and readiness-probing helpers
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

aicc_pg_free_port() {
    local low=20000 high=29999 port used
    local attempt
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

aicc_pg_host_reachable() {
    local host="$1" port="$2"
    (exec 3<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1
}

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
