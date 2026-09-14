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
# Every route below is confirmed with an operational probe, not inferred
# from installed-ness. Adversarial review of an earlier version (PR #435)
# found two ways that produced a false "usable" verdict: group membership
# without a reachable daemon (docker), and an executable without a working
# backend (podman) -- both fixed by actually calling into the engine.
# Similarly, `postgresql.service` is a Debian meta/umbrella unit whose
# active state does not establish that any cluster underneath it is online
# and accepting connections, so the native check reads `pg_lsclusters`
# for an actual online cluster and, where `pg_isready` exists, confirms it
# accepts connections rather than trusting the service's activation state.
#
# Read-only: it runs no installer and touches no service. See
# docs/operations/DATABASE_HOST_PROVISIONING.md for the actual remediation
# and deploy/provision-postgres-host.sh for the automated fix.
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
    in_group=0
    if id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        in_group=1
    fi
    if docker info >/dev/null 2>&1; then
        if [[ "$in_group" -eq 1 ]]; then
            pass "docker installed, $(id -un) is in the docker group, and the daemon is reachable"
        else
            pass "docker installed and reachable (rootless or already permitted)"
        fi
        docker_ok=1
    elif [[ "$in_group" -eq 1 ]]; then
        fail "docker installed and $(id -un) is in the docker group, but \`docker info\` failed (daemon not running, or socket unreachable)"
    else
        fail "docker installed but $(id -un) cannot reach the daemon (not in the docker group, no rootless context)"
    fi
else
    fail "docker not installed"
fi

if command -v podman >/dev/null 2>&1; then
    if podman info >/dev/null 2>&1; then
        pass "podman installed and operational (\`podman info\` succeeded)"
        podman_ok=1
    else
        fail "podman installed but \`podman info\` failed (rootless storage/subuid likely unconfigured)"
    fi
else
    fail "podman not installed"
fi

echo "== native PostgreSQL server =="
server_bin=""
for candidate in /usr/lib/postgresql/*/bin/postgres postgres; do
    if command -v "$candidate" >/dev/null 2>&1; then
        server_bin="$candidate"
        break
    fi
done

if [[ -n "$server_bin" ]]; then
    pass "server binary present ($server_bin)"

    online_cluster=""
    if command -v pg_lsclusters >/dev/null 2>&1; then
        cluster_lines="$(pg_lsclusters 2>/dev/null | tail -n +2)"
        if [[ -n "$cluster_lines" ]]; then
            echo "$cluster_lines" | sed 's/^/         /'
            online_cluster="$(echo "$cluster_lines" | awk '$4 == "online" {print; exit}')"
        fi
    fi

    if [[ -z "$online_cluster" ]]; then
        if command -v pg_lsclusters >/dev/null 2>&1; then
            fail "pg_lsclusters reports no cluster online (postgresql.service being active does not mean a cluster is running -- it is a meta unit)"
        else
            fail "pg_lsclusters not found, cannot confirm any cluster is actually online"
        fi
    else
        cluster_port="$(echo "$online_cluster" | awk '{print $3}')"
        if command -v pg_isready >/dev/null 2>&1; then
            if pg_isready -h 127.0.0.1 -p "${cluster_port:-5432}" >/dev/null 2>&1; then
                pass "pg_isready confirms the cluster on port ${cluster_port:-5432} accepts connections"
                native_ok=1
            else
                fail "pg_lsclusters reports the cluster online, but pg_isready cannot reach port ${cluster_port:-5432}"
            fi
        else
            fail "pg_lsclusters reports the cluster online, but pg_isready is not installed to confirm it accepts connections"
        fi
    fi
else
    fail "no postgres/pg_ctl/initdb server binary found"
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
