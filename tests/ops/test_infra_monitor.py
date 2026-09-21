from __future__ import annotations

import json
from pathlib import Path

import pytest

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
            ready_due=3,
            ready_due_age_seconds=901,
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

    ``ready_due`` is set to match ``ready_due_age_seconds``: the two come
    from one filter in ``_QUEUE_SNAPSHOT_SQL`` and the count is 0 exactly
    when the age is ``None``, so a non-``None`` age beside a count of 0 is a
    snapshot the database cannot produce.
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
            ready_due=1,
            ready_due_age_seconds=10,
        ),
        minimum_active_workers=4,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok


def test_claimed_queue_without_recent_success_fails_closed() -> None:
    """Claims whose leases have LAPSED (so the snapshot counts them as
    lapsed, not attended) and that nothing has recovered inside the stall
    window."""
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
            lapsed_claims=2,
            lapsed_claim_age_seconds=901,
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
            lapsed_claims=1,
            lapsed_claim_age_seconds=901,
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


def test_the_queue_probe_records_under_the_source_the_live_findings_carry(
    monkeypatch,
) -> None:
    """`monitor_finding` rows are keyed by (source, failure code), and the
    planner mints ONE task per open finding from that pair. The live
    control-01 probe records under `control-01:queue` (monitor_finding #481),
    so the unit may not drift to another spelling: a second source would open
    a second, unlinked finding for the same measurement.

    The unit carries the source as `Environment=` and the parser defaults to
    it, so this asserts the whole path rather than the spelling alone -- the
    two are useless apart, and only the environment reaches the CLI here (the
    ExecStart line is this host's absolute install path, which the public-repo
    leak guard forbids re-adding; see the unit's own comment)."""
    queue_unit = Path("deploy/systemd/voyn-queue-monitor.service").read_text()
    source = next(
        line.split("=", 2)[2]
        for line in queue_unit.splitlines()
        if line.startswith(f"Environment={infra_monitor.FINDING_SOURCE_ENV}=")
    )
    assert source == "control-01:queue"

    monkeypatch.setenv(infra_monitor.FINDING_SOURCE_ENV, source)
    args = infra_monitor.build_parser().parse_args(
        ["--skip-workers", "--prometheus-url", "http://metrics/ready"]
    )
    assert args.record_findings == "control-01:queue"

    exec_start = next(
        line for line in queue_unit.splitlines() if line.startswith("ExecStart=")
    )
    # The stall window is the UNATTENDED clock and must stay well under the
    # claim ceiling; conflating the two is what this unit was red for. The
    # ceiling itself is not spelled here -- the default IS the policy, and
    # `test_the_claim_ceiling_clears_one_legitimate_attempt` pins it.
    assert "--max-stalled-seconds 900" in exec_start
    assert "--max-claim-seconds" not in exec_start


