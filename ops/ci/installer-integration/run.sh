#!/bin/bash
# Installer integration next to a live legacy lane (VOYN-W0-AICC-INSTALLER-INTEGRATION-TEST-NEXT-TO-LEGACY).
#
# Runs as root inside a privileged systemd container with the repository
# mounted read-only at /repo. It reproduces the exact host shape that produced
# five live-only installer defects on 2026-09-08 -- a legacy template lane
# (User=voynadmin, legacy runtime root) still running while the isolated
# template is installed and lane 1 is rolled out through
# ops/aicc_staged_worker_rollout.py -- and asserts what each defect broke:
#   * the release root must be traversable by aicc-worker (#823: 0500 root, 200/CHDIR)
#   * the isolated lane's runtime root must not nest under the legacy one (#858)
#   * lane 1 must reach READY under aicc-worker with the isolation flag in its
#     real process environment (rollout verify_unit), credentials mounted and
#     the rotator's env file readable
#   * the exact-workspace sibling canary of the boundary script must pass with
#     lazily-created trees absent (#874) and a retired legacy family unit
#     not-found (#878)
# The worker itself is a stub (systemd-notify READY / SIGHUP reload): the
# database, AIOS wheels and executor toolchains are out of scope here and
# covered by the release preflight (#860) and the live canary cycle.
set -euo pipefail
REPO=/repo
log() { printf '\n== %s\n' "$*"; }
fail() { printf 'INSTALLER-INTEGRATION FAIL: %s\n' "$*" >&2; exit 1; }

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq python3 python3-venv git sudo >/dev/null

log "principals (repo sysusers/tmpfiles)"
id voynadmin >/dev/null 2>&1 || useradd -m -s /bin/bash voynadmin
systemd-sysusers "$REPO/deploy/sysusers.d/aicc-agent.conf"
systemd-tmpfiles --create "$REPO/deploy/tmpfiles.d/aicc-agent.conf"
install -d -m 0750 -o root -g aicc-worker /var/lib/aicc-worker
install -d -m 0750 -o aicc-rotator -g aicc-worker /var/lib/voyn-aicc-credential-rotation
install -d -m 0755 /etc/aicc /etc/voyn /etc/voyn/secrets
chmod 0700 /etc/voyn/secrets

log "stub worker + legacy lane 2 running under voynadmin (legacy runtime root)"
cat > /usr/local/bin/aicc-stub-worker <<'STUB'
#!/usr/bin/python3
"""Stand-in for `python -m command_center.worker`: proves the unit envelope,
not the worker. Notifies from the MAIN PID (NotifyAccess=main in the isolated
template rejects a child `systemd-notify`), READY only after the credential
and env file are readable, RELOADING/READY on SIGHUP, exits on SIGTERM."""
import os
import signal
import socket
import sys
import time


def notify(text: str) -> None:
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(path)
        sock.sendall(text.encode())


env_file = os.environ.get("AICC_WORKER_ENV_FILE", "")
if env_file and not os.access(env_file, os.R_OK):
    print(f"stub: cannot read {env_file}", file=sys.stderr)
    sys.exit(3)
pgpass = os.environ.get("PGPASSFILE", "")
if pgpass and not os.access(pgpass, os.R_OK):
    print(f"stub: cannot read {pgpass}", file=sys.stderr)
    sys.exit(3)


