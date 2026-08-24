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
    assert "enable --now aicc-control-plane-reconciler.timer" in installer
    for timer in (
        "aicc-backlog-planner.timer",
        "aicc-backlog-review.timer",
        "aicc-backlog-merge.timer",
        "aicc-queue-reaper.timer",
    ):
        assert timer in installer
    for service_name in ("aicc-backlog-review.service", "aicc-backlog-merge.service"):
        service_text = (systemd / service_name).read_text()
        assert "User=aicc-app" in service_text
        assert "EnvironmentFile=/etc/aicc/app.env" in service_text
        assert "WorkingDirectory=/opt/aicc" in service_text
    assert "git clone --local --no-hardlinks --no-checkout" in bootstrap
    assert "uv sync" in bootstrap and "--frozen" in bootstrap
    assert "root:(400|600)" in bootstrap
    assert "status --porcelain)" in bootstrap
    assert "exec \"$TARGET/ops/install_control_plane.sh\"" in bootstrap


def test_watchdog_repairs_once_and_requires_a_fresh_post_repair_probe():
    script = (ROOT / "ops" / "aicc_control_plane_watchdog.sh").read_text()
    first = script.index("control-plane-health")
    repair_timer = script.index("systemctl start aicc-control-plane-reconciler.timer")
    repair_tick = script.index("systemctl start aicc-control-plane-reconciler.service")
    second = script.rindex("control-plane-health")

    assert first < repair_timer < repair_tick < second
    assert "set -euo pipefail" in script


def test_composition_waits_for_ci_without_burning_retries_and_splits_deploy_blocker():
    composition = (ROOT / "command_center" / "db" / "cli.py").read_text()

    assert "Action.CI_WAIT: ci_wait_lane" in composition
    assert "awaiting_ci_or_backlog_ingest" in composition
    assert "Action.DEPLOY: unavailable_deploy_lane" in composition
    assert "deployment_capability_not_configured" in composition