def test_an_explicit_source_still_wins_over_the_environment(monkeypatch) -> None:
    """The environment is a default, not an override: a worker host that
    exports one source must not silently rewrite what an operator (or the
    worker-host unit, which passes no source at all and must keep recording
    nothing) asked for on the command line."""
    monkeypatch.setenv(infra_monitor.FINDING_SOURCE_ENV, "control-01:queue")
    argv = ["--skip-queue", "--prometheus-url", "http://metrics/ready"]

    explicit = infra_monitor.build_parser().parse_args(
        [*argv, "--record-findings", "worker-01:infra"]
    )
    assert explicit.record_findings == "worker-01:infra"

    monkeypatch.delenv(infra_monitor.FINDING_SOURCE_ENV)
    # Unset is the worker host: `voyn-infra-monitor.service` names no source,
    # so it measures and reports without touching `monitor_finding` at all.
    assert infra_monitor.build_parser().parse_args(argv).record_findings == ""
    assert "AICC_MONITOR_FINDING_SOURCE" not in Path(
        "deploy/systemd/voyn-infra-monitor.service"
    ).read_text()


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
    queue = QueueSnapshot(0, 0, 1, 0, 1.0)
    monkeypatch.setattr(
        infra_monitor,
        "discover_worker_units",
        lambda: (_ for _ in ()).throw(AssertionError("workers must stay unread")),
    )
    monkeypatch.setattr(infra_monitor, "read_queue_snapshot", lambda _queue: queue)
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

    # Five starved items well past the poll ceiling: three ready with a lane
    # free to take them, two claims whose leases lapsed. Inside the stall
    # window and past the ceiling -- the band `throughput_stalled` owns.
    #
    # DERIVED from the ceiling rather than written out, because it was 60.0
    # when the ceiling was 30.0 and the ceiling turned out to be half the
    # daemon's real worst-case poll gap. A literal here would have made
    # raising the ceiling look like it broke this test, when what it actually
    # did was move the band this helper is trying to sit in.
    starved = infra_monitor.CLAIM_POLL_CEILING_SECONDS * 2
    base = dict(ready=3, claimed=2, succeeded=100, dead=0, success_age_seconds=200.0,
                recent_dead=0, ready_due=3, ready_due_age_seconds=starved,
                lapsed_claims=2, lapsed_claim_age_seconds=starved)
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


@pytest.fixture
def recorded_statements(monkeypatch):
    """Every statement `record_findings` executes, in order, over a fake pool.

    A fake rather than a server because what is under test here is the CALL
    SEQUENCE -- which failures are recorded, and with which keys the clear is
    asked to spare them. What those statements then DO to the rows is a
    property of the SECURITY DEFINER functions, and is proved against a real
    server in `tests/db/test_monitor_finding_clear.py`.
    """
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
    return calls


def test_findings_are_recorded_and_cleared_through_the_definer_functions(
    recorded_statements,
) -> None:
    calls = recorded_statements
    infra_monitor.record_findings("worker-01:infra", ("active_workers:2<4",), {"x": 1})
    recorded = [c for c in calls if "monitor_record_finding" in c[0]]
    # Identity is the failure code, the measurement rides in the detail: the
    # same red probe measured 2<4 then 1<4 is ONE finding, not two tasks.
    assert recorded[0][1][:2] == ("worker-01:infra", "active_workers")
    assert json.loads(recorded[0][1][2]) == {"x": 1, "failure": "active_workers:2<4"}
    assert infra_monitor.finding_key("dead_letter_growth:53>0") == infra_monitor.finding_key("dead_letter_growth:54>0") == "dead_letter_growth"
    calls.clear()
    infra_monitor.record_findings("worker-01:infra", (), {})
    # A green tick clears the whole source, which is what an empty list of
    # still-failing keys means to `monitor_clear_finding(text, text[])`.
    cleared = [c for c in calls if "monitor_clear_finding" in c[0]]
    assert cleared == [("SELECT monitor_clear_finding(%s, %s)", ("worker-01:infra", []))]


