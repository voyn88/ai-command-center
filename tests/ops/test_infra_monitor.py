from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from command_center.ops import infra_monitor
from command_center.ops.infra_monitor import (
    PrometheusSnapshot,
    QueueSnapshot,
    SourceCloneSnapshot,
    evaluate,
    parse_worker_units,
    prometheus_is_ready,
)


def _ready_snapshot(_url: str, **_kwargs: object) -> PrometheusSnapshot:
    """A measured-ready metrics endpoint, for tests about everything else."""
    return PrometheusSnapshot(ready=True, status=200)


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


def test_recent_dead_letter_growth_fails_closed() -> None:
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=0,
            succeeded=10,
            dead=12,
            success_age_seconds=1,
            pending_age_seconds=None,
            recent_dead=2,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=0,
    )

    assert not report.ok
    assert report.failures == ("dead_letter_growth:2>0",)


def test_recent_dead_letter_threshold_is_configurable() -> None:
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=0,
            succeeded=10,
            dead=12,
            success_age_seconds=1,
            pending_age_seconds=None,
            recent_dead=2,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=2,
    )

    assert report.ok


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


# ---------------------------------------------------------------------------
# The metrics endpoint: why it is unready, and how many samples say so.
#
# VOYN-MON-CONTROL-01-QUEUE-PROMETHEUS-UNREADY: control-01's Prometheus binds
# the host's Tailscale address, the distro unit starts before that address
# exists and exits 1, Restart=on-abnormal does not retry an ordinary exit --
# and the recovery timer written for exactly that race was a file in
# deploy/systemd that nothing installed (fixed in the install transaction, and
# pinned by tests/ops/test_agent_principal_isolation.py). The monitor's half:
# `prometheus_unready` was a bare word, so a dead listener and one dropped
# packet on the tailnet were the same finding, and neither carried a status
# code, an errno or a sample count.
# ---------------------------------------------------------------------------


