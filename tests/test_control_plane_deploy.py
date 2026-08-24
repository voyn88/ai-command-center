from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_control_plane_units_and_installer_are_versioned_and_fail_closed():
    systemd = ROOT / "deploy" / "systemd"
    expected = {
        "aicc-control-plane-reconciler.service",
        "aicc-control-plane-reconciler.timer",
        "aicc-control-plane-watchdog.service",
        "aicc-control-plane-watchdog.timer",
        "aicc-control-plane-notify.service",
        "aicc-control-plane-notify.timer",
    }
    assert expected <= {path.name for path in systemd.iterdir()}

    service = (systemd / "aicc-control-plane-reconciler.service").read_text()
    timer = (systemd / "aicc-control-plane-reconciler.timer").read_text()
    watchdog = (systemd / "aicc-control-plane-watchdog.service").read_text()
    installer = (ROOT / "ops" / "install_control_plane.sh").read_text()
    bootstrap = (ROOT / "ops" / "bootstrap_control_plane.sh").read_text()

    assert "User=root" in service
    assert "control-plane-reconcile" in service
    assert "Persistent=true" in timer
    assert "/usr/local/libexec/aicc-control-plane-watchdog" in watchdog
    assert "40-character merged SHA" in installer
    assert "canonical /opt/aicc checkout" in installer
    assert "status --porcelain)" in installer
    assert "/var/lib/aicc-control-plane" in installer
    assert "deployed-sha" in installer
    assert "--dry-run" in installer
    assert "control-plane-health" in installer
    assert "control-plane-notification-health" in installer
    assert "control-plane-record-deployment" in installer
    assert "systemd-analyze verify" in installer
    assert "/etc/aicc/desired-state.json" in installer
    assert "/etc/aicc/deployer.env" in installer
    assert installer.index("migrator.env upgrade") < installer.index("daemon-reload")
    assert installer.index("daemon-reload") < installer.index("--dry-run")
    assert installer.index("--dry-run") < installer.index("enable --now")
    assert "control-units" in installer
    assert 'enable --now "${timer_units[@]}"' in installer
    for service_name in ("aicc-backlog-review.service", "aicc-backlog-merge.service"):
        service_text = (systemd / service_name).read_text()
        assert "User=aicc-app" in service_text
        assert "EnvironmentFile=/etc/aicc/app.env" in service_text
        assert "WorkingDirectory=/opt/aicc" in service_text
    assert "git clone --local --no-hardlinks --no-checkout" in bootstrap
    assert "uv sync" in bootstrap and "--frozen" in bootstrap
    assert "root:(400|600)" in bootstrap
    assert "/etc/aicc/migrator.env" in bootstrap
    assert "/etc/aicc/deployer.env" in bootstrap
    assert "status --porcelain)" in bootstrap
    # Not `exec`: the installer runs inside the bootstrap's own
    # transactional guard so a failed install triggers rollback_release
    # instead of replacing the process and losing the trap.
    assert "trap rollback_release ERR" in bootstrap
    installer_call = '"$TARGET/ops/install_control_plane.sh" "$TARGET" "$EXPECTED_SHA" "$TASK_ID"'
    assert installer_call in bootstrap
    assert bootstrap.index("trap rollback_release ERR") < bootstrap.index(installer_call)


def test_watchdog_repairs_once_and_requires_a_fresh_post_repair_probe():
    script = (ROOT / "ops" / "aicc_control_plane_watchdog.sh").read_text()
    first = script.index("control-plane-health")
    repair_timer = script.index("systemctl start aicc-control-plane-reconciler.timer")
    repair_tick = script.index("systemctl start aicc-control-plane-reconciler.service")
    second = script.rindex("control-plane-health")

    assert first < repair_timer < repair_tick < second
    notification_first = script.index("control-plane-notification-health")
    notification_repair = script.index("systemctl start aicc-control-plane-notify")
    notification_second = script.rindex("control-plane-notification-health")
    assert notification_first < notification_repair < notification_second
    assert "set -euo pipefail" in script


def test_composition_waits_for_ci_without_burning_retries_and_splits_deploy_blocker():
    composition = (ROOT / "command_center" / "db" / "cli.py").read_text()

    assert "Action.CI_WAIT: ci_wait_lane" in composition
    assert "awaiting_ci_or_backlog_ingest" in composition
    assert "Action.DEPLOY: unavailable_deploy_lane" in composition
    assert "deployment_capability_not_configured" in composition