def test_a_resolved_failure_is_cleared_while_its_red_siblings_stay_open(
    recorded_statements,
) -> None:
    """THE FINDING-LIFECYCLE REGRESSION (monitor_finding #481).

    `control-01:queue` reports six independent failures and clears them
    through ONE source. The clear used to run only on an all-green tick, and
    the probe is deployed with `--max-recent-dead 0` -- one dead-lettered item
    in the trailing hour is enough to keep `dead_letter_growth` red -- so a
    `queue_stalled` finding that has measured healthy for days had no
    reachable way out: the tick that would have cleared it never came.

    Now every tick reconciles: what is still measured is recorded, and the
    complement is cleared by name. `dead_letter_growth` keeps its row (and
    with it its `opened_at` and the task the planner linked), `queue_stalled`
    is not in the spared list and goes.
    """
    calls = recorded_statements
    infra_monitor.record_findings(
        "control-01:queue", ("dead_letter_growth:53>0",), {"ok": False}
    )

    assert [c[1][1] for c in calls if "monitor_record_finding" in c[0]] == [
        "dead_letter_growth"
    ]
    (clear,) = [c for c in calls if "monitor_clear_finding" in c[0]]
    source, spared = clear[1]
    assert source == "control-01:queue"
    # Keyed like the recording call, never the raw measurement: sparing
    # "dead_letter_growth:53>0" would match no row, so the finding this tick
    # just recorded would be cleared by the same tick.
    assert spared == ["dead_letter_growth"]
    assert "queue_stalled" not in spared
    # And it runs on a RED tick, which is the whole change: the clear used to
    # be the else-branch of `if failures`.
    assert calls.index(clear) > 0


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
    snapshot reports it as an attended claim and nothing as starved, and the
    monitor stays green."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=1,
            succeeded=100,
            dead=0,
            success_age_seconds=4000,
            attended_claims=1,
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
            attended_claims=4,
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
            lapsed_claims=1,
            lapsed_claim_age_seconds=1200,
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
            attended_claims=1,
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
    # The lease-less half: a ready item still inside its backoff is waiting
    # by design, so only a DUE one may start the stall clock.
    assert "w.available_at <= now()" in sql


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, the second half.
#
# Excluding attended claims from the stall clock was necessary and not
# sufficient. The queue is DESIGNED to hold more dispatched work than the fleet
# can claim -- `PlanLimits.wip_limit` is 4 against the 2 lanes of
# `deploy/aicc/worker-lanes`, and `backlog_dispatch` bounds concurrency by
# per-repository writer leases across three fleet repositories -- so a surplus
# item sits `ready` and DUE until a lane frees. That wait is the length of a
# whole attempt (`TimeoutStopSec=3660s` plus provisioning), far past
# `--max-stalled-seconds`, so `control-01:queue` would have gone red again on
# the ready rows alone once it stopped counting the claimed ones.
#
# A due ready item is now a stall only when a lane was FREE to take it:
# `queue_claim` has no repository or lane affinity and the daemon's idle poll
# backs off no further than 30s, so a free lane takes due work almost at once.


