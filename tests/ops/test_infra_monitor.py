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
#
# VOYN-MON-WORKER-01-INFRA-CRASH-LOOP: the first version of that probe judged
# the LIFETIME counter, so "has this unit ever restarted N times" -- true of
# every `Restart=` unit on a host that has been up long enough, and permanent
# once true. The verdict is now the gain across a window of ticks, which is
# what the word "loop" means and the only shape that can ever clear.


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

#: The monitor tick (voyn-infra-monitor.timer: OnUnitInactiveSec=2min) and the
#: defaults the unit runs with: five restarts GAINED in an hour is a loop.
TICK = 120.0
WINDOW = 3600.0
THRESHOLD = 5


def _history(
    *readings: tuple[float, dict[str, int]], window_seconds: float = WINDOW
) -> infra_monitor.RestartHistory:
    """Fold `(when, {unit: NRestarts})` readings into a history, tick by tick."""
    history = infra_monitor.RestartHistory()
    for when, counters in readings:
        history = history.observe(counters, now=when, window_seconds=window_seconds)
    return history


def _looping(
    unit: str, *, per_tick: int, ticks: int, start: int = 0, end: float = 0.0
) -> infra_monitor.RestartHistory:
    """A unit restarting `per_tick` times per two-minute tick, ending at `end`."""
    return _history(
        *(
            (end - (ticks - 1 - index) * TICK, {unit: start + index * per_tick})
            for index in range(ticks)
        )
    )


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
    units = infra_monitor.parse_unit_show(SHOW_OUTPUT)
    # Every 3 s: forty restarts gained between two ticks two minutes apart.
    history = _history(
        (0.0, {**infra_monitor.collapse_restart_counters(units), "ollama.service": 315851}),
        (TICK, infra_monitor.collapse_restart_counters(units)),
    )
    snapshot = infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=history, window_seconds=WINDOW
    )

    assert snapshot.crash_loops == (("ollama.service", 40),)
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
        # `+40` is the gain inside the window, not a lifetime total.
        "crash_loop:1:ollama.service=+40",
        "failed_units:1:aicc-agent-launcher@*.service=2",
    }
    assert {infra_monitor.finding_key(f) for f in report.failures} == {
        "crash_loop",
        "failed_units",
    }


def test_a_large_lifetime_counter_is_not_a_loop_once_the_restarting_stops() -> None:
    """The finding this task exists for.

    A unit that restarted 315 891 times and then stopped is not looping now.
    Under a lifetime threshold it was a `crash_loop` finding forever -- no
    later measurement could clear it, so the fail-closed monitor could never
    report ok again and the open finding could never be closed by a fix.
    """
    units = infra_monitor.parse_unit_show(SHOW_OUTPUT)
    counters = infra_monitor.collapse_restart_counters(units)
    quiet = _history(*((index * TICK, counters) for index in range(30)))

    snapshot = infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=quiet, window_seconds=WINDOW
    )

    assert snapshot.crash_loops == ()
    assert snapshot.window_seconds == WINDOW
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    # The launcher instances are still failed; nothing is crash looping.
    assert [f for f in report.failures if f.startswith("crash_loop")] == []


def test_a_loop_that_stops_clears_itself_once_the_window_has_passed() -> None:
    """Acceptance for a monitor finding is "ok for 24h", which requires the
    measurement to fall on its own once the host is healthy."""
    looping = _looping("ollama.service", per_tick=40, ticks=5, end=0.0)
    units = {"ollama.service": infra_monitor.UnitState(160, "activating")}
    assert infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=looping, window_seconds=WINDOW
    ).crash_loops == (("ollama.service", 160),)

    # The operator stops the loop; the counter stands still from here on.
    settled = looping
    for tick in range(1, int(WINDOW / TICK) + 2):
        settled = settled.observe(
            {"ollama.service": 160}, now=tick * TICK, window_seconds=WINDOW
        )
    snapshot = infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=settled, window_seconds=WINDOW
    )

    assert snapshot.crash_loops == ()
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert report.ok


