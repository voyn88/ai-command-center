from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / "ops" / "voyn_worker_health.sh"
SYNC = ROOT / "ops" / "voyn_sync_findings.sh"
CANARY = ROOT / "ops" / "voyn_aicc_worker_canary.sh"
RECONCILE = ROOT / "ops" / "voyn_worker_reconcile.sh"
DESIRED_READER = ROOT / "command_center" / "orchestrator" / "desired_state.py"
DESIRED_STATE = ROOT / "deploy" / "config" / "aicc-desired-state.json"


def _desired_env(path: Path = DESIRED_STATE) -> dict[str, str]:
    return {
        "PYTHON_BIN": sys.executable,
        "DESIRED_STATE_READER": str(DESIRED_READER),
        "AICC_DESIRED_STATE_FILE": str(path),
        "SYNC_BIN": "/usr/bin/true",
    }


def _fake_systemctl(tmp_path: Path) -> Path:
    script = tmp_path / "systemctl"
    script.write_text(
        """#!/usr/bin/env bash
set -eu
unit=$2
stamp=990000000
if [[ ${STALE_UNIT:-} == "$unit" ]]; then stamp=700000000; fi
cat <<EOF
LoadState=loaded
ActiveState=active
SubState=running
Type=notify
MainPID=123
Result=success
WatchdogUSec=3900000000
WatchdogTimestampMonotonic=$stamp
NRestarts=0
EOF
"""
    )
    script.chmod(0o755)
    return script