class _ReadinessHandler(BaseHTTPRequestHandler):
    """A readiness endpoint that answers from its server's `replies` queue."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling
        self.server.paths.append(self.path)
        status, body = self.server.replies.pop(0)
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        """Keep the test output clean."""


@contextmanager
def _readiness_server(replies):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadinessHandler)
    server.replies = list(replies)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/-/ready", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_prometheus_probe_reads_the_real_readiness_endpoint() -> None:
    """The happy path over actual HTTP, which nothing exercised before: a
    ready Prometheus answers 200 with a body that says so, on the path the
    deployed units pass (`/-/ready`)."""
    with _readiness_server([(200, "Prometheus Server is Ready.\n")]) as (url, server):
        snapshot = infra_monitor.read_prometheus_snapshot(url, retry_seconds=0)

    assert snapshot == PrometheusSnapshot(ready=True, status=200, attempts=1)
    assert snapshot.reason == ""
    assert server.paths == ["/-/ready"]


def test_prometheus_unready_carries_the_status_it_measured() -> None:
    """A Prometheus that is up but replaying its WAL answers 503. The finding
    says which, over how many samples, and stays ONE finding: the identity is
    the code before the first ':'."""
    with _readiness_server([(503, "Service Unavailable")] * 3) as (url, server):
        snapshot = infra_monitor.read_prometheus_snapshot(url, retry_seconds=0)

    assert snapshot.ready is False
    assert (snapshot.status, snapshot.attempts) == (503, 3)
    assert len(server.paths) == 3

    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=snapshot,
    )

    assert report.failures == ("prometheus_unready:http_503:3_attempts",)
    assert infra_monitor.finding_key(report.failures[0]) == "prometheus_unready"
    assert report.prometheus_ready is False


def test_prometheus_200_from_something_that_is_not_the_probe_is_not_ready() -> None:
    """A proxy error page, or another service on the port, answers 200 with a
    body that never says ready. Fail closed, and say which it was."""
    with _readiness_server([(200, "<html>nope</html>")] * 2) as (url, _server):
        snapshot = infra_monitor.read_prometheus_snapshot(
            url, attempts=2, retry_seconds=0
        )

    assert snapshot.ready is False
    assert snapshot.reason == "body_not_ready:http_200:2_attempts"


def test_prometheus_refused_connection_names_the_errno_not_just_unready() -> None:
    """Nothing listening is the live cause (a Prometheus that exited 1 at
    boot), and it is now distinguishable from every other way to be unready."""
    with _readiness_server([]) as (url, _server):
        pass  # the port is bound, then released: connections are refused.

    snapshot = infra_monitor.read_prometheus_snapshot(url, retry_seconds=0)

    assert snapshot.ready is False
    assert snapshot.status is None
    assert snapshot.error is not None and snapshot.error.startswith(
        ("ConnectionRefusedError", "ConnectionResetError")
    )
    assert snapshot.attempts == 3
    assert snapshot.reason == f"{snapshot.error}:3_attempts"


def test_one_lost_sample_is_not_an_outage() -> None:
    """The flap this probe used to turn into a finding and a task: Prometheus
    is reached across the tailnet, and a single dropped packet (or one 503 on
    the way up) answered for the whole two-minute tick. An unready sample is
    now confirmed before it is believed."""
    with _readiness_server(
        [(503, "Starting Up"), (200, "Prometheus Server is Ready.\n")]
    ) as (url, server):
        snapshot = infra_monitor.read_prometheus_snapshot(url, retry_seconds=0)

    assert snapshot.ready is True
    assert snapshot.attempts == 2
    assert len(server.paths) == 2

    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=snapshot,
    )

    assert report.ok and report.failures == ()


def test_confirmation_is_an_attempt_budget_and_can_be_switched_off() -> None:
    """Fail-closed is unchanged: the loop ends on the budget, never on a
    timer, and a single-sample probe is one flag away."""
    with _readiness_server([(503, "Starting Up")]) as (url, server):
        snapshot = infra_monitor.read_prometheus_snapshot(
            url, attempts=1, retry_seconds=0
        )

    assert snapshot.ready is False
    assert snapshot.attempts == 1
    assert len(server.paths) == 1
    assert (
        infra_monitor.build_parser()
        .parse_args(["--prometheus-url", "http://m/ready"])
        .prometheus_attempts
        == infra_monitor.PROMETHEUS_ATTEMPTS
    )
    with pytest.raises(SystemExit):
        infra_monitor.build_parser().parse_args(
            ["--prometheus-url", "http://m/ready", "--prometheus-attempts", "0"]
        )


def test_an_unprobeable_url_is_a_configuration_fault_and_is_not_retried() -> None:
    """Re-probing a URL this probe will not fetch cannot change the answer,
    so the confirmation loop does not spend its budget (or its sleeps) on it."""
    snapshot = infra_monitor.read_prometheus_snapshot("file:///etc/passwd")

    assert snapshot == PrometheusSnapshot(
        ready=False, error=infra_monitor.PROMETHEUS_UNSUPPORTED_URL, attempts=1
    )
    assert snapshot.reason == "unsupported_url"


def test_a_bare_boolean_verdict_still_evaluates_unchanged() -> None:
    """Callers that only have a verdict (the worker probe's report, every
    pre-existing test) keep the bare failure code and no detail."""
    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=False,
    )

    assert report.failures == ("prometheus_unready",)
    assert report.prometheus_ready is False


def test_main_measures_the_endpoint_with_confirmation_and_reports_the_reason(
    monkeypatch, capsys
) -> None:
    """End to end: the unit's `--prometheus-attempts` reaches the probe, and
    the printed measurement (what --record-findings stores) carries why."""
    seen: dict[str, object] = {}

    def _measure(url, *, attempts=infra_monitor.PROMETHEUS_ATTEMPTS):
        seen.update(url=url, attempts=attempts)
        return PrometheusSnapshot(ready=False, status=503, attempts=attempts)

    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _measure)

    result = infra_monitor.main(
        [
            "--skip-workers",
            "--skip-queue",
            "--minimum-active-workers",
            "0",
            "--prometheus-url",
            "http://metrics.invalid/-/ready",
            "--prometheus-attempts",
            "2",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert seen == {"url": "http://metrics.invalid/-/ready", "attempts": 2}
    assert payload["failures"] == ["prometheus_unready:http_503:2_attempts"]
    assert payload["prometheus_ready"] is False
    assert result == 1


def test_systemd_probes_keep_database_access_off_the_worker_host() -> None:
    worker_unit = Path("deploy/systemd/voyn-infra-monitor.service").read_text()
    queue_unit = Path("deploy/systemd/voyn-queue-monitor.service").read_text()
    refresh_unit = Path(
        "deploy/systemd/voyn-aicc-source-clone-refresh.service"
    ).read_text()
    refresh_timer = Path(
        "deploy/systemd/voyn-aicc-source-clone-refresh.timer"
    ).read_text()

    assert "--skip-queue" in worker_unit
    assert "Environment=AICC_SOURCE_CLONE_REPO=." in worker_unit
    assert "EnvironmentFile=" not in worker_unit
    assert "--skip-workers" in queue_unit
    assert "EnvironmentFile=/home/voynadmin/aicc-preprod/.env" in queue_unit
    assert "command_center.ops.source_clone_refresh" in refresh_unit
    assert "ReadWritePaths=/home/voynadmin/aicc-preprod/repo" in refresh_unit
    assert "ReadWritePaths=/home/voynadmin/Projects/ai-command-center" in refresh_unit
    assert "ReadWritePaths=-/home/voynadmin/Projects/aios" in refresh_unit
    assert "--repo /home/voynadmin/Projects/ai-command-center" in refresh_unit
    assert "Unit=voyn-aicc-source-clone-refresh.service" in refresh_timer


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


def test_main_skip_queue_serializes_null_without_reading_database(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        infra_monitor,
        "discover_worker_units",
        lambda: {"voyn-aicc-worker@1.service": "active"},
    )
    monkeypatch.setattr(
        infra_monitor,
        "read_queue_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("queue must stay unread")),
    )
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)

    result = infra_monitor.main(
        [
            "--minimum-active-workers",
            "1",
            "--skip-queue",
            "--prometheus-url",
            "http://metrics/ready",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["queue"] is None


def test_main_skip_workers_reads_queue_without_inspecting_systemd(
    monkeypatch, capsys
) -> None:
    queue = QueueSnapshot(0, 0, 1, 0, 1.0, None)
    monkeypatch.setattr(
        infra_monitor,
        "discover_worker_units",
        lambda: (_ for _ in ()).throw(AssertionError("workers must stay unread")),
    )
    monkeypatch.setattr(infra_monitor, "read_queue_snapshot", lambda: queue)
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)

    result = infra_monitor.main(
        [
            "--skip-workers",
            "--minimum-active-workers",
            "0",
            "--prometheus-url",
            "http://metrics/ready",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["queue"]["succeeded"] == 1


def _queue(**kw):
    from command_center.ops.infra_monitor import QueueSnapshot

    base = dict(
        ready=3,
        claimed=2,
        succeeded=100,
        dead=0,
        success_age_seconds=200.0,
        pending_age_seconds=60.0,
        recent_dead=0,
    )
    base.update(kw)
    return QueueSnapshot(**base)


def test_executor_quota_refusals_are_their_own_failure_class() -> None:
    from command_center.ops.infra_monitor import evaluate

    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        _queue(recent_dead=2, recent_quota_dead=2),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=5,
    )
    assert "executor_quota_exhausted:2" in report.failures
    assert not any(f.startswith("dead_letter_growth") for f in report.failures)


def test_quota_deaths_do_not_double_count_as_dead_letter_growth() -> None:
    """One cause, one finding: a death already surfaced as
    executor_quota_exhausted is not also dead-letter growth."""
    from command_center.ops.infra_monitor import evaluate

    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        _queue(recent_dead=2, recent_quota_dead=2),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=0,
    )
    assert "executor_quota_exhausted:2" in report.failures
    assert not any(f.startswith("dead_letter_growth") for f in report.failures)


def test_unexplained_deaths_beyond_the_quota_class_still_raise_growth() -> None:
    from command_center.ops.infra_monitor import evaluate

    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        _queue(recent_dead=3, recent_quota_dead=2),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=0,
    )
    assert "dead_letter_growth:1>0" in report.failures


def test_spinning_lanes_with_no_success_in_an_hour_are_a_throughput_stall() -> None:
    from command_center.ops.infra_monitor import evaluate

    # Pending age keeps resetting (items re-claimed), so queue_stalled does
    # not fire -- but nothing succeeded for an hour while work is waiting.
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        _queue(recent_succeeded=0),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )
    assert report.failures == ("throughput_stalled:0_succeeded_in_1h",)
    healthy = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        _queue(recent_succeeded=4),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )
    assert healthy.ok


def test_findings_are_recorded_and_cleared_through_the_definer_functions(
    monkeypatch,
) -> None:
    from command_center.ops import infra_monitor

    calls: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            calls.append((sql.strip(), params))

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            calls.append(("commit", None))

    class _Pool:
        @staticmethod
        def open_pool(cfg):
            calls.append(("open", None))

        @staticmethod
        def connection():
            return _Conn()

        @staticmethod
        def close_pool():
            calls.append(("close", None))

    import sys
    import types

    fake_db = types.ModuleType("command_center.db")
    fake_db.pool = _Pool
    fake_cfg = types.ModuleType("command_center.db.config")
    fake_cfg.load_config = lambda: {}
    monkeypatch.setitem(sys.modules, "command_center.db", fake_db)
    monkeypatch.setitem(sys.modules, "command_center.db.pool", _Pool)
    monkeypatch.setitem(sys.modules, "command_center.db.config", fake_cfg)
    infra_monitor.record_findings("worker-01:infra", ("active_workers:2<4",), {"x": 1})
    recorded = [c for c in calls if "monitor_record_finding" in c[0]]
    # Identity is the failure code, the measurement rides in the detail: the
    # same red probe measured 2<4 then 1<4 is ONE finding, not two tasks.
    assert recorded[0][1][:2] == ("worker-01:infra", "active_workers")
    assert json.loads(recorded[0][1][2]) == {"x": 1, "failure": "active_workers:2<4"}
    assert (
        infra_monitor.finding_key("dead_letter_growth:53>0")
        == infra_monitor.finding_key("dead_letter_growth:54>0")
        == "dead_letter_growth"
    )
    calls.clear()
    infra_monitor.record_findings("worker-01:infra", (), {})
    assert any("monitor_clear_finding" in c[0] for c in calls)


def test_main_exits_non_zero_when_findings_cannot_be_recorded_even_if_healthy(
    monkeypatch, capsys
) -> None:
    """Review of fc167cf7: a healthy measurement whose persistence failed
    exited 0, so a broken finding store passed unnoticed. Fail closed."""
    monkeypatch.setattr(
        infra_monitor,
        "discover_worker_units",
        lambda: {"voyn-aicc-worker@1.service": "active"},
    )
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)
    monkeypatch.setattr(
        infra_monitor,
        "record_findings",
        lambda source, failures, detail: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    result = infra_monitor.main(
        [
            "--minimum-active-workers",
            "1",
            "--skip-queue",
            "--prometheus-url",
            "http://m/ready",
            "--record-findings",
            "worker-01:infra",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["findings_recorded"] is False
    assert "db down" in out["findings_error"]
    assert result == 1


# ---------------------------------------------------------------------------
# The PR review-window reconciler, watched by its effect and not by its unit.
#
# VOYN-W0-AICC-PR-WINDOW-RECONCILER-NOT-DEPLOYED-ON-CONTROL: the labeller had
# never been installed on the control host, the window-gated workflows (CI,
# Acceptance gate, boundary fitness) run on a PR only while it carries a
# review-window label, and so every fleet PR opened with no CI whatsoever --
# 24 of them, plus #907 on 2026-09-09 -- with nothing anywhere saying so. The
# probe below is that missing alarm, and it is deliberately blind to WHY:
# absent, disabled, crashing or out of GitHub quota all read the same.
# ---------------------------------------------------------------------------

_HOUR_AGO = "2026-09-09T20:00:00Z"
_NOW = 1788988800.0  # 2026-09-09T21:20:00Z, an hour and twenty minutes later


def _pr(number: int, labels: tuple[str, ...] = (), created: str = _HOUR_AGO) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/voyn/aicc/pull/{number}",
        "createdAt": created,
        "labels": [{"name": name} for name in labels],
    }


def _fleet(*numbers: int) -> frozenset[str]:
    return frozenset(f"voyn/aicc/pull/{number}" for number in numbers)


def test_an_unlabelled_fleet_pr_past_the_grace_period_is_a_finding() -> None:
    """The exact live shape: an open PR the fleet opened, an hour old, with no
    window label on it, while the reconciler is not running anywhere."""
    snapshot = infra_monitor.PrWindowSnapshot(
        unlabelled=infra_monitor.unlabelled_evidence_prs(
            [_pr(907), _pr(906, ("review-window:waiting",))],
            _fleet(906, 907),
            now=_NOW,
            labels=infra_monitor.window_label_names(),
            grace_seconds=900,
        ),
        open_prs=2,
        evidence_prs=2,
        grace_seconds=900,
    )

    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        pr_window=snapshot,
    )

    assert snapshot.unlabelled == ((907, 4800),)
    assert not report.ok
    (failure,) = report.failures
    assert failure.startswith("pr_window_unlabelled:1_prs_oldest_4800s>900s:")
    assert failure.endswith(":907")
    # One finding for the reconciler however many PRs it has left unlabelled:
    # the count is a measurement and measurements ride in the detail.
    assert infra_monitor.finding_key(failure) == "pr_window_unlabelled"


def test_a_labelled_young_or_unrelated_pr_is_not_a_finding() -> None:
    """Three distinct reasons a missing label proves nothing: the reconciler
    already labelled it (blocked is a label like any other), the PR is younger
    than the grace period so no tick was due yet, and the PR is not the
    fleet's -- somebody else's pull request is somebody else's business."""
    unlabelled = infra_monitor.unlabelled_evidence_prs(
        [
            _pr(901, ("review-window:blocked",)),
            _pr(902, created="2026-09-09T21:15:00Z"),
            _pr(903),
        ],
        _fleet(901, 902),
        now=_NOW,
        labels=infra_monitor.window_label_names(),
        grace_seconds=900,
    )

    assert unlabelled == ()