def test_restarts_spread_across_days_are_not_a_loop() -> None:
    """A tunnel that reconnects now and then, a lane that rides out a database
    blip: the same total, nowhere near the rate."""
    day = 86400.0
    history = _history(
        *((index * day, {"voyn-aicc-pgtunnel.service": index}) for index in range(40))
    )

    snapshot = infra_monitor.evaluate_unit_health(
        {"voyn-aicc-pgtunnel.service": infra_monitor.UnitState(39, "active")},
        crash_loop_restarts=THRESHOLD,
        history=history,
        window_seconds=WINDOW,
    )

    assert snapshot.crash_loops == ()


def test_an_operator_restart_rebaselines_instead_of_reporting_a_negative_gain() -> None:
    looping = _looping("ollama.service", per_tick=40, ticks=5, end=0.0)
    # `systemctl restart ollama` zeroes NRestarts; the series before it says
    # nothing about the series after it.
    after = looping.observe({"ollama.service": 0}, now=TICK, window_seconds=WINDOW)

    assert after.gained("ollama.service") == 0
    assert (
        infra_monitor.evaluate_unit_health(
            {"ollama.service": infra_monitor.UnitState(0, "active")},
            crash_loop_restarts=THRESHOLD,
            history=after,
            window_seconds=WINDOW,
        ).crash_loops
        == ()
    )
    # And the loop is found again from the new baseline, not hidden by it.
    relooping = after.observe({"ollama.service": 40}, now=2 * TICK, window_seconds=WINDOW)
    assert relooping.gained("ollama.service") == 40


def test_the_first_tick_measures_no_rate_and_the_next_one_does() -> None:
    """One reading is not a rate. The cost is one tick (two minutes); a loop
    running every 3 s clears any threshold inside that single tick."""
    first = _history((0.0, {"ollama.service": 315891}))
    assert first.gained("ollama.service") == 0

    second = first.observe({"ollama.service": 315931}, now=TICK, window_seconds=WINDOW)
    assert second.gained("ollama.service") == 40


def test_crash_looping_template_instances_collapse_to_the_highest_count() -> None:
    show = "\n".join(
        [
            _show_block("aicc-agent-launcher@1-1-984.service", 7, "activating"),
            _show_block("aicc-agent-launcher@2-2-984.service", 40, "activating"),
            _show_block("aicc-agent-launcher@3-3-984.service", 5, "activating"),
        ]
    )
    units = infra_monitor.parse_unit_show(show)
    counters = infra_monitor.collapse_restart_counters(units)
    assert counters == {"aicc-agent-launcher@*.service": 40}

    history = _history((0.0, {"aicc-agent-launcher@*.service": 0}), (TICK, counters))
    snapshot = infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=history, window_seconds=WINDOW
    )

    assert snapshot.crash_loops == (("aicc-agent-launcher@*.service", 40),)


def test_the_finding_detail_is_capped_not_unbounded() -> None:
    names = [f"svc{i}.service" for i in range(infra_monitor.UNIT_LISTING_CAP + 3)]
    show = "\n".join(_show_block(name, 25, "activating") for name in names)
    history = _history(
        (0.0, {name: 0 for name in names}), (TICK, {name: 25 for name in names})
    )
    snapshot = infra_monitor.evaluate_unit_health(
        infra_monitor.parse_unit_show(show),
        crash_loop_restarts=THRESHOLD,
        history=history,
        window_seconds=WINDOW,
    )
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    (failure,) = report.failures
    assert failure.startswith(f"crash_loop:{infra_monitor.UNIT_LISTING_CAP + 3}:")
    assert failure.endswith(",+3_more")
    assert failure.count("=+25") == infra_monitor.UNIT_LISTING_CAP


