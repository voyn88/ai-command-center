import json
from pathlib import Path

from command_center import observability
from command_center.observability import (
    CONTENT_TYPE,
    WorkerTelemetry,
    render_control_metrics,
)


ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self):
        self.executed = []
        self.results = iter([
            [("execution", "ready", 2), ("execution", "claimed", 1)],
            [("execution", 42)],
            [("TASK-1", "repo", "aicc_w_1", "wat-1", 12, 288)],
        ])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql):
        self.executed.append(sql)
        self.rows = next(self.results)

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.cur = Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self.cur


def test_control_metrics_expose_queue_worker_task_and_lease_without_secrets():
    body = render_control_metrics(Connection)
    assert 'aicc_queue_lag_seconds{queue="execution"} 42.0' in body
    assert 'task="TASK-1"' in body and 'worker="aicc_w_1"' in body
    assert "claim_token" not in body


def test_control_metrics_measure_a_new_lease_before_its_first_heartbeat():
    connection = Connection()
    body = render_control_metrics(lambda: connection)
    # A claim starts with heartbeat_at=NULL. The SQL must fall back to the
    # attempt creation timestamp rather than poisoning the entire scrape.
    assert any(
        "COALESCE(a.heartbeat_at, a.created_at)" in sql
        for sql in connection.cur.executed
    )
    assert "aicc_metrics_scrape_error 0" in body


def test_worker_metrics_include_sha_and_resource_telemetry():
    telemetry = WorkerTelemetry(
        worker="w1", task="T1", sha="abc", attempt="a1", cost_per_hour=3.6
    )
    telemetry.start(task="T1", sha="abc", attempt="a1")
    telemetry.finish(succeeded=True, sha="def")
    body = telemetry.render()
    assert 'sha="def"' in body
    assert "aicc_worker_cpu_seconds_total" in body
    assert "aicc_worker_resident_memory_bytes" in body
    assert "aicc_worker_task_runtime_seconds_total" in body
    assert "aicc_worker_estimated_cost_total" in body
    assert 'aicc_worker_tasks_total{outcome="succeeded",worker="w1"} 1' in body


def test_control_metrics_stay_parseable_when_database_is_down():
    def unavailable():
        raise RuntimeError("database unavailable")

    body = render_control_metrics(unavailable)
    assert "aicc_control_up 1" in body
    assert "aicc_metrics_scrape_error 1" in body


def test_worker_loss_alert_and_dashboard_pin_acceptance_signals():
    alerts = (ROOT / "deploy/observability/aicc-alerts.yml").read_text()
    assert 'alert: AICCWorkerLost' in alerts
    assert 'up{job="aicc-worker"} == 0' in alerts

    dashboard = json.loads(
        (ROOT / "deploy/observability/grafana-dashboard.json").read_text()
    )
    expressions = " ".join(
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    )
    for metric in (
        "aicc_control_up",
        "aicc_worker_up",
        "aicc_worker_active",
        "aicc_queue_lag_seconds",
        "aicc_lease_age_seconds",
        "aicc_worker_cpu_seconds_total",
        "aicc_worker_task_runtime_seconds_total",
        "aicc_worker_estimated_cost_total",
        "aicc_worker_resident_memory_bytes",
    ):
        assert metric in expressions


def test_control_metrics_endpoint_is_served_and_degrades_without_a_database():
    """The scrape surface must be reachable on the real app, and a control host
    whose database is down must still return a parseable body (Prometheus drops
    the whole target on a 500, hiding the very incident being diagnosed)."""
    from fastapi.testclient import TestClient

    from command_center.webapi.app import create_app

    response = TestClient(create_app()).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert "aicc_control_up 1" in response.text
    # No PostgreSQL is configured in this test process.
    assert "aicc_metrics_scrape_error 1" in response.text


def test_resident_memory_is_reported_in_bytes_on_every_platform(monkeypatch):
    """`ru_maxrss` is kilobytes on Linux and bytes on macOS; a single constant
    would misreport one of them by 1024x."""

    class Usage:
        ru_maxrss = 2048

    monkeypatch.setattr(observability.sys, "platform", "linux")
    assert observability._resident_memory_bytes(Usage) == 2048 * 1024
    monkeypatch.setattr(observability.sys, "platform", "darwin")
    assert observability._resident_memory_bytes(Usage) == 2048