def test_due_work_queued_behind_a_full_fleet_is_backpressure_not_a_stall() -> None:
    """THE REGRESSION. Two lanes, both holding live leases, and a third
    dispatched item ready and due for an hour behind them: the fleet is at
    capacity, which is the planner's WIP limit working, not a stall."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        QueueSnapshot(
            ready=1,
            claimed=2,
            succeeded=100,
            dead=0,
            success_age_seconds=4000,
            recent_succeeded=0,
            ready_due=1,
            ready_due_age_seconds=3600,
            attended_claims=2,
            live_claim_age_seconds=3600,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.ok
    assert report.failures == ()


def test_a_free_lane_that_leaves_due_work_unclaimed_is_still_a_stall() -> None:
    """The same snapshot with one lane free. `queue_claim` takes the oldest
    due row with no repository or lane affinity, so a free lane that has not
    claimed for the whole stall window is broken, and capacity must not
    excuse it."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        QueueSnapshot(
            ready=1,
            claimed=1,
            succeeded=100,
            dead=0,
            success_age_seconds=4000,
            ready_due=1,
            ready_due_age_seconds=3600,
            attended_claims=1,
            live_claim_age_seconds=3600,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_a_full_fleet_never_excuses_a_lapsed_claim() -> None:
    """Capacity answers "could a lane have taken this?", which is a question
    only unclaimed work raises. A claim whose lease lapsed is already held by
    nobody, so a busy fleet is no explanation for it and the zombie check
    keeps firing at full capacity."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        QueueSnapshot(
            ready=0,
            claimed=3,
            succeeded=100,
            dead=0,
            success_age_seconds=10,
            lapsed_claims=1,
            lapsed_claim_age_seconds=1200,
            attended_claims=2,
            live_claim_age_seconds=1200,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert not report.ok
    assert report.failures == ("queue_stalled",)


def test_a_just_enqueued_item_is_not_a_throughput_stall_on_a_quiet_fleet() -> None:
    """`throughput_stalled` is the one check that fires INSIDE the stall
    window, so it has to be bounded below as well. An hour with no successes
    is ordinary here -- a single attempt may run longer than that -- so a
    queue that had been empty all night would otherwise go red the second the
    planner dispatched the first task, seconds before a free lane's next
    poll."""
    seconds_old = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=1,
            claimed=0,
            succeeded=100,
            dead=0,
            success_age_seconds=7200,
            recent_succeeded=0,
            ready_due=1,
            ready_due_age_seconds=5,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert seconds_old.ok

    # Past the poll ceiling every free lane has polled at least once, so the
    # same item still sitting there IS evidence -- the floor is a grace
    # period, not an exemption.
    polled_past = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=1,
            claimed=0,
            succeeded=100,
            dead=0,
            success_age_seconds=7200,
            recent_succeeded=0,
            ready_due=1,
            ready_due_age_seconds=300,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert polled_past.failures == ("throughput_stalled:0_succeeded_in_1h",)


def test_the_throughput_floor_covers_the_workers_poll_ceiling() -> None:
    """The floor is not a number pulled from the air: it is how long a free
    lane may take to notice due work.

    THE REGRESSION (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED). This test used
    to assert `CLAIM_POLL_CEILING_SECONDS >= WorkerConfig().idle_max_seconds`
    and the constant was exactly that, 30.0 -- restating one of the daemon's
    numbers rather than measuring the thing the floor is about.
    `idle_max_seconds` caps THE BACKOFF, not the sleep: the daemon sleeps
    `idle + random.uniform(0, idle)`, so once the backoff saturates at 30s
    every gap between polls is uniform on [30s, 60s). Half of that range sat
    above the floor, and an item enqueued onto a quiet queue in it read as
    `throughput_stalled`.

    So the bound is DERIVED here, by running the daemon's own backoff to
    saturation, and a change to either the cap or the jitter fails this test
    instead of the fleet.
    """
    from command_center.worker.daemon import WorkerConfig

    config = WorkerConfig()
    idle = config.idle_min_seconds
    worst_gap = 0.0
    for _ in range(64):  # well past saturation at idle_min=1 -> idle_max=30
        # `self._sleep(min(idle + random.uniform(0, idle), cap))`, taken at
        # its supremum: `cap` is the watchdog half-interval (120s for the
        # unit's WatchdogSec=240s) and never clamps this.
        worst_gap = max(worst_gap, idle * 2)
        idle = min(idle * 2, config.idle_max_seconds)

    assert worst_gap == pytest.approx(config.idle_max_seconds * 2)
    assert infra_monitor.CLAIM_POLL_CEILING_SECONDS >= worst_gap


def test_a_lane_that_has_not_polled_yet_is_not_a_throughput_stall() -> None:
    """The live false positive the floor above was too low to stop: a quiet
    queue, a free lane whose saturated idle backoff has not come round again,
    and an item 45 seconds old. Nothing has been OFFERED to a claimer yet, so
    there is nothing to have been ignored -- but with the floor at 30s this
    minted a `throughput_stalled` finding, and the planner minted a task from
    it. 45s is inside [30s, 60s): reachable on every poll cycle, not a
    corner."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active"},
        QueueSnapshot(
            ready=1,
            claimed=0,
            succeeded=100,
            dead=0,
            success_age_seconds=7200,
            recent_succeeded=0,
            ready_due=1,
            ready_due_age_seconds=45,
        ),
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )

    assert report.ok, report.failures