def test_a_probe_that_could_not_measure_is_its_own_failure_class(monkeypatch) -> None:
    """Fail closed, but say which thing failed: "gh is broken" and "the
    reconciler is not labelling" want different fixes, and one must never be
    filed as the other."""
    monkeypatch.setattr(
        infra_monitor,
        "_open_prs",
        lambda repo, limit: (_ for _ in ()).throw(
            RuntimeError("gh pr list failed: 403")
        ),
    )
    monkeypatch.setattr(infra_monitor, "read_pr_evidence", frozenset)

    snapshot = infra_monitor.read_pr_window_snapshot(
        "/repo", grace_seconds=900, scan_limit=200
    )
    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        pr_window=snapshot,
    )

    assert snapshot.unlabelled == () and "403" in snapshot.error
    assert not report.ok
    assert infra_monitor.finding_key(report.failures[0]) == "pr_window_probe_failed"


def test_the_probe_matches_evidence_to_pull_requests_by_identity() -> None:
    """The `pr` evidence a task recorded and the url `gh pr list` prints are
    two spellings of one pull request. Comparing them raw would make a
    trailing slash or a capitalised host empty the intersection silently --
    a monitor reporting a healthy fleet because it matched nothing at all."""
    assert (
        infra_monitor.pr_identity("https://GitHub.com/Voyn/AICC/pull/907/")
        == infra_monitor.pr_identity("https://github.com/voyn/aicc/pull/907")
        == "voyn/aicc/pull/907"
    )
    assert infra_monitor.pr_identity("https://github.com/voyn/aicc/issues/907") is None
    assert infra_monitor.pr_identity("") is None


def test_the_probe_costs_one_github_request_and_is_off_by_default(monkeypatch) -> None:
    """It shares a GraphQL quota with the review, merge and window ticks
    themselves (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS), so it lists
    once, fetches no per-PR detail, and asks for the OLDEST open PRs -- the
    ones a missing label has hurt longest. A host that passes no repo runs it
    at all."""
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["cwd"] == "/repo"
        return _Completed()

    monkeypatch.setattr(infra_monitor.subprocess, "run", fake_run)
    monkeypatch.setattr(infra_monitor, "read_pr_evidence", frozenset)

    infra_monitor.read_pr_window_snapshot("/repo", grace_seconds=900, scan_limit=200)

    assert len(calls) == 1
    assert calls[0][:5] == ["gh", "pr", "list", "--state", "open"]
    assert "sort:created-asc" in calls[0]
    assert "--limit" in calls[0] and "200" in calls[0]
    assert "statusCheckRollup" not in " ".join(calls[0])
    assert (
        infra_monitor.build_parser()
        .parse_args(["--prometheus-url", "http://m/ready"])
        .pr_window_repo
        == ""
    )
    # The control unit turns it on by environment, because its ExecStart names
    # an absolute home path this public repository cannot restate.
    monkeypatch.setenv("AICC_PR_WINDOW_REPO", "/clone")
    assert (
        infra_monitor.build_parser()
        .parse_args(["--prometheus-url", "http://m/ready"])
        .pr_window_repo
        == "/clone"
    )


