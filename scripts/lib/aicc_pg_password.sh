# shellcheck shell=bash
# Shared throwaway-password generation (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Both the container route and the local-binary harness need one superuser
# password for a database that gets torn down at the end of the same run, so
# one implementation is shared rather than risking the two drifting (e.g. one
# route staying on a weaker fallback after the other is hardened).
#
# Usage:
#   . "$(dirname "$0")/lib/aicc_pg_password.sh"
#   password="$(aicc_pg_generate_password)"

aicc_pg_generate_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 16
    elif [ -r /dev/urandom ]; then
        od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
    else
        python3 -c 'import secrets; print(secrets.token_hex(16))'
    fi
}