def test_the_claim_capacity_default_matches_the_canonical_lane_registry() -> None:
    """A lane runs one attempt at a time, so capacity is the lane count. The
    default tracks the registry the install transaction ships; a fleet that
    grows past it has to say so with --claim-capacity, and this pins the two
    together so growing it cannot silently leave the monitor behind."""
    registry = (
        Path(__file__).resolve().parents[2] / "deploy/aicc/worker-lanes"
    ).read_text(encoding="utf-8")
    lanes = [
        line.strip()
        for line in registry.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert infra_monitor.DEFAULT_CLAIM_CAPACITY == len(lanes)


def test_claim_capacity_is_configurable_and_reaches_the_verdict(
    monkeypatch, capsys
) -> None:
    """The flag has to travel all the way from argv into `evaluate`: a
    scaled fleet whose third lane sits idle while due work waits is a stall
    the default two-lane capacity would excuse."""
    queue = QueueSnapshot(
        ready=1,
        claimed=2,
        succeeded=100,
        dead=0,
        success_age_seconds=4000,
        ready_due=1,
        ready_due_age_seconds=3600,
        attended_claims=2,
        live_claim_age_seconds=3600,
    )
    monkeypatch.setattr(infra_monitor, "read_queue_snapshot", lambda _queue: queue)
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)

    argv = [
        "--skip-workers",
        "--minimum-active-workers",
        "0",
        "--prometheus-url",
        "http://metrics/ready",
    ]

    assert infra_monitor.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["failures"] == []

    assert infra_monitor.main([*argv, "--claim-capacity", "3"]) == 1
    assert json.loads(capsys.readouterr().out)["failures"] == ["queue_stalled"]


