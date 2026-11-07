# shellcheck shell=bash
# Shared throwaway-password generation (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

aicc_pg_generate_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 16
    elif [ -r /dev/urandom ]; then
        od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
    else
        python3 -c 'import secrets; print(secrets.token_hex(16))'
    fi
}