def on_hup(signum, frame):
    notify("RELOADING=1\nMONOTONIC_USEC=%d" % (time.monotonic_ns() // 1000))
    time.sleep(0.2)
    notify("READY=1\nSTATUS=aicc-ready")


def on_term(signum, frame):
    sys.exit(0)


signal.signal(signal.SIGHUP, on_hup)
signal.signal(signal.SIGTERM, on_term)
notify("READY=1\nSTATUS=aicc-ready")
while True:
    time.sleep(1)
STUB
chmod 0755 /usr/local/bin/aicc-stub-worker
cat > /etc/systemd/system/voyn-aicc-worker@.service <<'LEGACY'
[Unit]
Description=AICC queue worker (instance %i)
[Service]
Type=notify
User=voynadmin
RuntimeDirectory=voyn-aicc-worker
RuntimeDirectoryMode=0750
ExecStart=/usr/local/bin/aicc-stub-worker
Restart=always
[Install]
WantedBy=multi-user.target
LEGACY
systemctl daemon-reload
systemctl enable --now voyn-aicc-worker@2.service
for _ in $(seq 1 30); do [ "$(systemctl show voyn-aicc-worker@2 -p StatusText --value)" = aicc-ready ] && break; sleep 1; done
[ "$(systemctl show voyn-aicc-worker@2 -p StatusText --value)" = aicc-ready ] || fail "legacy lane 2 did not reach READY"
[ "$(stat -c %U /run/voyn-aicc-worker)" = voynadmin ] || fail "legacy runtime root is not owned by voynadmin (test shape)"

log "immutable release at /opt/aicc/releases/<sha> the installer would stage"
sha=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo 0000000000000000000000000000000000000000)
release=/opt/aicc/releases/$sha
install -d -m 0755 /opt/aicc/releases
mkdir -p "$release"
(cd "$REPO" && tar -cf - --exclude=.git --exclude=.venv .) | tar -xf - -C "$release"
mkdir -p "$release/.venv/bin"
cat > "$release/.venv/bin/python" <<'SHIM'
#!/bin/sh
# The template's ExecStart: /opt/aicc/current/.venv/bin/python -m command_center.worker
exec /usr/local/bin/aicc-stub-worker "$@"
SHIM
chown -R root:root "$release"; chmod -R a-w "$release"; chmod 0555 "$release"; chmod 0555 "$release/.venv/bin/python"
ln -sfn "$release" /opt/aicc/current
runuser -u aicc-worker -- test -x /opt/aicc/current || fail "aicc-worker cannot traverse the release root (#823 shape)"
runuser -u aicc-worker -- test -x /opt/aicc/current/.venv/bin/python || fail "aicc-worker cannot execute the release interpreter"

log "host files the isolated template and the rollout require"
printf 'VOYN_LEASE_DSN=host=127.0.0.1 port=5433 dbname=x user=y\n' > /etc/aicc/lease.env; chmod 0640 /etc/aicc/lease.env; chgrp aicc-worker /etc/aicc/lease.env
printf 'AICC_WORKSPACE_AUTHORITY_KEY=hex:%s\n' "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /etc/aicc/workspace-authority.env
chown root:aicc-publisher /etc/aicc/workspace-authority.env; chmod 0640 /etc/aicc/workspace-authority.env
: > /etc/aicc/executors.env; chmod 0640 /etc/aicc/executors.env; chgrp aicc-worker /etc/aicc/executors.env
for i in 1 2; do printf 'AICC_PUBLISH_OWNER=lane-%s\nVOYN_LEASE_SESSION=lane-%s\n' "$i" "$i" > /etc/aicc/worker-$i.env; chmod 0644 /etc/aicc/worker-$i.env; done
printf 'AICC_PG_HOST=127.0.0.1\nAICC_PG_PORT=5433\nAICC_PG_USER=aicc_w_wrk_test\nAICC_PG_PASSWORD=secret\n' > /var/lib/voyn-aicc-credential-rotation/worker.env
chown aicc-rotator:aicc-worker /var/lib/voyn-aicc-credential-rotation/worker.env; chmod 0640 /var/lib/voyn-aicc-credential-rotation/worker.env
printf '127.0.0.1:5433:*:aicc_w_wrk_test:secret\n' > /etc/voyn/secrets/voyn_lease_pgpass; chmod 0600 /etc/voyn/secrets/voyn_lease_pgpass
install -m 0644 "$REPO/deploy/aicc/worker-lanes" /etc/aicc/worker-lanes
install -m 0644 "$REPO/deploy/aicc/privileged-principals" /etc/aicc/privileged-principals
install -m 0644 "$REPO/deploy/aicc/agent-workspace-roots" /etc/aicc/agent-workspace-roots
install -m 0640 -g aicc-agent "$REPO/deploy/aicc/agent.env" /etc/aicc/agent.env
mkdir -p /home/voynadmin/Projects/ai-command-center

log "install the isolated template + drop-in over the legacy template (lane 2 keeps running)"
install -m 0644 "$REPO/deploy/systemd/voyn-aicc-worker@.service" /etc/systemd/system/voyn-aicc-worker@.service
install -d -m 0755 /etc/systemd/system/voyn-aicc-worker@.service.d
install -m 0644 "$REPO/deploy/systemd/voyn-aicc-worker-principal-isolation.conf" /etc/systemd/system/voyn-aicc-worker@.service.d/20-principal-isolation.conf
# The drop-in Requires= the recovery barrier; provide an inert one here.
cat > /etc/systemd/system/aicc-principal-recovery.service <<'REC'
[Unit]
Description=inert recovery barrier (integration harness)
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
REC
systemctl daemon-reload
systemctl start aicc-principal-recovery.service
[ "$(systemctl is-active voyn-aicc-worker@2)" = active ] || fail "legacy lane 2 stopped when the template was replaced"

log "staged rollout of the registry lanes (lane 1 first, legacy lane 2 beside it)"
python3 "$REPO/ops/aicc_staged_worker_rollout.py" rollout --lanes /etc/aicc/worker-lanes --agent-user aicc-agent \
  || fail "staged rollout refused (see RolloutError above)"

log "assertions on the isolated lane"
[ "$(systemctl show voyn-aicc-worker@1 -p User --value)" = aicc-worker ] || fail "lane 1 is not User=aicc-worker"
[ "$(systemctl show voyn-aicc-worker@1 -p StatusText --value)" = aicc-ready ] || fail "lane 1 is not READY"
pid=$(systemctl show voyn-aicc-worker@1 -p MainPID --value)
[ "$(stat -c %U /proc/$pid)" = aicc-worker ] || fail "lane 1 MainPID does not run as aicc-worker"
tr '\0' '\n' < /proc/$pid/environ | grep -qx 'AICC_AGENT_PRINCIPAL_ISOLATION=required' || fail "isolation flag absent from the live process environment"
[ -f /run/aicc-worker-lanes/1/pgpass ] || fail "credential was not installed under the lane's own runtime root (#858 shape)"
[ ! -e /run/voyn-aicc-worker/1 ] || fail "isolated lane nested under the legacy runtime root"
systemctl reload voyn-aicc-worker@1 || fail "lane 1 does not support reload (rotation needs it)"

log "boundary: exact-workspace sibling canary and flag checks (script excerpt conditions)"
# The full boundary script also proves the launcher socket and immutable
# binaries the installer places; here we run its two checks that failed live
# without an install: the sibling canary namespace and the flag/family loop.
canary_root=$(mktemp -d /srv/aicc-workspaces/.principal-boundary.XXXXXX)
mkdir "$canary_root/workspace" "$canary_root/sibling"; chown -R root:aicc-workspace "$canary_root"
chmod 2770 "$canary_root" "$canary_root/workspace" "$canary_root/sibling"
: > "$canary_root/workspace/visible"; : > "$canary_root/sibling/must-not-read"
chown root:aicc-workspace "$canary_root/workspace/visible" "$canary_root/sibling/must-not-read"; chmod 0660 "$canary_root"/*/*
paths=$(sed -n 's/^principal_inaccessible_paths="\(.*\)"$/\1/p' "$REPO/ops/verify-agent-principal-boundary.sh")
[ -n "$paths" ] || fail "cannot read the boundary script's inaccessible path list"
systemd-run --quiet --wait --pipe --collect --property=DynamicUser=yes \
  --property="SupplementaryGroups=aicc-workspace aicc-agent-auth" --property=NoNewPrivileges=yes \
  --property=ProtectSystem=strict --property="InaccessiblePaths=$paths /srv/aicc-workspaces" \
  --property="BindPaths=$canary_root/workspace:/workspace" -- /bin/sh -c \
  'test -r /workspace/visible && ! test -r /srv/aicc-workspaces/.principal-boundary.*/sibling/must-not-read' \
  || fail "exact-workspace sibling isolation canary failed (#874 shape: absent trees must be tolerated)"
rm -rf "$canary_root"
for unit in aicc-worker.service voyn-aicc-worker@1.service voyn-aicc-worker@2.service; do
  load=$(systemctl show "$unit" --property=LoadState --value)
  if [ "$load" = not-found ]; then [ "$unit" = aicc-worker.service ] || fail "registered lane not loaded: $unit"; continue; fi
  systemctl show "$unit" --property=Environment --value | tr ' ' '\n' | grep -qx 'AICC_AGENT_PRINCIPAL_ISOLATION=required' \
    || fail "isolation flag did not reach $unit exactly"
done
echo "INSTALLER-INTEGRATION PASS: lane 1 isolated next to a live legacy lane; release traversable; runtime roots distinct; canary + flag checks pass"