def test_a_capacity_of_zero_cannot_excuse_every_unclaimed_item() -> None:
    """`backlog_dispatch` reads its own cap as `greatest(p_wip_limit, 1)`;
    capacity follows that convention rather than letting 0 mean "no lane can
    ever claim, so nothing is ever late"."""
    report = evaluate(
        {},
        QueueSnapshot(
            ready=1,
            claimed=0,
            succeeded=100,
            dead=0,
            success_age_seconds=4000,
            ready_due=1,
            ready_due_age_seconds=3600,
        ),
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=0,
    )

    assert report.failures == ("queue_stalled",)


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, the third half (monitor_finding
# #2471).
#
# Capacity gated the COMPARISON and left the CLOCK running. The queue is meant
# to hold work no lane can attend yet, so the surplus row's due age climbs for
# hours while the fleet is legitimately full -- and the instant occupancy drops
# below capacity that hours-old number is measured against 900s. Occupancy
# drops at every attempt boundary (the daemon's loop claims again immediately,
# but `queue_complete` commits first), at every lane restart the 5-minute
# self-deploy tick issues, and at every drain. The probe samples every two
# minutes.
#
# So the stall clock is bounded by how long the FLEET has been standing still:
# an item is only being ignored while there is somebody to ignore it.


def _backpressure(**kw):
    """Two lanes, a third dispatched item ready and due for an hour behind
    them -- `PlanLimits.wip_limit` (4) against 2 lanes, the shape the queue is
    designed to hold."""
    from command_center.ops.infra_monitor import QueueSnapshot

    base = dict(
        ready=1,
        claimed=2,
        succeeded=100,
        dead=0,
        success_age_seconds=4000,
        recent_succeeded=1,
        ready_due=1,
        ready_due_age_seconds=3600,
        attended_claims=2,
        live_claim_age_seconds=3600,
    )
    base.update(kw)
    return QueueSnapshot(**base)


def test_the_instant_a_lane_frees_is_not_a_stall_that_was_hours_old() -> None:
    """THE REGRESSION. One of the two lanes has just committed its result, so
    the fleet is momentarily one claim below capacity while the item it is
    about to take has been due for an hour. Before the fleet clock, every
    attempt boundary on a healthy fleet was a `queue_stalled` tick waiting for
    the 2-minute timer to land on it."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(
            claimed=1,
            attended_claims=1,
            # The completion itself is the fleet event: a lane handed work
            # back a moment ago, so nothing here has been ignored for an hour.
            fleet_idle_seconds=0.4,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.ok, report.failures


def test_a_lane_that_has_not_moved_for_the_whole_window_is_still_a_stall() -> None:
    """The bound is not an excuse. The same free lane, but the fleet has taken
    nothing and handed nothing back for the whole stall window: there is no
    claim in flight to explain the wait, and the clock runs in full."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(claimed=1, attended_claims=1, fleet_idle_seconds=3600),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.failures == ("queue_stalled",)


def test_an_unmeasurable_fleet_clock_excuses_nothing() -> None:
    """A queue no lane has ever claimed from has no fleet clock at all --
    `max()` over an empty `work_attempt` is NULL. That is the shape of a fleet
    that never started, so the due age stands on its own and the probe fails
    closed rather than treating "no evidence" as "serving"."""
    report = evaluate(
        {},
        _backpressure(
            claimed=0,
            attended_claims=0,
            live_claim_age_seconds=None,
            fleet_idle_seconds=None,
        ),
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.failures == ("queue_stalled",)


def test_a_busy_fleet_clock_never_excuses_a_lapsed_claim() -> None:
    """The bound answers "was anybody free to take this?", which only
    UNCLAIMED work asks. A claim whose lease lapsed is held by nobody, and a
    neighbouring lane claiming away beside it says nothing about it -- only
    the reaper does. So the lapse keeps its own clock at any fleet activity,
    exactly as it keeps it at any capacity."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(
            claimed=3,
            lapsed_claims=1,
            lapsed_claim_age_seconds=1200,
            fleet_idle_seconds=1.0,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.failures == ("queue_stalled",)


def test_the_fleet_clock_reads_claims_and_releases_but_not_heartbeats() -> None:
    """The measurement's honesty is in the CASE. `queue_heartbeat` writes
    `updated_at = now()` on a row that stays 'active', so reading `updated_at`
    unconditionally would make one lane renewing one lease look like a fleet
    claiming continuously -- and that would excuse every stall there is.
    `tests/db/test_infra_monitor_queue_snapshot.py` proves the behaviour
    against a real server; this keeps the expression from being flattened in a
    checkout with no PostgreSQL to hand."""
    sql = infra_monitor._QUEUE_SNAPSHOT_SQL
    assert (
        "CASE WHEN a.state = 'active' THEN a.created_at\n"
        "                         ELSE a.updated_at END" in sql
    )


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, the fourth half (monitor_finding
# #2766): the measurement spanned every queue, the verdict knew one fleet.
# ---------------------------------------------------------------------------


def test_the_probes_queue_default_matches_the_daemons_own() -> None:
    """Capacity is pinned to the lane registry; the queue is pinned to the
    lanes' own config, and for the same reason. `queue_claim` serves exactly
    the queue it is given and the daemon gives it exactly this one, so a
    default that drifted from it would leave the probe measuring work no lane
    claims -- a `queue_stalled` finding with no way for the fleet to clear it.

    Imported here rather than in `infra_monitor` on purpose: the probe runs
    from the control host's checkout and must not need the worker package to
    read one string. This test is the seam that keeps the copy honest.
    """
    from command_center.worker.daemon import WorkerConfig

    assert infra_monitor.DEFAULT_QUEUE == WorkerConfig().queue


def test_the_queue_flag_reaches_the_measurement(monkeypatch, capsys) -> None:
    """The name has to travel from argv into the statement's parameter, not
    just into the namespace: the snapshot is the only thing that knows which
    rows it read, so a flag that stopped at `args` would measure `execution`
    while reporting under another queue's source."""
    asked: list[str] = []

    def _read(queue: str):
        asked.append(queue)
        return QueueSnapshot(0, 0, 1, 0, 1.0)

    monkeypatch.setattr(infra_monitor, "read_queue_snapshot", _read)
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)

    argv = [
        "--skip-workers",
        "--minimum-active-workers",
        "0",
        "--prometheus-url",
        "http://metrics/ready",
    ]

    assert infra_monitor.main(argv) == 0
    assert asked == [infra_monitor.DEFAULT_QUEUE]

    assert infra_monitor.main([*argv, "--queue", "staging"]) == 0
    assert asked == [infra_monitor.DEFAULT_QUEUE, "staging"]
    capsys.readouterr()


def test_the_statement_binds_the_queue_to_both_of_its_questions(monkeypatch) -> None:
    """Two placeholders, and `read_queue_snapshot` binds the same name to
    both. One filters the pending work; the other filters the attempts that
    say whether the lanes serving that work have moved. Binding only the
    first would let another queue's lane wind this queue's fleet clock
    forward, and `evaluate` takes `min(due_age, fleet_idle)` -- so a stall of
    any age here would be excused by progress somewhere else.
    """
    assert infra_monitor._QUEUE_SNAPSHOT_SQL.count("%s") == 2

    bound: list[tuple] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, statement, params=None): bound.append((statement, params))
        def fetchone(self):
            return (0, 0, 0, 0, None, 0, 0, 0, 0, None, 0, None, 0, None, None)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    class _Pool:
        @staticmethod
        def open_pool(cfg): pass
        @staticmethod
        def connection(): return _Conn()
        @staticmethod
        def close_pool(): pass

    # The same shape as `recorded_statements`, and for the same reason: the
    # REAL `command_center.db` is replaced along with its submodules, so
    # `from command_center.db import pool` cannot bind this stub onto the
    # genuine package and leak it into every later test in the session.
    import sys
    import types
    fake_db = types.ModuleType("command_center.db")
    fake_db.pool = _Pool
    fake_cfg = types.ModuleType("command_center.db.config")
    fake_cfg.load_config = lambda: {}
    monkeypatch.setitem(sys.modules, "command_center.db", fake_db)
    monkeypatch.setitem(sys.modules, "command_center.db.pool", _Pool)
    monkeypatch.setitem(sys.modules, "command_center.db.config", fake_cfg)

    infra_monitor.read_queue_snapshot("staging")

    assert bound == [(infra_monitor._QUEUE_SNAPSHOT_SQL, ("staging", "staging"))]


