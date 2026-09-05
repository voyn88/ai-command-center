from __future__ import annotations

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
        QueueSnapshot(ready=0, claimed=0, succeeded=10, dead=2, success_age_seconds=9999),
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
        QueueSnapshot(ready=3, claimed=0, succeeded=10, dead=2, success_age_seconds=901),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_claimed_queue_without_recent_success_fails_closed() -> None:
    report = evaluate(
        {
            "voyn-aicc-worker@1.service": "active",
            "voyn-aicc-worker@2.service": "active",
            "voyn-aicc-worker@3.service": "active",
            "voyn-aicc-worker@4.service": "active",
        },
        QueueSnapshot(ready=0, claimed=2, succeeded=10, dead=2, success_age_seconds=901),
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
        QueueSnapshot(ready=0, claimed=0, succeeded=0, dead=0, success_age_seconds=None),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=False,
    )

    assert not report.ok
    assert report.failures == ("active_workers:1<2", "prometheus_unready")


def test_prometheus_probe_rejects_non_http_urls() -> None:
    assert not prometheus_is_ready("file:///etc/passwd")
    assert not prometheus_is_ready("http://user:secret@example.invalid/-/ready")