def test_the_threshold_boundary_is_inclusive_and_a_few_restarts_are_not_a_loop() -> None:
    history = _history(
        (0.0, {"exactly.service": 0, "almost.service": 0, "voyn-crm.service": 2}),
        (TICK, {"exactly.service": 5, "almost.service": 4, "voyn-crm.service": 2}),
    )
    units = {
        "exactly.service": infra_monitor.UnitState(5, "active"),
        "almost.service": infra_monitor.UnitState(4, "active"),
        "voyn-crm.service": infra_monitor.UnitState(2, "active"),
    }
    snapshot = infra_monitor.evaluate_unit_health(
        units, crash_loop_restarts=THRESHOLD, history=history, window_seconds=WINDOW
    )
    assert snapshot.crash_loops == (("exactly.service", 5),)
    assert snapshot.failed_units == ()

    quiet = infra_monitor.evaluate_unit_health(
        {"voyn-crm.service": infra_monitor.UnitState(2, "active")},
        crash_loop_restarts=THRESHOLD,
        history=_history((0.0, {"voyn-crm.service": 2}), (TICK, {"voyn-crm.service": 2})),
        window_seconds=WINDOW,
    )
    assert quiet.crash_loops == () and quiet.failed_units == ()
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=quiet,
    )
    assert report.ok

    with pytest.raises(ValueError):
        infra_monitor.evaluate_unit_health(
            {},
            crash_loop_restarts=0,
            history=infra_monitor.RestartHistory(),
            window_seconds=WINDOW,
        )
    with pytest.raises(ValueError):
        infra_monitor.evaluate_unit_health(
            {},
            crash_loop_restarts=THRESHOLD,
            history=infra_monitor.RestartHistory(),
            window_seconds=0,
        )


def test_the_window_bounds_the_comparison_and_the_file_stays_bounded() -> None:
    history = _history(
        (0.0, {"svc.service": 0}),
        (WINDOW, {"svc.service": 100}),
        (2 * WINDOW, {"svc.service": 130}),
    )

    # The 0.0 sample is outside the window of the last tick, so the gain is
    # measured from the only sample still inside it.
    assert history.gained("svc.service") == 30
    assert len(history.samples["svc.service"]) == 2

    # A unit that vanished from the host leaves the file with it: per-connection
    # launcher instances must not accumulate one entry per launch forever.
    after = history.observe({"other.service": 1}, now=3 * WINDOW, window_seconds=WINDOW)
    assert set(after.samples) == {"other.service"}

    capped = infra_monitor.RestartHistory()
    for index in range(infra_monitor.RESTART_SAMPLE_CAP + 50):
        capped = capped.observe(
            {"svc.service": index}, now=float(index), window_seconds=WINDOW
        )
    assert len(capped.samples["svc.service"]) == infra_monitor.RESTART_SAMPLE_CAP


def test_unit_health_probe_failure_fails_closed() -> None:
    snapshot = infra_monitor.UnitHealthSnapshot(
        crash_loops=(), failed_units=(), error="RuntimeError: systemctl failed"
    )
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert any(f.startswith("unit_health_probe_failed:") for f in report.failures)


def _systemctl_stub(counters: dict[str, int], calls: list[list[str]] | None = None):
    """`systemctl list-units` + `show` over a fixed set of units."""

    def _run(args, **_kwargs):
        if calls is not None:
            calls.append(args)
        if args[1] == "list-units":
            listing = "".join(f"  {unit} loaded active running D\n" for unit in counters)
            return subprocess.CompletedProcess(args, 0, stdout=listing, stderr="")
        assert args[1] == "show"
        requested = args[args.index("--") + 1 :]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="\n".join(
                _show_block(unit, counters[unit], "activating") for unit in requested
            ),
            stderr="",
        )

    return _run