# ---------------------------------------------------------------------------
# Capacity is OCCUPANCY, not attendance (monitor_finding #13366).
#
# `spare_capacity` answers "was a lane free to take this?", and it used to ask
# `attended_claims < capacity` -- how many lanes hold a LIVE LEASE. That is a
# different question. A lane whose lease slipped is still HOLDING its item:
# the row stays `claimed` until the reaper takes it back, and `queue_claim`
# will not hand that lane a second one meanwhile.
#
# The gap between the two is how a healthy long run can still look for a few
# seconds, and the measurement has to survive it however rare it gets. It was
# once ROUTINE: the beat ran at `visibility_seconds / 3` (100s against a 300s
# window), which puts the third beat ON the deadline, so any two consecutive
# failed beats lapsed the lease -- a database blip, or the
# `voyn-aicc-pgtunnel.service` restart the credential rotation cycles on its
# own schedule (0029: "the fleet coming BACK from a stall ... that cost two
# beats"). `WorkerDaemon.beat_interval_seconds` now renews with a whole beat
# of margin (monitor_finding #13420), so that blip no longer lapses anything
# and these rules have less to excuse. What still reaches them --  an outage
# past the tolerance, a host that is gone -- is the same shape, and
# `aicc-queue-reaper.timer` still clears it on the next minute while the probe
# samples every two.
#
# These are here beside the DB proofs and not only in them, for the reason
# `test_reap_bound.py` exists: they need no server, so they run in every gate
# on every machine.


