#!/usr/bin/env bash
# Report whether this host can run a PostgreSQL server for local integration
# runs (tests/db) or production (control-01) -- and by which route.
#
# Written for VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER: a manual, one-off probe
# on voyn-control-01/voyn-worker-01 found `voynadmin` outside the `docker`
# group, no rootless Docker, no Podman, and (at the time) no native
# `postgres`/`pg_ctl`/`initdb` binaries -- so no route to a real server
# existed and tests/db could only skip. This script turns that probe into
# something rerunnable, so the next audit is a command instead of a shell
# session, and so a fix can be verified without repeating it by hand.
#
# Read-only: it runs no installer and touches no service. See
# docs/operations/DATABASE_HOST_PROVISIONING.md for the actual remediation
# and deploy/provision-postgres-host.sh for the automated fix.
#
# A usable native route is either an active postgresql.service *or* at least
# one cluster that pg_lsclusters reports as online. Agent/sandbox sessions on
# an already-provisioned host often cannot talk to systemd (`Transport
# endpoint is not connected`) even while the cluster is listening -- trusting
# only systemctl produced a false "no route" there.
#
# Exit status: 0 when at least one route to a real server is usable, 1
# otherwise. Usage: scripts/check_postgres_host.sh

set -uo pipefail

pass() { printf '  [ok]   %s\n' "$1"; }
fail() { printf '  [miss] %s\n' "$1"; }

native_ok=0
docker_ok=0
podman_ok=0

echo "== container routes =="
if command -v docker >/dev/null 2>&1; then
    if id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        pass "docker installed, $(id -un) is in the docker group"
        docker_ok=1
    elif docker info >/dev/null 2>&1; then
        pass "docker installed and reachable (rootless or already permitted)"
        docker_ok=1
    else
        fail "docker installed but $(id -un) cannot reach the daemon (not in the docker group, no rootless context)"
    fi
else
    fail "docker not installed"
fi

if command -v podman >/dev/null 2>&1; then
    pass "podman installed"
    podman_ok=1
else
    fail "podman not installed"
fi

echo "== native PostgreSQL server =="
server_bin=""
# Absolute package paths first: Debian/Ubuntu put the real binary under
# /usr/lib/postgresql/<ver>/bin/ and do not put `postgres` on PATH.
for candidate in /usr/lib/postgresql/*/bin/postgres; do
    if [[ -x "$candidate" ]]; then
        server_bin="$candidate"
        break
    fi
done
if [[ -z "$server_bin" ]] && command -v postgres >/dev/null 2>&1; then
    server_bin="$(command -v postgres)"
fi
# Fall back to the companion server tools named in the original probe when the
# `postgres` binary itself is absent from the usual locations.
if [[ -z "$server_bin" ]]; then
    for tool in pg_ctl initdb; do
        for candidate in /usr/lib/postgresql/*/bin/"$tool"; do
            if [[ -x "$candidate" ]]; then
                server_bin="$candidate"
                break 2
            fi
        done
        if command -v "$tool" >/dev/null 2>&1; then
            server_bin="$(command -v "$tool")"
            break
        fi
    done
fi

cluster_online=0
if command -v pg_lsclusters >/dev/null 2>&1; then
    # Column 4 of `pg_lsclusters -h` is Status (online/down/…).
    if pg_lsclusters -h 2>/dev/null | awk '$4 == "online" { found=1 } END { exit !found }'; then
        cluster_online=1
    fi
    if pg_lsclusters 2>/dev/null | grep -q .; then
        pg_lsclusters | sed 's/^/         /'
    fi
fi

systemd_active=0
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet postgresql 2>/dev/null; then
    systemd_active=1
fi

if [[ -n "$server_bin" ]]; then
    pass "server binary present ($server_bin)"
    if [[ "$systemd_active" -eq 1 ]]; then
        pass "postgresql.service is active"
        native_ok=1
    elif [[ "$cluster_online" -eq 1 ]]; then
        pass "at least one PostgreSQL cluster is online (pg_lsclusters)"
        native_ok=1
    else
        fail "postgresql.service is not active and no cluster reports online"
    fi
else
    fail "no postgres/pg_ctl/initdb server binary found"
    if [[ "$cluster_online" -eq 1 ]]; then
        # Cluster table can still prove a running server when binaries are
        # outside the paths we searched (unusual packaging).
        pass "at least one PostgreSQL cluster is online (pg_lsclusters)"
        native_ok=1
    fi
fi

if command -v psql >/dev/null 2>&1; then
    pass "psql client present ($(psql --version))"
else
    fail "psql client not found"
fi

echo "== verdict =="
if [[ "$native_ok" -eq 1 || "$docker_ok" -eq 1 || "$podman_ok" -eq 1 ]]; then
    echo "  at least one route to a real PostgreSQL server is usable on this host."
    exit 0
fi
echo "  no route to a real PostgreSQL server: tests/db will skip and control-01 cannot deploy its migrations."
echo "  see docs/operations/DATABASE_HOST_PROVISIONING.md"
exit 1