def test_main_skips_the_pr_window_probe_unless_a_repo_is_given(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)
    monkeypatch.setattr(
        infra_monitor,
        "read_pr_window_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )

    result = infra_monitor.main(
        [
            "--skip-workers",
            "--skip-queue",
            "--minimum-active-workers",
            "0",
            "--prometheus-url",
            "http://m/ready",
        ]
    )

    assert json.loads(capsys.readouterr().out)["pr_window"] is None
    assert result == 0


def test_main_reports_the_pr_window_probe_when_a_repo_is_given(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)
    monkeypatch.setattr(
        infra_monitor,
        "read_pr_window_snapshot",
        lambda repo, **kwargs: infra_monitor.PrWindowSnapshot(
            unlabelled=((907, 4800),),
            open_prs=25,
            evidence_prs=25,
            grace_seconds=kwargs["grace_seconds"],
        ),
    )

    result = infra_monitor.main(
        [
            "--skip-workers",
            "--skip-queue",
            "--minimum-active-workers",
            "0",
            "--prometheus-url",
            "http://m/ready",
            "--pr-window-repo",
            "/repo",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["pr_window"]["unlabelled"] == [[907, 4800]]
    assert payload["pr_window"]["grace_seconds"] == 900
    assert any(f.startswith("pr_window_unlabelled:") for f in payload["failures"])
    assert result == 1


def test_the_finding_source_can_be_given_by_environment(monkeypatch) -> None:
    """How the control unit turns recording on without touching its own
    ExecStart (leak_guard.sh): the same shape as AICC_PR_WINDOW_REPO."""
    monkeypatch.setenv("AICC_MONITOR_FINDING_SOURCE", "control-01:queue")
    with_env = infra_monitor.build_parser().parse_args(
        ["--prometheus-url", "http://m/ready"]
    )
    overridden = infra_monitor.build_parser().parse_args(
        ["--prometheus-url", "http://m/ready", "--record-findings", "control-01:other"]
    )
    monkeypatch.delenv("AICC_MONITOR_FINDING_SOURCE")
    without_env = infra_monitor.build_parser().parse_args(
        ["--prometheus-url", "http://m/ready"]
    )

    assert with_env.record_findings == "control-01:queue"
    assert overridden.record_findings == "control-01:other"
    # Unset records nothing: a host that does not want a finding store is
    # unaffected, exactly as before.
    assert without_env.record_findings == ""


def test_the_control_probe_watches_the_pr_window_and_records_its_findings() -> None:
    """The probe rides the control-host unit because that is where the
    backlog database (the `pr` evidence) and the fleet's gh identity are; the
    worker probe has neither. It is switched on by environment rather than by
    a flag: that unit's ExecStart names an absolute home path, which a public
    repository cannot restate in an added line (leak_guard.sh). Its finding
    then reaches the planner through that unit's own --record-findings source,
    exactly like every other failure class."""
    queue_unit = Path("deploy/systemd/voyn-queue-monitor.service").read_text()
    worker_unit = Path("deploy/systemd/voyn-infra-monitor.service").read_text()

    assert "Environment=AICC_PR_WINDOW_REPO=." in queue_unit
    # "Reaches the planner" is a source, not a hope: with none, the probe
    # measures, prints, and exits non-zero into a oneshot nobody reads. Set
    # by environment for the same reason the PR-window repo is -- this unit's
    # ExecStart may not be restated in an added line -- and carrying the
    # identity control-01's own findings already use.
    assert "Environment=AICC_MONITOR_FINDING_SOURCE=control-01:queue" in queue_unit
    assert "--record-findings" not in worker_unit
    assert "AICC_MONITOR_FINDING_SOURCE" not in worker_unit
    assert "AICC_PR_WINDOW_REPO" not in worker_unit


# ---------------------------------------------------------------------------
# The source clone that isolated read-only review lanes clone from.
#
# VOYN-W0-AICC-BOUND-SOURCE-CLONE-NEVER-REFRESHED: review lanes used a local
# bound clone whose HEAD could sit behind origin indefinitely, so every
# detached read-only checkout was faithfully cloned from stale source.
# ---------------------------------------------------------------------------


def test_source_clone_staleness_is_its_own_failure_class() -> None:
    snapshot = SourceCloneSnapshot(
        path="/repo",
        local_head="a" * 40,
        remote_head="b" * 40,
        stale=True,
    )

    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        source_clone=snapshot,
    )

    assert report.failures == ("source_clone_stale:aaaaaaaaaaaa!=bbbbbbbbbbbb",)
    assert infra_monitor.finding_key(report.failures[0]) == "source_clone_stale"


def test_source_clone_probe_failure_fails_closed() -> None:
    snapshot = SourceCloneSnapshot(
        path="/repo",
        local_head=None,
        remote_head=None,
        stale=True,
        error="RuntimeError: origin unreachable",
    )

    report = evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        source_clone=snapshot,
    )

    assert report.failures == (
        "source_clone_probe_failed:RuntimeError: origin unreachable",
    )
    assert infra_monitor.finding_key(report.failures[0]) == "source_clone_probe_failed"


def test_source_clone_snapshot_uses_git_and_never_raises(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_git(repo, args, **kwargs):
        calls.append(args)
        if args == ["rev-parse", "HEAD"]:
            return "a" * 40
        if args == ["ls-remote", "origin", "HEAD"]:
            return f"{'b' * 40}\tHEAD"
        raise AssertionError(args)

    monkeypatch.setattr(infra_monitor, "_git_stdout", fake_git)
    stale = infra_monitor.read_source_clone_snapshot("/repo")
    assert (
        stale.stale and stale.local_head == "a" * 40 and stale.remote_head == "b" * 40
    )
    assert calls == [["rev-parse", "HEAD"], ["ls-remote", "origin", "HEAD"]]

    monkeypatch.setattr(
        infra_monitor,
        "_git_stdout",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("git broken")),
    )
    failed = infra_monitor.read_source_clone_snapshot("/repo")
    assert failed.stale and "git broken" in (failed.error or "")


def test_main_reports_the_source_clone_probe_when_a_repo_is_given(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)
    monkeypatch.setattr(
        infra_monitor,
        "read_source_clone_snapshot",
        lambda repo: SourceCloneSnapshot(
            path=repo,
            local_head="a" * 40,
            remote_head="b" * 40,
            stale=True,
        ),
    )

    result = infra_monitor.main(
        [
            "--skip-workers",
            "--skip-queue",
            "--minimum-active-workers",
            "0",
            "--prometheus-url",
            "http://m/ready",
            "--source-clone-repo",
            "/repo",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["source_clone"]["path"] == "/repo"
    assert payload["source_clone"]["stale"] is True
    assert any(f.startswith("source_clone_stale:") for f in payload["failures"])
    assert result == 1


# --- host unit health: crash loops and failed units -------------------------
#
# worker-01, 2026-09-02..14: `ollama.service` restarted every 3 s for twelve
# days (315 891 restarts) and `voyn-canary.service` every 15 s for eighteen
# (70 305) while every monitor stayed green, because each probe only asked
# `is-active` of its own hand-picked units. The host probe reads every service
# unit's NRestarts and active state so a crash loop anywhere on the host is a
# finding, not a symptom the owner reports.


def _show_block(unit: str, restarts: int, active: str) -> str:
    return f"Id={unit}\nNRestarts={restarts}\nActiveState={active}\n"


SHOW_HEALTHY = _show_block("voyn-ollama.service", 0, "active")
SHOW_LOOP = _show_block("ollama.service", 315891, "activating")
SHOW_LAUNCHER_A = _show_block("aicc-agent-launcher@2079-27636-984.service", 0, "failed")
SHOW_LAUNCHER_B = _show_block("aicc-agent-launcher@2080-27700-984.service", 0, "failed")
SHOW_FEW_RESTARTS = _show_block("voyn-crm.service", 2, "active")
SHOW_OUTPUT = f"{SHOW_HEALTHY}\n{SHOW_LOOP}\n{SHOW_LAUNCHER_A}\n{SHOW_LAUNCHER_B}\n{SHOW_FEW_RESTARTS}"

# Real `systemctl list-units --type=service --all` output WITHOUT --plain and
# --no-legend: header, the `●` marker on failed rows, a legend footer.
LIST_UNITS_REAL = """  UNIT                                            LOAD   ACTIVE     SUB     DESCRIPTION
  aicc-agent-launcher@2079-27636-984.service      loaded failed     failed  AICC launcher
● aicc-agent-launcher@2080-27700-984.service      loaded failed     failed  AICC launcher
  ollama.service                                  loaded activating auto-restart Ollama Service
  voyn-crm.service                                loaded active     running VOYN CRM
  voyn-ollama.service                             loaded active     running VOYN Ollama

Legend: LOAD   → Reflects whether the unit definition was properly loaded.

5 loaded units listed.
"""


def test_unit_health_parses_systemctl_show_blocks() -> None:
    units = infra_monitor.parse_unit_show(SHOW_OUTPUT)
    assert units["ollama.service"].restarts == 315891
    assert units["ollama.service"].active_state == "activating"
    assert units["voyn-crm.service"].restarts == 2
    assert len(units) == 5


def test_service_names_survive_headers_markers_and_the_legend() -> None:
    assert infra_monitor.parse_service_names(LIST_UNITS_REAL) == [
        "aicc-agent-launcher@2079-27636-984.service",
        "aicc-agent-launcher@2080-27700-984.service",
        "ollama.service",
        "voyn-crm.service",
        "voyn-ollama.service",
    ]


def test_a_crash_looping_unit_is_a_finding_and_instances_collapse_by_template() -> None:
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(SHOW_OUTPUT), crash_loop_restarts=5
    )
    assert snapshot.crash_loops == (("ollama.service", 315891),)
    # Per-connection template instances are one failed template with a count,
    # not one finding per connection.
    assert snapshot.failed_units == (("aicc-agent-launcher@*.service", 2),)
    report = infra_monitor.evaluate(
        {},
        None,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        unit_health=snapshot,
    )
    assert set(report.failures) == {
        "crash_loop:1:ollama.service=315891",
        "failed_units:1:aicc-agent-launcher@*.service=2",
    }
    assert {infra_monitor.finding_key(f) for f in report.failures} == {
        "crash_loop",
        "failed_units",
    }


def test_crash_looping_template_instances_collapse_to_the_highest_count() -> None:
    show = "\n".join(
        [
            _show_block("aicc-agent-launcher@1-1-984.service", 7, "activating"),
            _show_block("aicc-agent-launcher@2-2-984.service", 12, "activating"),
            _show_block("aicc-agent-launcher@3-3-984.service", 5, "activating"),
        ]
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    assert snapshot.crash_loops == (("aicc-agent-launcher@*.service", 12),)


def test_the_finding_detail_is_capped_not_unbounded() -> None:
    show = "\n".join(
        _show_block(f"svc{i}.service", 9, "activating")
        for i in range(infra_monitor.UNIT_LISTING_CAP + 3)
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    (failure,) = report.failures
    assert failure.startswith(f"crash_loop:{infra_monitor.UNIT_LISTING_CAP + 3}:")
    assert failure.endswith(",+3_more")
    assert failure.count("=9") == infra_monitor.UNIT_LISTING_CAP


def test_the_threshold_boundary_is_inclusive_and_a_few_restarts_are_not_a_loop() -> None:
    show = "\n".join(
        [
            SHOW_HEALTHY,
            SHOW_FEW_RESTARTS,
            _show_block("exactly.service", 5, "active"),
            _show_block("almost.service", 4, "active"),
        ]
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    assert snapshot.crash_loops == (("exactly.service", 5),)
    assert snapshot.failed_units == ()

    quiet = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(f"{SHOW_HEALTHY}\n{SHOW_FEW_RESTARTS}"),
        crash_loop_restarts=5,
    )
    assert quiet.crash_loops == () and quiet.failed_units == ()
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=quiet,
    )
    assert report.ok

    with pytest.raises(ValueError):
        infra_monitor.evaluate_unit_health({}, crash_loop_restarts=0)


def test_unit_health_probe_failure_fails_closed() -> None:
    snapshot = infra_monitor.UnitHealthSnapshot(
        crash_loops=(), failed_units=(), error="RuntimeError: systemctl failed"
    )
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert any(f.startswith("unit_health_probe_failed:") for f in report.failures)


def test_unit_health_snapshot_asks_systemctl_for_every_service_and_never_raises(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def _run(args, **_kwargs):
        calls.append(args)
        if args[1] == "list-units":
            return subprocess.CompletedProcess(args, 0, stdout=LIST_UNITS_REAL, stderr="")
        assert args[1] == "show"
        requested = args[args.index("--") + 1 :]
        # Answer only for what was asked, with only the requested properties,
        # so a wrong property list or a dropped unit is visible here.
        properties = next(a for a in args if a.startswith("--property=")).split("=", 1)[1]
        assert set(properties.split(",")) >= {"Id", "NRestarts", "ActiveState"}
        blocks = {
            "voyn-ollama.service": SHOW_HEALTHY,
            "ollama.service": SHOW_LOOP,
            "aicc-agent-launcher@2079-27636-984.service": SHOW_LAUNCHER_A,
            "aicc-agent-launcher@2080-27700-984.service": SHOW_LAUNCHER_B,
            "voyn-crm.service": SHOW_FEW_RESTARTS,
        }
        return subprocess.CompletedProcess(
            args, 0, stdout="\n".join(blocks[u] for u in requested), stderr=""
        )

    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    snapshot = infra_monitor.read_unit_health_snapshot(crash_loop_restarts=5)
    assert snapshot.error is None
    assert snapshot.crash_loops == (("ollama.service", 315891),)
    assert snapshot.failed_units == (("aicc-agent-launcher@*.service", 2),)
    listing, show = calls
    assert listing[:2] == ["systemctl", "list-units"]
    assert {"--type=service", "--all", "--plain", "--no-legend", "--no-pager"} <= set(listing)
    assert show[:2] == ["systemctl", "show"] and "--" in show
    assert set(show[show.index("--") + 1 :]) == set(
        infra_monitor.parse_service_names(LIST_UNITS_REAL)
    )

    def _boom(args, **_kwargs):
        raise OSError("no systemctl")

    monkeypatch.setattr(infra_monitor.subprocess, "run", _boom)
    failed = infra_monitor.read_unit_health_snapshot(crash_loop_restarts=5)
    assert failed.error is not None and "no systemctl" in failed.error


# --- retired units: a unit this host no longer has is not a failed unit ----
#
# VOYN-MON-WORKER-01-INFRA-FAILED-UNITS. `failed` is a LATCH, not a
# measurement: it records how a unit's last run ended and outlives the
# condition. For a unit this host still has, the next run clears it, so the
# latch being set is a live fault. For a unit whose unit FILE is gone it can
# never be cleared by anything the host does -- there is nothing to start,
# nothing to repair -- and systemd keeps the unit loaded for no other reason
# than to remember it.
#
# The staged rollout retires `voyn-aicc-worker{,-2}.service` and
# `aicc-worker.service` by stopping and *disabling* them, which removes the
# symlink that was their fragment (RETIRED_LEGACY_UNITS in
# ops/aicc_install_transaction.py exists because quiesce must tolerate that
# absence). A stop that reached its timeout latched `failed` on the way out.
# So worker-01 carried three units that were correctly, deliberately gone and
# permanently `failed`, and this probe reported them as broken infrastructure
# every two minutes from the day it was added -- a `failed_units` finding that
# no repair could clear.


def _retired_block(unit: str, restarts: int = 0) -> str:
    """The live shape: no unit file, latched failed, counters frozen."""
    return (
        f"Id={unit}\nLoadState=not-found\n"
        f"NRestarts={restarts}\nActiveState=failed\n"
    )


def _loaded_block(unit: str, restarts: int, active: str) -> str:
    return f"Id={unit}\nLoadState=loaded\nNRestarts={restarts}\nActiveState={active}\n"


def test_a_unit_this_host_no_longer_has_is_not_a_failed_unit() -> None:
    """worker-01's exact table: the three retired legacy workers and nothing
    else wrong. The measurement is healthy, and the corpses are still named."""
    show = "\n".join(
        _retired_block(unit)
        for unit in (
            "aicc-worker.service",
            "voyn-aicc-worker-2.service",
            "voyn-aicc-worker.service",
        )
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    assert snapshot.failed_units == ()
    assert snapshot.crash_loops == ()
    assert snapshot.retired_units == (
        ("aicc-worker.service", 1),
        ("voyn-aicc-worker-2.service", 1),
        ("voyn-aicc-worker.service", 1),
    )

    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert report.failures == ()
    assert report.ok
    # Reclassified, not silenced: the residue rides in the journalled report,
    # as the tick actually prints it.
    journalled = json.loads(json.dumps(infra_monitor._json_report(report)))
    assert journalled["unit_health"]["retired_units"] == [
        ["aicc-worker.service", 1],
        ["voyn-aicc-worker-2.service", 1],
        ["voyn-aicc-worker.service", 1],
    ]
    assert journalled["unit_health"]["failed_units"] == []


def test_a_retired_units_frozen_restart_count_is_not_a_crash_loop_either() -> None:
    """Otherwise clearing `failed_units` only re-raises the same corpses as
    `crash_loop`: a unit with no fragment cannot restart, so its NRestarts is
    residue by exactly the same argument."""
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(_retired_block("voyn-canary.service", 70305)),
        crash_loop_restarts=5,
    )
    assert snapshot.crash_loops == ()
    assert snapshot.failed_units == ()
    # One entry for the unit, counted like failed_units -- the 70 305 frozen
    # restarts are not a number anything can act on any more.
    assert snapshot.retired_units == (("voyn-canary.service", 1),)


def test_a_unit_this_host_does_have_is_still_a_fault_when_it_fails() -> None:
    """The probe must not go blind. A present-but-failing unit is exactly the
    class this probe exists to see, and a unit file that is present but broken
    loads as `error`/`bad-setting` -- never `not-found` -- so it stays a
    finding too."""
    show = "\n".join(
        [
            _retired_block("voyn-aicc-worker-2.service"),
            _loaded_block("voyn-aicc-source-clone-refresh.service", 0, "failed"),
            "Id=voyn-aicc-pgtunnel.service\nLoadState=bad-setting\n"
            "NRestarts=0\nActiveState=failed\n",
            _loaded_block("ollama.service", 315891, "activating"),
        ]
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    assert snapshot.failed_units == (
        ("voyn-aicc-pgtunnel.service", 1),
        ("voyn-aicc-source-clone-refresh.service", 1),
    )
    assert snapshot.crash_loops == (("ollama.service", 315891),)
    assert snapshot.retired_units == (("voyn-aicc-worker-2.service", 1),)

    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert any(f.startswith("failed_units:2:") for f in report.failures)
    assert "voyn-aicc-worker-2.service" not in " ".join(report.failures)


def test_a_dangling_dependency_reference_is_not_a_unit_at_all() -> None:
    """`Wants=prometheus.service` on a host without Prometheus loads as
    not-found and inactive. systemd holds it only because something names it,
    there is no latch, and it has never been a finding -- so it is not
    reported as residue either."""
    show = (
        "Id=prometheus.service\nLoadState=not-found\n"
        "NRestarts=0\nActiveState=inactive\n"
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show), crash_loop_restarts=5
    )
    assert snapshot.failed_units == ()
    assert snapshot.crash_loops == ()
    assert snapshot.retired_units == ()


def test_the_probe_asks_systemd_for_load_state_rather_than_assuming_it(
    monkeypatch,
) -> None:
    """The lesson of 7570626c: a rule the code states but never puts in force
    is a rule that does nothing. Without LoadState in the `show` argv every
    unit's `load_state` is empty, no unit is ever residue, and the corpses are
    counted as faults again -- with every other assertion still passing."""
    captured: list[list[str]] = []
    retired = _retired_block("voyn-aicc-worker-2.service")

    def _run(args, **_kwargs):
        captured.append(args)
        if args[1] == "list-units":
            return subprocess.CompletedProcess(
                args, 0, stdout="voyn-aicc-worker-2.service not-found failed failed\n",
                stderr="",
            )
        properties = next(
            a for a in args if a.startswith("--property=")
        ).split("=", 1)[1]
        assert "LoadState" in properties.split(",")
        return subprocess.CompletedProcess(args, 0, stdout=retired, stderr="")

    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    snapshot = infra_monitor.read_unit_health_snapshot(crash_loop_restarts=5)
    assert snapshot.error is None
    assert snapshot.failed_units == ()
    assert snapshot.retired_units == (("voyn-aicc-worker-2.service", 1),)
    show = captured[1]
    assert "LoadState" in next(
        a for a in show if a.startswith("--property=")
    ).split("=", 1)[1].split(",")


def test_the_retirement_that_produces_the_corpses_clears_the_latch() -> None:
    """The other half of the fix, cross-checked here so the two cannot drift:
    the rollout that removes these units' fragment must un-latch them, or the
    fleet keeps producing residue for the probe to classify."""
    rollout = Path("ops/aicc_staged_worker_rollout.py").read_text(encoding="utf-8")
    retire = rollout.split("def retire_legacy_units", 1)[1].split("\ndef ", 1)[0]
    for action in ("stop", "disable", "reset-failed"):
        assert f'systemd.run("{action}", unit, check=False)' in retire
    verify = rollout.split("def verify_legacy_units_retired", 1)[1].split("\ndef ", 1)[0]
    assert 'active == "failed"' in verify


def test_a_unit_that_show_silently_drops_is_a_failed_measurement(monkeypatch) -> None:
    def _run(args, **_kwargs):
        if args[1] == "list-units":
            return subprocess.CompletedProcess(args, 0, stdout=LIST_UNITS_REAL, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=SHOW_HEALTHY, stderr="")

    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    snapshot = infra_monitor.read_unit_health_snapshot(crash_loop_restarts=5)
    assert snapshot.error is not None and "1 of 5" in snapshot.error
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert any(f.startswith("unit_health_probe_failed:") for f in report.failures)


# --- deploy lag: green main versus what production actually runs ----------
#
# worker-01, 2026-09-13 14:48 .. 09-14 18:49: six merged PRs sat behind a
# refused promotion for 28 hours while test and preprod advanced. Nothing
# compared the branch head with the SHA production reports, so "green main"
# said nothing about what customers were running. The clock is the oldest
# undeployed commit, not the head: a busy branch must not hide its lag by
# merging often.


def _lag(**overrides):
    base = {
        "repo": "voyn88/voyn-logistics-crm", "branch": "main",
        "branch_head": "a" * 40, "deployed_sha": "b" * 40,
        "undeployed_commits": 6, "lag_seconds": 100_000.0, "grace_seconds": 2700.0,
    }
    base.update(overrides)
    return infra_monitor.DeployLagSnapshot(**base)


def test_deploy_lag_is_a_finding_once_the_oldest_undeployed_commit_passes_grace() -> None:
    snapshot = _lag()
    assert snapshot.lagging is True
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, deploy_lag=snapshot,
    )
    assert set(report.failures) == {
        "deploy_lag:voyn88/voyn-logistics-crm:bbbbbbbb!=aaaaaaaa_6_commits_100000s>2700s"
    }
    assert {infra_monitor.finding_key(f) for f in report.failures} == {"deploy_lag"}


def test_a_young_backlog_or_a_deployed_head_is_not_deploy_lag() -> None:
    assert _lag(lag_seconds=60.0).lagging is False
    assert _lag(deployed_sha="a" * 40, undeployed_commits=0, lag_seconds=0.0).lagging is False
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True,
        deploy_lag=_lag(deployed_sha="a" * 40, undeployed_commits=0, lag_seconds=0.0),
    )
    assert report.ok


def test_deploy_lag_probe_failure_fails_closed() -> None:
    snapshot = _lag(
        branch_head=None, deployed_sha=None, undeployed_commits=None, lag_seconds=None,
        error="RuntimeError: gh api failed",
    )
    assert snapshot.lagging is False
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, deploy_lag=snapshot,
    )
    assert any(f.startswith("deploy_lag_probe_failed:") for f in report.failures)


def _gh_stub(responses: dict[str, str], calls: list[list[str]]):
    def _run(args, **_kwargs):
        calls.append(args)
        assert args[:2] == ["gh", "api"]
        return subprocess.CompletedProcess(args, 0, stdout=responses[args[2]], stderr="")

    return _run


HEAD_JSON = '{"sha": "' + "a" * 40 + '", "commit": {"committer": {"date": "2026-09-14T12:00:00Z"}}}'

#: 2026-09-14T13:00:00Z, the "now" every dated deploy-lag test freezes.
NOW = 1789390800.0


def _commit(sha: str, date: str, *parents: str) -> dict:
    """One commit as the compare endpoint returns it: GitHub carries
    `parents` on every entry, first parent first."""
    return {
        "sha": sha,
        "commit": {"committer": {"date": date}},
        "parents": [{"sha": parent} for parent in parents],
    }


def test_deploy_lag_snapshot_dates_the_oldest_undeployed_commit(monkeypatch) -> None:
    calls: list[list[str]] = []
    # A linear range: both commits landed on main directly, so each commit's
    # own date is also the date main gained it.
    compare = json.dumps({
        "status": "ahead",
        "total_commits": 2,
        "commits": [
            _commit("c1", "2026-09-13T16:00:00Z", "b" * 40),
            _commit("a" * 40, "2026-09-14T12:00:00Z", "c1"),
        ],
    })
    monkeypatch.setattr(
        infra_monitor.subprocess, "run",
        _gh_stub({
            "repos/voyn88/voyn-logistics-crm/commits/main": HEAD_JSON,
            "repos/voyn88/voyn-logistics-crm/compare/" + "b" * 40 + "..." + "a" * 40: compare,
        }, calls),
    )
    monkeypatch.setattr(infra_monitor, "_fetch_json", lambda url, timeout=5: {"release_sha": "b" * 40})
    # now = 2026-09-14T13:00:00Z: the head is 1 h old, the oldest undeployed commit 21 h.
    monkeypatch.setattr(infra_monitor.time, "time", lambda: NOW)
    snapshot = infra_monitor.read_deploy_lag_snapshot(
        "voyn88/voyn-logistics-crm", "http://127.0.0.1:8089/version",
        branch="main", grace_seconds=2700.0,
    )
    assert snapshot.error is None
    assert snapshot.branch_head == "a" * 40 and snapshot.deployed_sha == "b" * 40
    assert snapshot.undeployed_commits == 2
    assert abs(snapshot.lag_seconds - 21 * 3600) < 1
    assert snapshot.lagging is True
    assert len(calls) == 2


def test_deploy_lag_snapshot_makes_one_request_when_production_runs_the_head(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        infra_monitor.subprocess, "run",
        _gh_stub({"repos/r/commits/main": HEAD_JSON}, calls),
    )
    monkeypatch.setattr(infra_monitor, "_fetch_json", lambda url, timeout=5: {"release_sha": "a" * 40})
    snapshot = infra_monitor.read_deploy_lag_snapshot(
        "r", "http://127.0.0.1:8089/version", branch="main", grace_seconds=2700.0
    )
    assert snapshot.error is None and snapshot.lagging is False
    assert snapshot.undeployed_commits == 0
    assert len(calls) == 1


def test_production_ahead_of_the_branch_is_not_deploy_lag(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        infra_monitor.subprocess, "run",
        _gh_stub({
            "repos/r/commits/main": HEAD_JSON,
            "repos/r/compare/" + "b" * 40 + "..." + "a" * 40: '{"status": "behind", "commits": []}',
        }, calls),
    )
    monkeypatch.setattr(infra_monitor, "_fetch_json", lambda url, timeout=5: {"release_sha": "b" * 40})
    snapshot = infra_monitor.read_deploy_lag_snapshot(
        "r", "http://127.0.0.1:8089/version", branch="main", grace_seconds=2700.0
    )
    assert snapshot.error is None and snapshot.lagging is False


@pytest.mark.parametrize(
    "breakage",
    ["gh_nonzero", "gh_raises", "version_raises", "no_release_sha", "compare_no_dates"],
)
def test_deploy_lag_snapshot_never_raises(monkeypatch, breakage) -> None:
    def _run(args, **_kwargs):
        if breakage == "gh_nonzero":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 404")
        if breakage == "gh_raises":
            raise OSError("no gh")
        if args[2].startswith("repos/r/compare/"):
            return subprocess.CompletedProcess(
                args, 0, stdout='{"status": "ahead", "commits": [{"sha": "x"}]}', stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout=HEAD_JSON, stderr="")

    def _version(url, timeout=5):
        if breakage == "version_raises":
            raise TimeoutError("version endpoint timed out")
        if breakage == "no_release_sha":
            return {"version": "0.2.0"}
        return {"release_sha": "b" * 40}

    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    monkeypatch.setattr(infra_monitor, "_fetch_json", _version)
    snapshot = infra_monitor.read_deploy_lag_snapshot(
        "r", "http://127.0.0.1:8089/version", branch="main", grace_seconds=2700.0
    )
    assert snapshot.error is not None
    assert snapshot.lagging is False
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, deploy_lag=snapshot,
    )
    assert any(f.startswith("deploy_lag_probe_failed:") for f in report.failures)