def test_a_lease_that_slipped_does_not_free_the_lane_holding_it() -> None:
    """THE REGRESSION. The fleet is at capacity -- two lanes, two items held
    -- with the surplus dispatched item due behind them for an hour, which is
    `PlanLimits.wip_limit` (4) against 2 lanes working as designed. One
    lease is 30 seconds late. No lane is free, nothing has stopped, and the
    reaper will clear the lapse within the minute."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(
            claimed=2,
            attended_claims=1,
            lapsed_claims=1,
            lapsed_claim_age_seconds=30,
            # An hour, because both lanes have been busy for an hour: no
            # attempt changed state, which is what two long runs look like.
            # The fleet clock cannot catch this one.
            fleet_idle_seconds=3600,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.ok, report.failures


def test_a_lapse_past_the_window_is_a_stall_at_any_occupancy() -> None:
    """What the fix gives up, and why nothing is lost. A lapsed claim can
    also mean the lane is GONE. That case is not reached through capacity --
    it never was -- because `lapsed_claim_age_seconds` is weighed
    unconditionally: not gated by capacity, not bounded by the fleet clock.
    The same full-fleet snapshot, with the lapse older than the window."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(
            claimed=2,
            attended_claims=1,
            lapsed_claims=1,
            lapsed_claim_age_seconds=1200,
            fleet_idle_seconds=3600,
        ),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.failures == ("queue_stalled",)


def test_a_lane_that_gave_its_item_back_is_free_again() -> None:
    """The bound on how long the fix can excuse anything. The reaper moves
    the item OUT of `claimed`, so occupancy drops on its own and the
    due-ready clock runs again against a fleet standing still. A lapsed claim
    can never excuse work for longer than `aicc-queue-reaper.timer` takes --
    and a reaper that stops is what the test above measures."""
    report = evaluate(
        {"voyn-aicc-worker@1.service": "active", "voyn-aicc-worker@2.service": "active"},
        _backpressure(claimed=1, attended_claims=1, fleet_idle_seconds=3600),
        minimum_active_workers=2,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )

    assert report.failures == ("queue_stalled",)


def test_occupancy_is_what_capacity_weighs_and_the_two_can_differ() -> None:
    """The distinction stated directly, so a future reader cannot restore the
    old test by reading `attended_claims` as "the busy lanes". Identical
    snapshots but for which column carries the second claim: both describe a
    fleet holding two items against two lanes, and both are backpressure."""
    common = dict(
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        claim_capacity=2,
    )
    both_live = evaluate({}, _backpressure(claimed=2, attended_claims=2), **common)
    one_slipped = evaluate(
        {},
        _backpressure(
            claimed=2,
            attended_claims=1,
            lapsed_claims=1,
            lapsed_claim_age_seconds=30,
        ),
        **common,
    )

    assert both_live.ok and one_slipped.ok
    assert both_live.failures == one_slipped.failures == ()


def test_counting_a_lapsed_claim_as_occupancy_can_only_excuse_never_accuse() -> None:
    """The direction of the change, pinned as a property rather than as a
    case. `claimed` is `attended_claims + lapsed_claims` by construction --
    the two filters are disjoint and together cover `state = 'claimed'` -- so
    `claimed < capacity` implies `attended_claims < capacity` and never the
    reverse. Spare capacity can therefore only go from true to false, the
    starved set can only shrink, and no tick that was green can be turned red
    by this rule.

    That matters because this is a FAIL-CLOSED probe whose red ticks mint
    tasks: a change to the capacity test earns its way in by removing false
    accusations, and must not be able to add one anywhere. Here the same
    fleet is described twice at every capacity, moving one claim from
    attended to lapsed -- which is exactly what a slipped lease does to the
    snapshot."""
    for capacity in (1, 2, 3, 4):
        for held in range(1, 5):
            common = dict(
                minimum_active_workers=0,
                max_stalled_seconds=900,
                prometheus_ready=True,
                claim_capacity=capacity,
            )
            all_live = evaluate(
                {}, _backpressure(claimed=held, attended_claims=held), **common
            )
            one_slipped = evaluate(
                {},
                _backpressure(
                    claimed=held,
                    attended_claims=held - 1,
                    lapsed_claims=1,
                    # Inside the window, so the unconditional lapse clock is
                    # not what is being compared here.
                    lapsed_claim_age_seconds=30,
                ),
                **common,
            )

            accused = set(one_slipped.failures) - set(all_live.failures)
            assert not accused, (capacity, held, accused)
