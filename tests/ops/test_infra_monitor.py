from __future__ import annotations

from pathlib import Path

from command_center.ops.infra_monitor import (
    QueueSnapshot,
    evaluate,
    parse_worker_units,
    prometheus_is_ready,
)


def test_worker_discovery_uses_current_templated_lanes_only() -> None:
    output = """\
voyn-aicc-worker@1.service loaded active running AICC queue worker
voyn-aicc-worker@2.service loaded failed failed AICC queue worker
voyn-aicc-worker.service loaded inactive dead Legacy worker
voyn-claude.service loaded inactive dead Retired worker
voyn-aicc-worker@4.service loaded active running AICC queue worker
"""

    assert parse_worker_units(output) == {
        "voyn-aicc-worker@1.service": "active",
        "voyn-aicc-worker@2.service": "failed",
        "voyn-aicc-worker@4.service": "active",
    }


def test_idle_queue_is_healthy_even_when_last_success_is_old() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(
            ready=0,
            claimed=0,
            succeeded=10,
            dead=2,
            success_age_seconds=9999,
            pending_age_seconds=None,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok
    assert report.failures == ()


def test_pending_queue_without_recent_progress_fails_closed() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(
            ready=3,
            claimed=0,
            succeeded=10,
            dead=2,
            success_age_seconds=901,
            pending_age_seconds=901,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_new_pending_work_does_not_turn_an_idle_queue_red() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(
            ready=1,
            claimed=0,
            succeeded=10,
            dead=2,
            success_age_seconds=9999,
            pending_age_seconds=10,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok


def test_claimed_queue_without_recent_success_fails_closed() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(
            ready=0,
            claimed=2,
            succeeded=10,
            dead=2,
            success_age_seconds=901,
            pending_age_seconds=901,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_unrelated_success_does_not_hide_a_zombie_claim() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(
            ready=0,
            claimed=1,
            succeeded=11,
            dead=2,
            success_age_seconds=5,
            pending_age_seconds=901,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_inactive_lane_and_prometheus_failure_are_reported() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "failed",
        },
        QueueSnapshot(
            ready=0,
            claimed=0,
            succeeded=0,
            dead=0,
            success_age_seconds=None,
            pending_age_seconds=None,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=False,
    )

    assert not report.ok
    assert report.failures == ("active_workers:1<2", "prometheus_unready")


def test_prometheus_probe_rejects_non_http_urls() -> None:
    assert not prometheus_is_ready("file:///etc/passwd")
    assert not prometheus_is_ready("http://user@example.invalid/-/ready")


def test_systemd_probes_keep_database_access_off_the_worker_host() -> None:
    worker_unit = Path("deploy/systemd/voyn-infra-monitor.service").read_text()
    queue_unit = Path("deploy/systemd/voyn-queue-monitor.service").read_text()

    assert "--skip-queue" in worker_unit
    assert "EnvironmentFile=" not in worker_unit
    assert "--skip-workers" in queue_unit
    assert "EnvironmentFile=/home/voynadmin/aicc-preprod/.env" in queue_unit


def test_evaluate_can_skip_queue_without_hiding_worker_failures() -> None:
    healthy = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        None,
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )
    failed = evaluate(
        {"voyn-aicc-worker@1.service": "failed"},
        None,
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert healthy.ok
    assert healthy.queue is None
    assert failed.failures == ("active_workers:0<1",)