def test_the_probe_remembers_the_previous_tick_through_its_state_file(
    monkeypatch, tmp_path
) -> None:
    state = tmp_path / "state" / "unit-restarts.json"

    monkeypatch.setattr(
        infra_monitor.subprocess, "run", _systemctl_stub({"ollama.service": 315851})
    )
    first = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD, state_path=state, window_seconds=WINDOW, now=0.0
    )
    assert first.error is None and first.crash_loops == ()
    assert json.loads(state.read_text())["units"]["ollama.service"] == [[0.0, 315851]]

    monkeypatch.setattr(
        infra_monitor.subprocess, "run", _systemctl_stub({"ollama.service": 315891})
    )
    second = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD, state_path=state, window_seconds=WINDOW, now=TICK
    )

    assert second.crash_loops == (("ollama.service", 40),)
    assert second.window_seconds == WINDOW


def test_a_corrupt_state_file_costs_one_window_but_an_unwritable_one_fails_closed(
    monkeypatch, tmp_path
) -> None:
    state = tmp_path / "unit-restarts.json"
    state.write_text("{not json")
    monkeypatch.setattr(
        infra_monitor.subprocess, "run", _systemctl_stub({"ollama.service": 315891})
    )

    # Tolerated: this tick rewrites the file, so the next one has a baseline.
    recovered = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD, state_path=state, window_seconds=WINDOW, now=0.0
    )
    assert recovered.error is None
    assert json.loads(state.read_text())["units"]["ollama.service"] == [[0.0, 315891]]

    # Not tolerated: a probe that cannot leave a baseline behind would report
    # "no crash loops" on every future tick and never know it was blind.
    blocked = tmp_path / "read-only" / "unit-restarts.json"
    blocked.parent.mkdir()
    blocked.parent.chmod(0o500)
    try:
        snapshot = infra_monitor.read_unit_health_snapshot(
            crash_loop_restarts=THRESHOLD, state_path=blocked, window_seconds=WINDOW, now=0.0
        )
    finally:
        blocked.parent.chmod(0o700)
    assert snapshot.error is not None and snapshot.crash_loops == ()
    report = infra_monitor.evaluate(
        {}, None, minimum_active_workers=0, max_stalled_seconds=900,
        prometheus_ready=True, unit_health=snapshot,
    )
    assert any(f.startswith("unit_health_probe_failed:") for f in report.failures)


def test_unit_health_without_a_state_path_is_a_usage_error(monkeypatch) -> None:
    """A probe that cannot remember the previous tick cannot measure a rate:
    argparse says so at startup instead of reporting a confident "no loops"."""
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    with pytest.raises(SystemExit):
        infra_monitor.parse_args(["--prometheus-url", "http://m/ready", "--unit-health"])

    # systemd's StateDirectory= is how the unit turns it on; the list form is
    # what systemd actually exports.
    monkeypatch.setenv("STATE_DIRECTORY", "/var/lib/voyn-infra-monitor:/var/lib/other")
    assert (
        infra_monitor.default_crash_loop_state()
        == "/var/lib/voyn-infra-monitor/unit-restarts.json"
    )


def test_the_monitor_unit_gives_the_crash_loop_probe_somewhere_to_remember() -> None:
    """ProtectSystem=strict, ProtectHome=read-only and PrivateTmp leave the
    tick no writable path at all without this."""
    unit = Path("deploy/systemd/voyn-infra-monitor.service").read_text()

    assert "StateDirectory=voyn-infra-monitor" in unit
    assert "--unit-health" in unit
    assert "--crash-loop-restarts 5" in unit


def test_unit_health_snapshot_asks_systemctl_for_every_service_and_never_raises(
    monkeypatch, tmp_path
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

    state = tmp_path / "unit-restarts.json"
    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    snapshot = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD, state_path=state, window_seconds=WINDOW
    )
    assert snapshot.error is None
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
    failed = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD, state_path=state, window_seconds=WINDOW
    )
    assert failed.error is not None and "no systemctl" in failed.error


