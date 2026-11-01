from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from command_center.ops import infra_monitor
from command_center.ops.infra_monitor import (
    QueueSnapshot,
    SourceCloneSnapshot,
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


def _spinning(**kw):
    """Lanes spinning on refusals -- the shape `throughput_stalled` is for.

    One lane holds an item, the other is free, and the work they keep failing
    has come back as due-ready rows past the poll ceiling. That is what
    "spinning" leaves behind in the queue: `queue_fail` requeues, so occupancy
    DROPS between spins and the requeued item is offered to a free lane again.

    Built out here rather than taken from `_queue()`, which is a
    FULL-OCCUPANCY shape (`claimed` == the default capacity). Its corroboration
    used to come from its two lapsed claims, and a lapse below the stall window
    is the reaper working rather than the pipeline stalling -- see
    `test_a_slipped_lease_inside_the_reapers_cadence_is_not_a_throughput_stall`.
    """
    from command_center.ops.infra_monitor import QueueSnapshot

    starved = infra_monitor.CLAIM_POLL_CEILING_SECONDS * 2
    base = dict(
        ready=3, claimed=1, succeeded=100, dead=0, success_age_seconds=200.0,
        recent_dead=0, ready_due=3, ready_due_age_seconds=starved,
        lapsed_claims=0, lapsed_claim_age_seconds=None,
        attended_claims=1, live_claim_age_seconds=starved,
        # The spin itself keeps this fresh: a claim or a `queue_fail` every few
        # seconds is an attempt changing state, which is what the fleet clock
        # measures. It must not be what excuses the verdict.
        fleet_idle_seconds=starved,
    )
    base.update(kw)
    return QueueSnapshot(**base)


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
    report = evaluate({"voyn-aicc-worker@1.service": "active"}, _spinning(recent_succeeded=0),
                      minimum_active_workers=1, max_stalled_seconds=900, prometheus_ready=True)
    assert report.failures == ("throughput_stalled:0_succeeded_in_1h",)
    healthy = evaluate({"voyn-aicc-worker@1.service": "active"}, _spinning(recent_succeeded=4),
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
    assert (
        infra_monitor.finding_key("dead_letter_growth:53>0")
        == infra_monitor.finding_key("dead_letter_growth:54>0")
        == "dead_letter_growth"
    )
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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
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


def _slipped_lease(**kw):
    """The deployed fleet at full stretch with one lease late.

    Two lanes 70 minutes into legitimate attempts (`voyn-aicc-worker@.service`
    allows one 3660s, `--max-claim-seconds` 5400s), the two surplus dispatched
    items due behind them (`PlanLimits.wip_limit` 4 against 2 lanes), and lane
    B's lease past its deadline while lane B is still running. Nothing here is
    a fault: `recent_succeeded` is 0 because both attempts are longer than the
    hour it counts over, occupancy is full so no lane was free, and
    `aicc-queue-reaper.timer` clears the slip on its next minute.
    """
    from command_center.ops.infra_monitor import QueueSnapshot

    base = dict(
        ready=2, claimed=2, succeeded=100, dead=0, success_age_seconds=4500.0,
        recent_dead=0, recent_quota_dead=0, recent_succeeded=0,
        ready_due=2, ready_due_age_seconds=7200.0,
        lapsed_claims=1, lapsed_claim_age_seconds=120.0,
        attended_claims=1, live_claim_age_seconds=4200.0,
        # An hour, because both lanes have been busy for one -- no attempt
        # changed state, which is what two healthy long runs look like.
        fleet_idle_seconds=4200.0,
    )
    base.update(kw)
    return QueueSnapshot(**base)


def _judge(snapshot):
    from command_center.ops.infra_monitor import evaluate

    return evaluate(
        {},
        snapshot,
        minimum_active_workers=0,
        max_stalled_seconds=900,
        prometheus_ready=True,
        max_recent_dead=0,
    )


def test_a_slipped_lease_inside_the_reapers_cadence_is_not_a_throughput_stall() -> None:
    """THE REGRESSION (monitor_finding #14016), and the last way this probe
    could not report `ok` at a healthy fleet.

    `throughput_stalled` is the one check that fires INSIDE the stall window,
    so its floor is what makes it honest -- and the floor belongs to ONE of
    the two starvation clocks. `CLAIM_POLL_CEILING_SECONDS` is how long a free
    lane may take to notice DUE READY work, so past it every free lane has
    polled and an unclaimed due row really was passed over.

    Nothing a claimer does clears a LAPSED CLAIM. `queue_claim` cannot even
    see the row -- it is still `claimed` -- and only `aicc-queue-reaper.timer`
    clears it: a one-minute timer with `AccuracySec=15s` whose own unit calls
    a failed tick "a failed tick, not an incident", plus 0040's and 0041's
    deliberate deferral of a contended or freshly renewed row to the next
    tick. A two-minute slip is one missed tick, and the only threshold in
    `evaluate` that describes the reaper is `--max-stalled-seconds`, which is
    exactly what `lapsed_claim_age_seconds` is weighed against -- above, and
    unconditionally.

    Applying the claimer's floor to the reaper's clock made the state the
    three commits before this one taught the probe to read as healthy red
    again under a different name, two minutes after the slip that
    `test_a_lane_whose_lease_slipped_is_still_holding_its_lane` (30 seconds)
    proved green."""
    assert _judge(_slipped_lease()).ok, _judge(_slipped_lease()).failures

    # Where the excuse ends: the reaper's cadence, not the reaper's absence.
    # One second past the window the same slip IS reported -- by the
    # unconditional clock, under its own name, and alone.
    unreaped = _judge(_slipped_lease(lapsed_claim_age_seconds=901.0))
    assert unreaped.failures == ("queue_stalled",)


def test_no_lapse_inside_the_stall_window_can_mint_a_throughput_stall() -> None:
    """The property behind the test above, swept rather than sampled.

    A lapse is the reaper's to clear and `--max-stalled-seconds` is the only
    threshold that measures whether the reaper still is, so no lapse age
    strictly below that window may produce a failure on its own -- at any
    occupancy, with any due backlog behind it, and whether or not anything
    succeeded in the trailing hour. Above the window exactly one failure
    appears, and it is `queue_stalled`: one condition, one finding, one task.

    Swept because the defect was a BAND (60s to 900s), not a point, and a
    single sampled age inside it would leave the rest of the band free to
    come back."""
    window = 900.0
    for age in (0.5, 30.0, 59.9, 60.0, 60.1, 120.0, 450.0, 899.9, window):
        for succeeded in (0, 4):
            report = _judge(
                _slipped_lease(
                    lapsed_claim_age_seconds=age, recent_succeeded=succeeded
                )
            )
            assert report.ok, (age, succeeded, report.failures)
    for age in (900.1, 1800.0, 86400.0):
        for succeeded in (0, 4):
            report = _judge(
                _slipped_lease(
                    lapsed_claim_age_seconds=age, recent_succeeded=succeeded
                )
            )
            assert report.failures == ("queue_stalled",), (age, succeeded)


def test_the_throughput_check_never_fires_beside_queue_stalled() -> None:
    """The two are mutually exclusive by construction, and the planner is why:
    0038 keys a finding on the failure CODE and mints one task per code, so a
    single stalled queue reporting under both names opens two findings and
    files two tasks for one thing to fix.

    The upper bound is therefore still keyed on the WHOLE starved age rather
    than on the unoffered one -- a lapse past the window has already minted
    `queue_stalled`, even when the due-ready clock beside it is inside the
    window and a lane is free."""
    from command_center.ops.infra_monitor import QueueSnapshot

    # A lapse the reaper never cleared, with a free lane and a due item that
    # only just became due: the unoffered clock is inside the window, the
    # lapse clock is far outside it.
    both = _judge(
        QueueSnapshot(
            ready=1, claimed=1, succeeded=100, dead=0, success_age_seconds=7200.0,
            recent_dead=0, recent_succeeded=0,
            ready_due=1, ready_due_age_seconds=300.0,
            lapsed_claims=1, lapsed_claim_age_seconds=5000.0,
            attended_claims=0, live_claim_age_seconds=None,
            fleet_idle_seconds=5000.0,
        )
    )
    assert both.failures == ("queue_stalled",)


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
# own schedule (0041: "the fleet coming BACK from a stall ... that cost two
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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
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
    assert "--record-findings" not in worker_unit
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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
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
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
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