# VOYN-MON-WORKER-01-INFRA-DEPLOY-LAG-5ABD205763 (monitor_finding #38959):
# the probe dated the lag from the oldest commit OBJECT in the undeployed
# range, so a feature branch written days earlier and merged a minute ago
# opened `deploy_lag` the instant it merged -- the grace period for CI, the
# merge queue and the promotion tick was already spent before the code was
# even mergeable -- and the finding stayed open until production matched the
# head exactly, while the promotion pipeline was doing its job. The clock is
# when the BRANCH gained the commit: its first-parent chain.


def _deploy_lag_probe(monkeypatch, compare: str):
    """The probe against one compare payload, with the unit's 45-minute
    grace period and `NOW` frozen as the measurement time."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        infra_monitor.subprocess, "run",
        _gh_stub({
            "repos/r/commits/main": HEAD_JSON,
            "repos/r/compare/" + "b" * 40 + "..." + "a" * 40: compare,
        }, calls),
    )
    monkeypatch.setattr(infra_monitor, "_fetch_json", lambda url, timeout=5: {"release_sha": "b" * 40})
    monkeypatch.setattr(infra_monitor.time, "time", lambda: NOW)
    return infra_monitor.read_deploy_lag_snapshot(
        "r", "http://127.0.0.1:8089/version", branch="main", grace_seconds=2700.0
    )


def _merged_branch_compare(merge_date: str) -> str:
    """Five-day-old feature-branch commits that reached main through a merge
    commit: `merge_date` is when main gained all three."""
    return json.dumps({
        "status": "ahead",
        "total_commits": 3,
        "commits": [
            _commit("f1", "2026-09-09T08:00:00Z", "b" * 40),
            _commit("f2", "2026-09-09T09:00:00Z", "f1"),
            _commit("a" * 40, merge_date, "b" * 40, "f2"),
        ],
    })


def test_an_old_branch_merged_inside_the_grace_period_is_not_deploy_lag(monkeypatch) -> None:
    # Merged at 12:59, measured at 13:00: one minute of promotion budget used
    # out of forty-five, however old the commits the merge carried are.
    snapshot = _deploy_lag_probe(monkeypatch, _merged_branch_compare("2026-09-14T12:59:00Z"))
    assert snapshot.error is None
    assert snapshot.undeployed_commits == 3
    assert abs(snapshot.lag_seconds - 60.0) < 1
    assert snapshot.lagging is False
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, deploy_lag=snapshot,
    )
    assert report.ok


def test_deploy_lag_of_a_merged_branch_is_measured_from_the_merge(monkeypatch) -> None:
    # The same branch, merged 26 h ago and still not deployed: a real stall,
    # reported as 26 h -- not as the five days since the commits were written.
    snapshot = _deploy_lag_probe(monkeypatch, _merged_branch_compare("2026-09-13T11:00:00Z"))
    assert snapshot.error is None
    assert abs(snapshot.lag_seconds - 26 * 3600) < 1
    assert snapshot.lagging is True
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, deploy_lag=snapshot,
    )
    assert {infra_monitor.finding_key(f) for f in report.failures} == {"deploy_lag"}
    assert "_3_commits_93600s>2700s" in report.failures[0]


def test_a_compare_without_parents_dates_the_commit_objects_not_the_head(monkeypatch) -> None:
    # A payload the first-parent walk cannot follow must fall back to the
    # commit objects. Dating a partial walk would date the lag from the head,
    # the one number every new merge resets.
    compare = json.dumps({
        "status": "ahead",
        "commits": [
            {"sha": "c1", "commit": {"committer": {"date": "2026-09-13T16:00:00Z"}}},
            {"sha": "a" * 40, "commit": {"committer": {"date": "2026-09-14T12:00:00Z"}}},
        ],
    })
    snapshot = _deploy_lag_probe(monkeypatch, compare)
    assert snapshot.error is None
    assert snapshot.undeployed_commits == 2
    assert abs(snapshot.lag_seconds - 21 * 3600) < 1
    assert snapshot.lagging is True


def test_a_truncated_compare_page_counts_the_whole_range(monkeypatch) -> None:
    # GitHub returns at most 250 commits, oldest first, so a long stall's
    # payload has no head to walk from and `len(commits)` understates the
    # backlog. The count comes from `total_commits`; the clock from the
    # oldest commit on the page, which is the oldest undeployed one.
    compare = json.dumps({
        "status": "ahead",
        "total_commits": 612,
        "commits": [
            _commit("p1", "2026-09-01T00:00:00Z", "b" * 40),
            _commit("p2", "2026-09-02T00:00:00Z", "p1"),
        ],
    })
    snapshot = _deploy_lag_probe(monkeypatch, compare)
    assert snapshot.error is None
    assert snapshot.undeployed_commits == 612
    assert abs(snapshot.lag_seconds - 1_170_000.0) < 1  # 2026-09-01 -> 09-14T13:00
    assert snapshot.lagging is True


def test_a_first_parent_cycle_falls_back_instead_of_looping(monkeypatch) -> None:
    # c1 claims the head as its first parent. GitHub cannot return this, but
    # the walk must terminate on it rather than spin inside a monitor tick.
    compare = json.dumps({
        "status": "ahead",
        "total_commits": 2,
        "commits": [
            _commit("c1", "2026-09-13T16:00:00Z", "a" * 40),
            _commit("a" * 40, "2026-09-14T12:00:00Z", "c1"),
        ],
    })
    snapshot = _deploy_lag_probe(monkeypatch, compare)
    assert snapshot.error is None
    assert abs(snapshot.lag_seconds - 21 * 3600) < 1


def test_a_half_configured_deploy_lag_probe_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        infra_monitor.parse_args(
            ["--prometheus-url", "http://m/ready", "--deploy-lag-repo", "voyn88/x"]
        )
    with pytest.raises(SystemExit):
        infra_monitor.parse_args(["--prometheus-url", "http://m/ready", "--crash-loop-restarts", "0"])


def test_main_reports_the_host_probes_when_enabled(monkeypatch, capsys) -> None:
    monkeypatch.setattr(infra_monitor, "read_prometheus_snapshot", _ready_snapshot)
    monkeypatch.setattr(
        infra_monitor, "read_unit_health_snapshot",
        lambda crash_loop_restarts: infra_monitor.UnitHealthSnapshot(
            crash_loops=(("ollama.service", 315891),), failed_units=()
        ),
    )
    monkeypatch.setattr(
        infra_monitor, "read_deploy_lag_snapshot",
        lambda repo, url, branch, grace_seconds: _lag(repo=repo, branch=branch, grace_seconds=grace_seconds),
    )
    result = infra_monitor.main(
        [
            "--skip-workers", "--skip-queue", "--minimum-active-workers", "0",
            "--prometheus-url", "http://m/ready",
            "--unit-health",
            "--deploy-lag-repo", "voyn88/voyn-logistics-crm",
            "--deploy-lag-version-url", "http://127.0.0.1:8089/version",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["unit_health"]["crash_loops"] == [["ollama.service", 315891]]
    assert payload["deploy_lag"]["lagging"] is True
    assert payload["deploy_lag"]["undeployed_commits"] == 6
    assert any(f.startswith("crash_loop:") for f in payload["failures"])
    assert any(f.startswith("deploy_lag:") for f in payload["failures"])
    assert result == 1