def test_a_unit_that_show_silently_drops_is_a_failed_measurement(
    monkeypatch, tmp_path
) -> None:
    def _run(args, **_kwargs):
        if args[1] == "list-units":
            return subprocess.CompletedProcess(args, 0, stdout=LIST_UNITS_REAL, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=SHOW_HEALTHY, stderr="")

    monkeypatch.setattr(infra_monitor.subprocess, "run", _run)
    snapshot = infra_monitor.read_unit_health_snapshot(
        crash_loop_restarts=THRESHOLD,
        state_path=tmp_path / "unit-restarts.json",
        window_seconds=WINDOW,
    )
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


def test_deploy_lag_snapshot_dates_the_oldest_undeployed_commit(monkeypatch) -> None:
    calls: list[list[str]] = []
    compare = (
        '{"status": "ahead", "commits": ['
        '{"sha": "c1", "commit": {"committer": {"date": "2026-09-13T16:00:00Z"}}},'
        '{"sha": "c2", "commit": {"committer": {"date": "2026-09-14T12:00:00Z"}}}]}'
    )
    monkeypatch.setattr(
        infra_monitor.subprocess, "run",
        _gh_stub({
            "repos/voyn88/voyn-logistics-crm/commits/main": HEAD_JSON,
            "repos/voyn88/voyn-logistics-crm/compare/" + "b" * 40 + "..." + "a" * 40: compare,
        }, calls),
    )
    monkeypatch.setattr(infra_monitor, "_fetch_json", lambda url, timeout=5: {"release_sha": "b" * 40})
    # now = 2026-09-14T13:00:00Z: the head is 1 h old, the oldest undeployed commit 21 h.
    monkeypatch.setattr(infra_monitor.time, "time", lambda: 1789390800.0)
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


def test_a_half_configured_deploy_lag_probe_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        infra_monitor.parse_args(
            ["--prometheus-url", "http://m/ready", "--deploy-lag-repo", "voyn88/x"]
        )
    with pytest.raises(SystemExit):
        infra_monitor.parse_args(["--prometheus-url", "http://m/ready", "--crash-loop-restarts", "0"])


def test_main_reports_the_host_probes_when_enabled(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(infra_monitor, "prometheus_is_ready", lambda _url: True)
    seen: dict[str, object] = {}

    def _unit_health(*, crash_loop_restarts, state_path, window_seconds):
        seen.update(
            restarts=crash_loop_restarts, state=state_path, window=window_seconds
        )
        return infra_monitor.UnitHealthSnapshot(
            crash_loops=(("ollama.service", 40),),
            failed_units=(),
            window_seconds=window_seconds,
        )

    monkeypatch.setattr(infra_monitor, "read_unit_health_snapshot", _unit_health)
    monkeypatch.setattr(
        infra_monitor, "read_deploy_lag_snapshot",
        lambda repo, url, branch, grace_seconds: _lag(repo=repo, branch=branch, grace_seconds=grace_seconds),
    )
    result = infra_monitor.main(
        [
            "--skip-workers", "--skip-queue", "--minimum-active-workers", "0",
            "--prometheus-url", "http://m/ready",
            "--unit-health",
            "--crash-loop-state", str(tmp_path / "unit-restarts.json"),
            "--deploy-lag-repo", "voyn88/voyn-logistics-crm",
            "--deploy-lag-version-url", "http://127.0.0.1:8089/version",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert seen == {
        "restarts": 5,
        "state": Path(tmp_path / "unit-restarts.json"),
        "window": 3600.0,
    }
    assert payload["unit_health"]["crash_loops"] == [["ollama.service", 40]]
    assert payload["unit_health"]["window_seconds"] == 3600.0
    assert payload["deploy_lag"]["lagging"] is True
    assert payload["deploy_lag"]["undeployed_commits"] == 6
    assert any(f.startswith("crash_loop:") for f in payload["failures"])
    assert any(f.startswith("deploy_lag:") for f in payload["failures"])
    assert result == 1
