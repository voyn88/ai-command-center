from __future__ import annotations

import json
from pathlib import Path

from command_center.ops import infra_monitor
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
            pending_unattended=3,
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
    """A ready, due, unclaimed item ten seconds old is inside the stall
    window, so an old last-success does not make it red.

    ``pending_unattended`` is set to match ``pending_age_seconds``: the two
    come from one filter in ``_QUEUE_SNAPSHOT_SQL`` and the count is 0
    exactly when the age is ``None``. Left at its default of 0 beside a
    non-``None`` age this snapshot could not occur, and the assertion passed
    through the unattended gate without ever reaching the 10s < 900s
    comparison it is here to pin.
    """
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
            pending_unattended=1,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok


def test_claimed_queue_without_recent_success_fails_closed() -> None:
    """Claims whose leases have LAPSED (so the snapshot counts them
    unattended) and that nothing has recovered inside the stall window."""
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
            pending_unattended=2,
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
            pending_unattended=1,
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


def test_the_queue_probe_records_under_the_source_the_live_findings_carry() -> None:
    """`monitor_finding` rows are keyed by (source, failure code), and the
    planner mints ONE task per open finding from that pair. The live
    control-01 probe records under `control-01:queue` (monitor_finding #481),
    so the unit may not drift to another spelling: a second source would open
    a second, unlinked finding for the same measurement."""
    queue_unit = Path("deploy/systemd/voyn-queue-monitor.service").read_text()
    exec_start = next(
        line for line in queue_unit.splitlines() if line.startswith("ExecStart=")
    )

    assert "--record-findings control-01:queue" in exec_start
    # The stall window is the UNATTENDED clock and must stay well under the
    # claim ceiling; conflating the two is what this unit was red for. The
    # ceiling itself is not spelled here -- the default IS the policy, and
    # `test_the_claim_ceiling_clears_one_legitimate_attempt` pins it.
    assert "--max-stalled-seconds 900" in exec_start
    assert "--max-claim-seconds" not in exec_start


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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)

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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)

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

    base = dict(ready=3, claimed=2, succeeded=100, dead=0, success_age_seconds=200.0,
                pending_age_seconds=60.0, recent_dead=0, pending_unattended=5)
    base.update(kw)
    return QueueSnapshot(**base)


def test_executor_quota_refusals_are_their_own_failure_class() -> None:
    from command_center.ops.infra_monitor import evaluate

    report = evaluate({"voyn-aicc-worker@1.service": "active"}, _queue(recent_dead=2, recent_quota_dead=2),
                      minimum_active_workers=1, max_stalled_seconds=900, prometheus_ready=True,
                      max_recent_dead=5)
    assert "executor_quota_exhausted:2" in report.failures
    assert not any(f.startswith("dead_letter_growth") for f in report.failures)


def test_spinning_lanes_with_no_success_in_an_hour_are_a_throughput_stall() -> None:
    from command_center.ops.infra_monitor import evaluate

    # Pending age keeps resetting (items re-claimed), so queue_stalled does
    # not fire -- but nothing succeeded for an hour while work is waiting.
    report = evaluate({"voyn-aicc-worker@1.service": "active"}, _queue(recent_succeeded=0),
                      minimum_active_workers=1, max_stalled_seconds=900, prometheus_ready=True)
    assert report.failures == ("throughput_stalled:0_succeeded_in_1h",)
    healthy = evaluate({"voyn-aicc-worker@1.service": "active"}, _queue(recent_succeeded=4),
                       minimum_active_workers=1, max_stalled_seconds=900, prometheus_ready=True)
    assert healthy.ok


def test_findings_are_recorded_and_cleared_through_the_definer_functions(monkeypatch) -> None:
    from command_center.ops import infra_monitor

    calls: list[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): calls.append((sql.strip(), params))

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): calls.append(("commit", None))

    class _Pool:
        @staticmethod
        def open_pool(cfg): calls.append(("open", None))
        @staticmethod
        def connection(): return _Conn()
        @staticmethod
        def close_pool(): calls.append(("close", None))

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
    assert infra_monitor.finding_key("dead_letter_growth:53>0") == infra_monitor.finding_key("dead_letter_growth:54>0") == "dead_letter_growth"
    calls.clear()
    infra_monitor.record_findings("worker-01:infra", (), {})
    assert any("monitor_clear_finding" in c[0] for c in calls)