def test_health_requires_both_current_aicc_worker_watchdogs(tmp_path):
    systemctl = _fake_systemctl(tmp_path)
    uptime = tmp_path / "uptime"
    uptime.write_text("1000.00 1.00\n")
    result = subprocess.run(
        [str(HEALTH)],
        env={
            **os.environ,
            **_desired_env(),
            "SYSTEMCTL_BIN": str(systemctl),
            "UPTIME_FILE": str(uptime),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "voyn-aicc-worker@1.service ready=1" in result.stdout
    assert "voyn-aicc-worker@2.service ready=1" in result.stdout
    source = HEALTH.read_text()
    assert "voyn-claude.service" not in source
    assert "/run/voyn-claude/heartbeat" not in source


def test_health_fails_closed_when_either_worker_watchdog_is_stale(tmp_path):
    systemctl = _fake_systemctl(tmp_path)
    uptime = tmp_path / "uptime"
    uptime.write_text("1000.00 1.00\n")
    result = subprocess.run(
        [str(HEALTH)],
        env={
            **os.environ,
            **_desired_env(),
            "SYSTEMCTL_BIN": str(systemctl),
            "UPTIME_FILE": str(uptime),
            "STALE_UNIT": "voyn-aicc-worker@2.service",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "voyn-aicc-worker@2.service" in result.stdout
    assert "stale" in result.stdout


def test_health_fails_closed_after_restart_storm(tmp_path):
    systemctl = _fake_systemctl(tmp_path)
    systemctl.write_text(systemctl.read_text().replace("NRestarts=0", "NRestarts=4"))
    uptime = tmp_path / "uptime"
    uptime.write_text("1000.00 1.00\n")

    result = subprocess.run(
        [str(HEALTH)],
        env={
            **os.environ,
            **_desired_env(),
            "SYSTEMCTL_BIN": str(systemctl),
            "UPTIME_FILE": str(uptime),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "restarts=4 exceeds=3" in result.stdout


def test_findings_sync_uses_configured_dns_pinned_host_and_moves_only_on_success(
    tmp_path,
):
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent"
    outbox.mkdir()
    sent.mkdir()
    finding = outbox / "finding.json"
    finding.write_text('{"finding":"x"}\n')
    identity = tmp_path / "identity"
    identity.write_text("test-private-key")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("control.example ssh-ed25519 AAAATEST\n")
    calls = tmp_path / "calls"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>{calls!s}\ncat >/dev/null\n"
    )
    ssh.chmod(0o755)
    flock = tmp_path / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n")
    flock.chmod(0o755)

    result = subprocess.run(
        [str(SYNC)],
        env={
            **os.environ,
            "VOYN_FINDINGS_ENDPOINT": "control.example",
            "VOYN_FINDINGS_IDENTITY": str(identity),
            "VOYN_FINDINGS_KNOWN_HOSTS": str(known_hosts),
            "VOYN_FINDINGS_OUTBOX": str(outbox),
            "VOYN_FINDINGS_SENT": str(sent),
            "SSH_BIN": str(ssh),
            "FLOCK_BIN": str(flock),
            "MV_BIN": "/bin/mv",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not finding.exists()
    assert (sent / finding.name).exists()
    argv = calls.read_text()
    assert "control.example" in argv
    assert "UserKnownHostsFile=" + str(known_hosts) in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "10.20.0.2" not in SYNC.read_text()


def test_worker_monitor_units_and_installer_are_versioned_and_non_disruptive():
    systemd = ROOT / "deploy" / "systemd"
    expected = {
        "voyn-worker-health.service",
        "voyn-worker-health.timer",
        "voyn-worker-reconciler.service",
        "voyn-worker-reconciler.timer",
        "voyn-findings-sync.service",
        "voyn-findings-sync.timer",
        "voyn-canary.service",
    }
    assert expected <= {path.name for path in systemd.iterdir()}
    canary = (systemd / "voyn-canary.service").read_text()
    installer = (ROOT / "ops" / "install_worker_monitors.sh").read_text()
    config = (ROOT / "deploy" / "config" / "voyn-findings-sync.env").read_text()

    registry = DESIRED_STATE.read_text()
    assert "voyn-aicc-worker@1.service" in registry
    assert "voyn-aicc-worker@2.service" in registry
    assert "aicc-desired-state.json" in CANARY.read_text()
    assert "aicc-desired-state.json" in HEALTH.read_text()
    assert "aicc-desired-state.json" in RECONCILE.read_text()
    assert "voyn-claude.service" not in canary
    assert "voyn-control-01.tail39d0b6.ts.net" in config
    assert "exact 40-character merged SHA" in installer
    assert "/var/lib/voyn-worker-monitor" in installer
    assert "deployed-sha" in installer
    assert (
        "runuser -u voynadmin -- /opt/voyn-worker/bin/voyn-sync-findings check"
        in installer
    )
    assert "status --porcelain)" in installer
    assert "--untracked-files=no" not in installer
    assert "restart voyn-aicc-worker" not in installer
    assert "voyn-worker-reconcile" in installer
    assert installer.index("systemctl daemon-reload") < installer.index(
        "printf '%s\\n' \"$EXPECTED_SHA\""
    )
    assert installer.index("voyn-worker-health") < installer.index(
        "printf '%s\\n' \"$EXPECTED_SHA\""
    )


def test_canary_binds_evidence_to_the_exact_deployed_monitor_sha():
    canary = (ROOT / "ops" / "voyn_aicc_worker_canary.sh").read_text()

    assert "VOYN_MONITOR_DEPLOYED_SHA_FILE" in canary
    assert "deployed_sha" in canary
    assert "^[0-9a-f]{40}$" in canary
    assert "restart_baseline_" in canary
    assert "restart_deltas" in canary


def test_worker_recovery_is_allowlisted_bounded_and_circuit_broken():
    source = RECONCILE.read_text()

    assert "worker-units" in source
    assert "--kill-who=main --signal=TERM" in source
    assert 'restart "$unit"' not in source
    assert "ready lane quorum" in source
    assert "worker-minimum-stop-seconds" in source
    assert "MAX_FAILURES" in source
    assert "open_until" in source
    assert "eval" not in source


def test_worker_recovery_drains_only_failed_lane_and_keeps_sibling_ready(tmp_path):
    recovered = tmp_path / "recovered"
    calls = tmp_path / "calls"
    health = tmp_path / "health"
    health.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ $1 == voyn-aicc-worker@1.service && ! -f {recovered!s} ]]; then exit 1; fi\n"
        "exit 0\n"
    )
    health.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >>{calls!s}\n"
        "if [[ $* == *TimeoutStopUSec* ]]; then echo 3660000000; exit 0; fi\n"
        "if [[ $* == *ActiveState* ]]; then echo active; exit 0; fi\n"
        f"if [[ $1 == kill ]]; then touch {recovered!s}; fi\n"
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        [str(RECONCILE)],
        env={
            **os.environ,
            **_desired_env(),
            "SYSTEMCTL_BIN": str(systemctl),
            "HEALTH_PROBE": str(health),
            "STATE_DIRECTORY": str(tmp_path / "state"),
            "CHOWN_BIN": "/usr/bin/true",
            "VOYN_WORKER_RECOVERY_POLL_SECONDS": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invoked = calls.read_text()
    assert "kill --kill-who=main --signal=TERM voyn-aicc-worker@1.service" in invoked
    assert "voyn-aicc-worker@2.service" not in "\n".join(
        line for line in invoked.splitlines() if line.startswith("kill ")
    )
    assert "restart" not in invoked


def test_worker_recovery_refuses_timeout_shorter_than_declared_job_budget(tmp_path):
    calls = tmp_path / "calls"
    health = tmp_path / "health"
    health.write_text("#!/usr/bin/env bash\n[[ $1 != voyn-aicc-worker@1.service ]]\n")
    health.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >>{calls!s}\n"
        "if [[ $* == *TimeoutStopUSec* ]]; then echo 330000000; exit 0; fi\n"
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        [str(RECONCILE)],
        env={
            **os.environ,
            **_desired_env(),
            "SYSTEMCTL_BIN": str(systemctl),
            "HEALTH_PROBE": str(health),
            "STATE_DIRECTORY": str(tmp_path / "state"),
            "CHOWN_BIN": "/usr/bin/true",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unsafe stop timeout 330s" in result.stderr
    assert "kill " not in calls.read_text()


def test_worker_recovery_refuses_to_cycle_both_unhealthy_lanes(tmp_path):
    desired = json.loads(DESIRED_STATE.read_text())
    desired["worker_fleet"]["circuit_failure_threshold"] = 1
    registry = tmp_path / "desired.json"
    registry.write_text(json.dumps(desired))
    calls = tmp_path / "calls"
    health = tmp_path / "health"
    health.write_text("#!/usr/bin/env bash\nexit 1\n")
    health.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>{calls!s}\n")
    systemctl.chmod(0o755)

    result = subprocess.run(
        [str(RECONCILE)],
        env={
            **os.environ,
            **_desired_env(registry),
            "SYSTEMCTL_BIN": str(systemctl),
            "HEALTH_PROBE": str(health),
            "STATE_DIRECTORY": str(tmp_path / "state"),
            "CHOWN_BIN": "/usr/bin/true",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ready lane quorum would be violated" in result.stderr
    assert not calls.exists() or "kill" not in calls.read_text()
    assert "failures=1" in (tmp_path / "state" / "circuit").read_text()

    second = subprocess.run(
        [str(RECONCILE)],
        env={
            **os.environ,
            **_desired_env(registry),
            "SYSTEMCTL_BIN": str(systemctl),
            "HEALTH_PROBE": str(health),
            "STATE_DIRECTORY": str(tmp_path / "state"),
            "CHOWN_BIN": "/usr/bin/true",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 1
    assert "worker recovery circuit open" in second.stderr


def test_worker_recovery_scales_beyond_two_lanes_and_preserves_ready_quorum(tmp_path):
    desired = json.loads(DESIRED_STATE.read_text())
    desired["worker_fleet"]["units"] = [
        f"voyn-aicc-worker@{index}.service" for index in range(1, 5)
    ]
    desired["worker_fleet"]["minimum_ready_lanes"] = 2
    registry = tmp_path / "desired.json"
    registry.write_text(json.dumps(desired))
    recovered = tmp_path / "recovered"
    calls = tmp_path / "calls"
    health = tmp_path / "health"
    health.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ $1 == voyn-aicc-worker@1.service && ! -f {recovered!s} ]]; then exit 1; fi\n"
        "if [[ $1 == voyn-aicc-worker@2.service ]]; then exit 1; fi\n"
        "exit 0\n"
    )
    health.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >>{calls!s}\n"
        "if [[ $* == *TimeoutStopUSec* ]]; then echo 3660000000; exit 0; fi\n"
        "if [[ $* == *ActiveState* ]]; then echo active; exit 0; fi\n"
        f"if [[ $1 == kill ]]; then touch {recovered!s}; fi\n"
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        [str(RECONCILE)],
        env={
            **os.environ,
            **_desired_env(registry),
            "SYSTEMCTL_BIN": str(systemctl),
            "HEALTH_PROBE": str(health),
            "STATE_DIRECTORY": str(tmp_path / "state"),
            "CHOWN_BIN": "/usr/bin/true",
            "VOYN_WORKER_RECOVERY_POLL_SECONDS": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    drains = [
        line for line in calls.read_text().splitlines() if line.startswith("kill ")
    ]
    assert drains == ["kill --kill-who=main --signal=TERM voyn-aicc-worker@1.service"]


def test_findings_check_opens_a_real_pinned_ssh_connection():
    source = SYNC.read_text()

    check_block = source.split('if [[ "$mode" == check ]]', 1)[1].split("fi", 1)[0]
    assert '"$ssh_bin" -F /dev/null' in check_block
    assert "ConnectTimeout=15" in check_block
    assert "StrictHostKeyChecking=yes" in check_block
    assert " -G " not in check_block


def test_canary_start_records_exact_deployed_sha(tmp_path):
    state_dir = tmp_path / "state"
    deployed_sha = tmp_path / "deployed-sha"
    deployed_sha.write_text("a" * 40 + "\n")
    health = tmp_path / "health"
    health.write_text("#!/usr/bin/env bash\nexit 0\n")
    health.chmod(0o755)
    sha256sum = tmp_path / "sha256sum"
    sha256sum.write_text("#!/usr/bin/env bash\nprintf '%064d  %s\\n' 0 \"$1\"\n")
    sha256sum.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\nif [[ $* == *--property=NRestarts* ]]; then echo 0; fi\n"
    )
    systemctl.chmod(0o755)

    result = subprocess.run(
        [str(CANARY), "start"],
        env={
            **os.environ,
            **_desired_env(),
            "STATE_DIRECTORY": str(state_dir),
            "HEALTH_PROBE": str(health),
            "SHA256SUM_BIN": str(sha256sum),
            "SYSTEMCTL_BIN": str(systemctl),
            "VOYN_MONITOR_DEPLOYED_SHA_FILE": str(deployed_sha),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    state = (state_dir / "state").read_text()
    assert f"deployed_sha={'a' * 40}" in state