def test_main_exits_non_zero_when_findings_cannot_be_recorded_even_if_healthy(monkeypatch, capsys) -> None:
    """Review of fc167cf7: a healthy measurement whose persistence failed
    exited 0, so a broken finding store passed unnoticed. Fail closed."""
    monkeypatch.setattr(
        infra_monitor, "discover_worker_units", lambda: {"voyn-aicc-worker@1.service": "active"}
    )
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
    monkeypatch.setattr(
        infra_monitor,
        "record_findings",
        lambda source, failures, detail: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    result = infra_monitor.main(
        ["--minimum-active-workers", "1", "--skip-queue", "--prometheus-url", "http://m/ready",
         "--record-findings", "worker-01:infra"]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["findings_recorded"] is False
    assert "db down" in out["findings_error"]
    assert result == 1


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #481)
# ---------------------------------------------------------------------------
# `control-01:queue` reported `queue_stalled` against a healthy fleet. The
# stall clock was `now() - min(work_item.updated_at)` over every ready or
# claimed row, and for a claimed row that timestamp is the moment it was
# CLAIMED -- heartbeats renew `work_attempt.visible_until` and never touch the
# item. Any attempt outrunning `--max-stalled-seconds` (900s) therefore read as
# a stall, and the deployment's own units expect attempts far longer than that
# (`voyn-aicc-worker@.service`: TimeoutStopSec=3660s for one attempt, plus a
# 600s worktree clone before the agent starts). The monitor could not be green
# while the fleet did its job.


def test_a_live_lease_is_progress_not_a_stall() -> None:
    """THE REGRESSION. One lane 40 minutes into an attempt, heartbeating: the
    snapshot reports it as an attended claim (`pending_unattended == 0`), and
    the monitor stays green."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=1,
            succeeded=100,
            dead=0,
            success_age_seconds=4000,
            pending_age_seconds=None,
            pending_unattended=0,
            live_claim_age_seconds=2400,
            recent_succeeded=0,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok
    assert report.failures == ()


def test_a_live_lease_does_not_become_a_throughput_stall_either() -> None:
    """The other half of the same false positive: gating the throughput check
    on `ready + claimed` merely renamed the failure, because a fleet whose
    only work is one hour-long attempt has nothing succeeded in the trailing
    hour by construction."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=4,
            succeeded=100,
            dead=0,
            success_age_seconds=5000,
            pending_age_seconds=None,
            pending_unattended=0,
            live_claim_age_seconds=3000,
            recent_succeeded=0,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok


def test_a_claim_whose_lease_lapsed_is_still_a_stall() -> None:
    """The zombie the check exists for survives the fix: no live lease, and
    the reaper (every minute) has not recovered it inside the stall window."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=1,
            succeeded=100,
            dead=0,
            success_age_seconds=5,
            pending_age_seconds=1200,
            pending_unattended=1,
            live_claim_age_seconds=None,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert not report.ok
    assert "queue_stalled" in report.failures


def test_a_live_lease_held_past_the_ceiling_is_reported_as_overdue() -> None:
    """A heartbeat thread beating beside a wedged handler keeps the lease
    live forever, so "attended" cannot mean "never checked": the claim is
    bounded, just above one whole legitimate attempt instead of below it."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=1,
            succeeded=100,
            dead=0,
            success_age_seconds=9000,
            pending_age_seconds=None,
            pending_unattended=0,
            live_claim_age_seconds=9001,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_claim_seconds=5400,
    )

    assert not report.ok
    assert report.failures == ("claim_overdue:9001s>5400s",)
    # One finding, not one per tick: the measurement rides in the detail.
    assert infra_monitor.finding_key(report.failures[0]) == "claim_overdue"


def test_the_claim_ceiling_clears_one_legitimate_attempt() -> None:
    """The default is not a number pulled from the air: the worker unit gives
    a single attempt TimeoutStopSec=3660s, and the handler provisions a
    worktree (600s clone timeout) before the agent starts."""
    unit = (
        Path(__file__).resolve().parents[2]
        / "deploy/systemd/voyn-aicc-worker@.service"
    ).read_text(encoding="utf-8")
    stop_timeout = int(
        next(
            line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")
        ).split("=", 1)[1].rstrip("s")
    )
    assert infra_monitor.DEFAULT_MAX_CLAIM_SECONDS > stop_timeout


def test_the_snapshot_query_excludes_live_leases_from_the_stall_clock() -> None:
    """The clock is only as honest as the SQL that feeds it: without the join
    to `work_attempt_public` the monitor cannot tell an attended claim from an
    abandoned one, and this file's evaluate-level tests would pass over a
    snapshot that still measured `min(updated_at)` across every pending row.

    `tests/db/test_infra_monitor_queue_snapshot.py` executes this same
    statement against a real server; this one keeps the join from being
    dropped in a checkout with no PostgreSQL to hand."""
    sql = infra_monitor._QUEUE_SNAPSHOT_SQL
    assert "work_attempt_public" in sql
    assert "a.visible_until > now()" in sql
    # The lease-less half of "unattended": a ready item still inside its
    # backoff is waiting by design and must not start the stall clock.
    assert "w.available_at > now()" in sql
