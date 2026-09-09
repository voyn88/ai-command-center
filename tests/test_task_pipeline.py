"""Unit coverage for the desktop autopilot service (`command_center.task_pipeline`).

Covers AICC-DESKTOP-018: persistent settings, READY-entry -> `scheduler.WorkItem`
adaptation, deterministic ordering, capacity, retry history, decision-to-entry
mapping, the auto-merge opt-in, and the verified-merge -> Kanban `Done`
projection. Concurrency lives in `test_task_pipeline_concurrency.py`; the full
fake-Claude scenario lives in `test_task_pipeline_e2e.py`.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest

from command_center import (
    execution_queue,
    models,
    pipeline_settings,
    task_pipeline,
    tasks_repository,
)
from command_center.pipeline_settings import PipelineSettings
from command_center.runtime import api as runtime_api
from command_center.runtime import completion as completion_domain
from command_center.runtime import db as runtime_db
from command_center.runtime.completion import CompletionPolicy


def _task(**overrides):
    task = {
        "id": overrides.get("id", "t"),
        "project": "AIOS",
        "title": "Task",
        "status": "Backlog",
        "priority": "Medium",
        "depends_on": [],
    }
    task.update(models.default_task_execution_fields())
    task.update(models.default_task_workflow_fields())
    task.update(overrides)
    return task


def _entry(entry_id: str, task_id: str, *, added_at: str = "2026-07-01T00:00:00", state=None) -> dict:
    return {
        "id": entry_id,
        "task_id": task_id,
        "project": "AIOS",
        "state": state or execution_queue.STATE_READY,
        "reason": None,
        "run_id": None,
        "added_at": added_at,
        "evaluated_at": None,
        "launched_at": None,
    }


@pytest.fixture
def api(tmp_path):
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    yield api
    _drain_runs(api)


def _drain_runs(api) -> None:
    """Let every run this test started reach a terminal state before the
    autouse isolation fixtures are torn down.

    A `Supervisor` run is finalized on a background reader thread that writes a
    report through `runtime.reports.REPORTS_ROOT`. If the test returns while a
    fake-`claude` process is still sleeping, that thread finalizes *after*
    `isolated_reports_dir` has restored the real constant, and the report lands
    in the developer's actual `reports/` directory (exactly the contamination
    `conftest`'s marker scan exists to catch)."""
    import contextlib

    for run in api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES):
        with contextlib.suppress(Exception):
            api.request_cancel(run["id"], confirmed=True, grace_seconds=0.2)
    for run in api.list_runs():
        with contextlib.suppress(Exception):
            api.supervisor.wait_for_run(run["id"], timeout=20)


# --------------------------------------------------------------------------
# AICC-DESKTOP-004 — persistent opt-in settings
# --------------------------------------------------------------------------


def test_settings_default_to_everything_off(tmp_path):
    settings = pipeline_settings.load_settings(tmp_path)
    assert settings.enabled is False
    assert settings.auto_launch is False
    assert settings.auto_merge_after_checks is False
    assert settings.auto_launch_active is False
    assert settings.auto_merge_active is False


def test_settings_missing_file_resolves_to_defaults(tmp_path):
    assert pipeline_settings.load_settings(tmp_path) == PipelineSettings()


def test_settings_malformed_file_resolves_to_the_most_restrictive_document(tmp_path):
    """Still total, still nothing enabled — but no longer the plain defaults.

    A malformed file is a *failure*, not a fresh install, and the defaults
    answer it more permissively than the file they stand in for: `0.0` means no
    spend cap and `2` is above an operator's configured `1`. See
    `test_load_settings_substitutes_the_most_restrictive_document_not_the_defaults`.
    """
    path = pipeline_settings.settings_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    settings = pipeline_settings.load_settings(tmp_path)

    # Compared through `as_dict` because NaN != NaN makes dataclass equality
    # useless on the substituted ceiling — and that ceiling is the point.
    assert settings.as_dict() == pipeline_settings.stopped_settings().as_dict()
    assert math.isnan(settings.max_daily_spend_usd)
    assert settings.enabled is False
    assert settings.auto_launch_active is False
    assert settings.auto_merge_active is False


def test_settings_wrong_types_fall_back_per_field_never_enabling():
    settings = PipelineSettings.from_dict(
        {"enabled": "yes", "auto_launch": 1, "auto_merge_after_checks": "true", "max_global_concurrency": "5"}
    )
    assert settings.enabled is False
    assert settings.auto_launch is False
    assert settings.auto_merge_after_checks is False
    assert settings.max_global_concurrency == pipeline_settings.DEFAULT_MAX_GLOBAL_CONCURRENCY


def test_out_of_range_concurrency_falls_back_to_the_default():
    settings = PipelineSettings.from_dict(
        {"max_global_concurrency": 10_000, "max_agent_concurrency": -3}
    )
    assert settings.max_global_concurrency == pipeline_settings.DEFAULT_MAX_GLOBAL_CONCURRENCY
    assert settings.max_agent_concurrency == pipeline_settings.DEFAULT_MAX_AGENT_CONCURRENCY


def test_an_unusable_spend_ceiling_is_carried_as_nan_not_as_no_ceiling():
    """A corrupt ceiling must not read as "no ceiling configured".

    Every spend gate is written as `max_daily_spend_usd > 0`, and `0.0` is the
    documented "no cap" value — so falling back to the default here is not the
    conservative answer it is for the concurrency caps, it is the *most
    permissive one available*. NaN, `inf` and a wrongly-typed or out-of-range
    value all had to become something no `>` test silently accepts, which is
    what `_spend_ceiling` carries them as.
    """
    for bad in (
        float("nan"), float("inf"), float("-inf"),  # non-finite
        "5.0", "abc", None, True,                   # wrongly typed
        -5, 20_000,                                 # out of range
    ):
        settings = PipelineSettings.from_dict({"max_daily_spend_usd": bad})
        assert math.isnan(settings.max_daily_spend_usd), bad
        # The property that matters downstream: it is not readable as "unset".
        assert not settings.max_daily_spend_usd > 0, bad

    # Control 1: a finite in-range ceiling is still accepted unchanged, so the
    # guard above cannot be passing by rejecting everything.
    assert PipelineSettings.from_dict({"max_daily_spend_usd": 7.5}).max_daily_spend_usd == 7.5

    # Control 2: an *absent* key is genuinely "no cap configured", not
    # corruption — a fresh install with no settings file must still dispatch.
    assert PipelineSettings.from_dict({}).max_daily_spend_usd == 0.0
    # ...and an explicit 0.0 is the same deliberate "off".
    assert PipelineSettings.from_dict({"max_daily_spend_usd": 0.0}).max_daily_spend_usd == 0.0


def test_an_unusable_spend_ceiling_survives_a_save_load_round_trip(tmp_path):
    """The refusal must outlive persistence.

    `as_dict` cannot write NaN verbatim (`json.dump` emits bare `NaN`, which
    `JSON.parse` rejects), so it degrades to null — and null has to read back
    as *unusable*, not as "unset". Otherwise saving any unrelated setting once
    would launder a corrupt ceiling into an absent one and quietly restore the
    unlimited spend this gate exists to refuse.
    """
    corrupt = PipelineSettings.from_dict({"max_daily_spend_usd": "5.0"})
    assert math.isnan(corrupt.max_daily_spend_usd)

    assert corrupt.as_dict()["max_daily_spend_usd"] is None  # valid JSON
    reloaded = PipelineSettings.from_dict(corrupt.as_dict())
    assert math.isnan(reloaded.max_daily_spend_usd)

    # Through the real file, the way `save_settings`/`load_settings` do it.
    pipeline_settings.save_settings(tmp_path, corrupt)
    assert "NaN" not in pipeline_settings.settings_file_path(tmp_path).read_text()
    assert math.isnan(pipeline_settings.load_settings(tmp_path).max_daily_spend_usd)

    # Control: a healthy ceiling round-trips as itself.
    pipeline_settings.save_settings(tmp_path, PipelineSettings(max_daily_spend_usd=7.5))
    assert pipeline_settings.load_settings(tmp_path).max_daily_spend_usd == 7.5


def test_editing_an_unrelated_setting_cannot_launder_a_corrupt_ceiling(tmp_path):
    """Toggling one switch must not quietly restore unlimited spend.

    `update_settings` re-reads the file and merges through `from_dict`, so a
    corrupt ceiling passes through that merge on every unrelated edit. If it
    were laundered into `0.0` there, the *recoverable* corrupt file would
    become a permanently cap-free one — with an operator's name stamped on the
    write that did it. Same failure mode `update_policy` refuses for the
    dispatch policy's limit maps.
    """
    path = pipeline_settings.settings_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "max_daily_spend_usd": "5.0"}))
    assert math.isnan(pipeline_settings.load_settings(tmp_path).max_daily_spend_usd)

    updated = pipeline_settings.update_settings(
        tmp_path, actor="operator", auto_launch=True
    )

    assert updated.auto_launch is True  # the intended edit landed
    assert math.isnan(updated.max_daily_spend_usd)
    assert math.isnan(pipeline_settings.load_settings(tmp_path).max_daily_spend_usd)


def test_an_unreadable_settings_file_is_told_apart_from_an_absent_one(tmp_path):
    """`storage.read_json` collapses "absent", "malformed" and "unreadable"
    into one value, and the two ends of that collapse need opposite answers.

    Absent is a fresh install: the all-off defaults are exactly right.
    Unreadable is a failure, and the same defaults get it wrong in *both*
    directions at once — the switches decay to off (safe, but reported as an
    operator decision nobody made) while `max_daily_spend_usd` decays to
    `0.0`, which means no cap at all.
    """
    path = pipeline_settings.settings_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Absent: still the defaults, no exception. A fresh install must work.
    assert pipeline_settings.read_settings_document(tmp_path) == {}

    for raw in ('{"enabled": tru', "", "   ", "[]", '"a string"', "null", "42"):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(pipeline_settings.UnreadableSettings):
            pipeline_settings.read_settings_document(tmp_path)

    # Control: a well-formed document is returned verbatim.
    path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert pipeline_settings.read_settings_document(tmp_path) == {"enabled": True}


def test_load_settings_stays_total_on_an_unreadable_file(tmp_path):
    """The display surfaces and the pipeline tick keep the reader they have.

    `load_settings` is deliberately total — it must not start raising into
    `app.py` and the UI panels. What it cannot do is tell its caller *why*
    everything is off, which is why the two callers for which that is
    load-bearing read the document directly.
    """
    path = pipeline_settings.settings_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"enabled": tru', encoding="utf-8")

    settings = pipeline_settings.load_settings(tmp_path)

    assert settings.enabled is False  # fail closed, as before
    assert settings.auto_launch_active is False


def test_toggling_a_switch_on_a_torn_file_cannot_delete_the_spend_ceiling(tmp_path):
    """The measured fail-open chain, pinned end to end.

    An operator running `enabled=True, max_daily_spend_usd=5.0,
    max_global_concurrency=1` suffers a torn write. Every reader now sees the
    all-off defaults, so the dispatch plan used to report `kill_switch_engaged`
    — and the remedy that names is "turn the master switch back on". Doing so
    merged that one change onto the laundered defaults and *persisted*
    `max_daily_spend_usd: 0.0` (no cap) and `max_global_concurrency: 2`,
    stamped `updated_by` with the operator's own id, as though they had asked
    for it. No corrupt data survived anywhere: the guardrails were simply gone,
    and every gate read healthy from then on.

    So the write is refused. This panel-and-API-facing mutator cannot restate
    the ceiling — nothing passes `max_daily_spend_usd` to it — so it must not
    inherit an unreadable one.
    """
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True, auto_launch=True, max_daily_spend_usd=5.0,
            max_global_concurrency=1,
        ),
    )
    path = pipeline_settings.settings_file_path(tmp_path)
    before = path.read_text(encoding="utf-8")

    path.write_text('{"enabled": true, "max_daily_spend_us', encoding="utf-8")

    with pytest.raises(pipeline_settings.UnreadableSettings):
        pipeline_settings.update_settings(tmp_path, actor="operator", enabled=True)

    # Nothing was written: the torn file is still torn, which is recoverable.
    # A laundered one would not have been.
    assert path.read_text(encoding="utf-8") == '{"enabled": true, "max_daily_spend_us'

    # And the documented remedy works: `save_settings` states the whole
    # document explicitly rather than inheriting the unreadable part.
    pipeline_settings.save_settings(
        tmp_path, PipelineSettings.from_dict(json.loads(before))
    )
    restored = pipeline_settings.load_settings(tmp_path)
    assert restored.max_daily_spend_usd == 5.0
    assert restored.max_global_concurrency == 1


def test_a_settings_file_that_is_not_utf8_is_typed_unreadable(tmp_path):
    """`read_text` raises `UnicodeDecodeError` — a `ValueError`, not an
    `OSError` — so this file used to escape `read_settings_document` untyped.

    That matters because *every* caller handles the typed exception and none
    handles the bare one: `load_settings` promises to be total, the autopilot
    panel's write guard catches `UnreadableSettings` to refuse the write, and
    `kill_switch` catches it to replace the document. A torn write that cuts a
    multi-byte sequence in half is exactly this file, so the shape is not
    exotic — it is one of the likelier ways the file breaks.
    """
    path = pipeline_settings.settings_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"enabled": true, "max_daily_spend_usd": 5.\xff\xfe')

    with pytest.raises(pipeline_settings.UnreadableSettings):
        pipeline_settings.read_settings_document(tmp_path)

    # And the total reader stays total rather than raising into the UI.
    assert pipeline_settings.load_settings(tmp_path).enabled is False


def test_load_settings_substitutes_the_most_restrictive_document_not_the_defaults(
    tmp_path,
):
    """Staying total is not a licence to answer more permissively than the file
    that could not be read.

    The plain defaults are the right answer to "nothing configured yet" and the
    wrong one here, because four of their fields are at the *permissive* end:
    the spend ceiling reads `0.0` (no cap), both concurrency caps read `2` —
    above an operator who configured `1`, and read straight into
    `db.create_run`'s atomic global cap — the review gate reads open, and the
    two attempt budgets authorise relaunches, hence spend, that a configured
    `0`/`1` did not.
    """
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True,
            max_daily_spend_usd=5.0,
            max_global_concurrency=1,
            max_agent_concurrency=1,
            require_independent_review=True,
            max_rework_attempts=0,
            max_run_attempts=1,
        ),
    )
    pipeline_settings.settings_file_path(tmp_path).write_text(
        '{"enabled": true, "max_daily', encoding="utf-8"
    )

    substituted = pipeline_settings.load_settings(tmp_path)

    # Nothing is permitted that the unreadable document might have forbidden.
    assert substituted.enabled is False
    assert substituted.auto_launch_active is False
    assert math.isnan(substituted.max_daily_spend_usd)  # never 0.0 == no cap
    assert substituted.max_global_concurrency == pipeline_settings.MIN_CONCURRENCY
    assert substituted.max_agent_concurrency == pipeline_settings.MIN_CONCURRENCY
    assert substituted.require_independent_review is True
    assert substituted.max_rework_attempts == pipeline_settings.MIN_REWORK_ATTEMPTS
    assert substituted.max_run_attempts == pipeline_settings.MIN_RUN_ATTEMPTS
    # The defaults would have loosened three of those on the way past.
    defaults = PipelineSettings()
    assert defaults.max_global_concurrency > substituted.max_global_concurrency
    assert defaults.max_daily_spend_usd == 0.0
    assert defaults.require_independent_review is False

    # A *missing* file is still a fresh install, not a failure: plain defaults.
    pipeline_settings.settings_file_path(tmp_path).unlink()
    assert pipeline_settings.load_settings(tmp_path) == PipelineSettings()


class _NoRunsApi:
    """The narrowest thing `kill_switch` needs: something to ask for runs."""

    def __init__(self, db_path):
        self.db_path = db_path

    def list_runs(self):
        return []


def test_kill_switch_persists_the_stop_when_the_settings_are_unreadable(tmp_path):
    """The measured fail-open: an emergency stop that stopped nothing.

    `kill_switch` read the settings through the *total* reader, so a torn file
    arrived as `enabled=False`, the `if settings.enabled` write was skipped —
    and the report still said `disabled: True`. Nothing was persisted. The
    moment the file read again (a permissions blip restored, a half-written
    file completed by its writer), `enabled` was still `True` and the next tick
    launched, with the operator believing the machine was stopped.

    Refusing to write is the fail-closed answer for `update_settings`, whose
    write would *inherit* the unreadable file and delete guardrails. Here it is
    the fail-open one: it leaves automation armed. So this caller overwrites —
    with `stopped_settings()`, so the replacement cannot raise a ceiling
    either, and after preserving the bytes it is about to destroy.
    """
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(enabled=True, auto_launch=True, max_daily_spend_usd=5.0),
    )
    path = pipeline_settings.settings_file_path(tmp_path)
    path.write_text('{"enabled": true, "max_daily_spend_us', encoding="utf-8")

    report = task_pipeline.kill_switch(
        tmp_path, _NoRunsApi(tmp_path / "runtime.db"), confirmed=True
    )

    assert report["disabled"] is True
    # The stop is on disk, not merely claimed in the report.
    persisted = pipeline_settings.read_settings_document(tmp_path)
    assert persisted["enabled"] is False
    assert persisted["auto_launch"] is False
    # And the replacement did not launder the ceiling into "no cap": null reads
    # back as unusable, which keeps dispatch refusing after a re-enable.
    assert persisted["max_daily_spend_usd"] is None
    assert math.isnan(pipeline_settings.load_settings(tmp_path).max_daily_spend_usd)

    # The operator is told their configuration was replaced, and where the
    # bytes went — nothing else can reconstruct the ceiling they held.
    assert report["settings_unreadable"]
    quarantined = pathlib.Path(report["settings_quarantined_to"])
    assert quarantined.read_text(encoding="utf-8") == (
        '{"enabled": true, "max_daily_spend_us'
    )

    # A later tick, in this process or any other, launches nothing.
    assert pipeline_settings.load_settings(tmp_path).auto_launch_active is False


def test_kill_switch_still_cancels_when_the_replacement_write_fails(tmp_path, monkeypatch):
    """The two effects are independent, so one failing must not take the other.

    A directory whose settings file cannot be read often cannot be written
    either. Cancelling the live runs does not depend on that file at all and is
    the more urgent half, so it proceeds — and the report says `disabled:
    False`, because nothing was persisted and the next tick is still free to
    launch. Claiming `True` here would be the same false report this test's
    sibling exists to remove, one branch over.
    """
    pipeline_settings.save_settings(
        tmp_path, PipelineSettings(enabled=True, auto_launch=True)
    )
    pipeline_settings.settings_file_path(tmp_path).write_text("{torn", encoding="utf-8")

    def _no_write(*_args, **_kwargs):
        raise PermissionError("read-only data directory")

    monkeypatch.setattr(pipeline_settings, "save_settings", _no_write)

    class _OneRunningApi:
        def __init__(self):
            self.cancelled = []

        def list_runs(self):
            return [{"id": "r1", "state": "RUNNING"}]

        def request_cancel(self, run_id, *, confirmed):
            assert confirmed is True
            self.cancelled.append(run_id)

    api = _OneRunningApi()
    report = task_pipeline.kill_switch(tmp_path, api, confirmed=True)

    assert api.cancelled == ["r1"]  # the urgent half happened
    assert report["cancelled"] == ["r1"]
    assert report["disabled"] is False  # and the report does not pretend
    assert "read-only data directory" in report["settings_write_error"]
    assert report["settings_unreadable"]


def test_kill_switch_leaves_a_readable_settings_document_otherwise_intact(tmp_path):
    """The healthy path is unchanged: the master switch goes off and every
    other field the operator set is preserved, including the spend ceiling."""
    pipeline_settings.save_settings(
        tmp_path,
        PipelineSettings(
            enabled=True,
            auto_launch=True,
            max_daily_spend_usd=5.0,
            max_global_concurrency=1,
        ),
    )

    report = task_pipeline.kill_switch(
        tmp_path, _NoRunsApi(tmp_path / "runtime.db"), confirmed=True
    )

    after = pipeline_settings.load_settings(tmp_path)
    assert after.enabled is False
    assert after.max_daily_spend_usd == 5.0
    assert after.max_global_concurrency == 1
    assert report["settings_unreadable"] is None
    assert report["settings_quarantined_to"] is None
    assert report["settings_write_error"] is None
    assert report["disabled"] is True
    assert not pipeline_settings.settings_file_path(tmp_path).with_suffix(
        ".json" + pipeline_settings.UNREADABLE_SUFFIX
    ).exists()


def test_update_settings_still_works_on_a_healthy_and_on_an_absent_file(tmp_path):
    """The refusal must not cost the ordinary path anything."""
    # Absent file: a fresh install can still opt in.
    first = pipeline_settings.update_settings(tmp_path, actor="op", enabled=True)
    assert first.enabled is True

    # Healthy file: an unrelated edit preserves the configured ceiling.
    import dataclasses

    pipeline_settings.save_settings(
        tmp_path, dataclasses.replace(first, max_daily_spend_usd=5.0)
    )
    second = pipeline_settings.update_settings(tmp_path, actor="op", auto_launch=True)
    assert second.auto_launch is True
    assert second.max_daily_spend_usd == 5.0


def test_a_lone_auto_launch_flag_cannot_launch_without_the_master_switch():
    settings = PipelineSettings(enabled=False, auto_launch=True, auto_merge_after_checks=True)
    assert settings.auto_launch_active is False
    assert settings.auto_merge_active is False


def test_save_and_load_settings_round_trips(tmp_path):
    saved = pipeline_settings.save_settings(
        tmp_path, PipelineSettings(enabled=True, auto_launch=True, max_global_concurrency=4)
    )
    assert saved.auto_launch_active is True
    loaded = pipeline_settings.load_settings(tmp_path)
    assert loaded == saved
    assert loaded.max_global_concurrency == 4


def test_update_settings_preserves_untouched_fields_and_stamps_the_actor(tmp_path):
    pipeline_settings.save_settings(tmp_path, PipelineSettings(enabled=True, max_agent_concurrency=3))
    updated = pipeline_settings.update_settings(tmp_path, actor="operator", auto_launch=True)
    assert updated.enabled is True
    assert updated.auto_launch is True
    assert updated.max_agent_concurrency == 3
    assert updated.updated_by == "operator"
    assert updated.updated_at


def test_update_settings_rejects_unknown_key(tmp_path):
    with pytest.raises(TypeError, match="Unknown pipeline setting"):
        pipeline_settings.update_settings(tmp_path, autolaunch=True)


def test_persisted_settings_are_json_round_trippable(tmp_path):
    pipeline_settings.save_settings(tmp_path, PipelineSettings(enabled=True, max_agent_concurrency=3))
    on_disk = json.loads(pipeline_settings.settings_file_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True
    assert on_disk["max_agent_concurrency"] == 3


def test_disabled_tick_does_nothing(tmp_path, api):
    result = task_pipeline.tick(tmp_path, api, {})
    assert result.ran is False
    assert result.status == task_pipeline.TICK_DISABLED
    assert result.decisions == ()
    # No queue file is created by a disabled tick.
    assert not execution_queue.queue_file_path(tmp_path).exists()


# --------------------------------------------------------------------------
# AICC-DESKTOP-002 — READY entry -> scheduler.WorkItem adaptation
# --------------------------------------------------------------------------


def test_adapts_ready_entry_with_clean_workspace(tmp_path, git_repo, api):
    task = _task(id="a", workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")], {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, db_path=api.db_path
    )
    assert len(wave.work_items) == 1
    item = wave.work_items[0]
    assert item.task_id == "a"
    assert item.workspace == str(git_repo.resolve())
    assert item.dependencies_met is True
    assert item.attempts_made == 0
    assert wave.entry_by_task == {"a": "q1"}
    assert wave.skipped == ()


def test_entry_for_missing_task_is_skipped_with_reason(tmp_path, api):
    wave = task_pipeline.adapt_ready_entries([_entry("q1", "ghost")], {}, {}, db_path=api.db_path)
    assert wave.work_items == ()
    assert [(s.action, s.reason_code) for s in wave.skipped] == [
        (task_pipeline.ACTION_SKIPPED, task_pipeline.REASON_TASK_MISSING)
    ]
    assert wave.skipped[0].remediation


def test_entry_without_workspace_is_skipped_with_reason(tmp_path, api):
    task = _task(id="a", workspace_path=None)
    wave = task_pipeline.adapt_ready_entries([_entry("q1", "a")], {"a": task}, {}, db_path=api.db_path)
    assert wave.work_items == ()
    assert wave.skipped[0].reason_code == task_pipeline.REASON_WORKSPACE_UNCONFIGURED


def test_dirty_workspace_is_skipped_and_never_consumes_a_capacity_slot(tmp_path, git_repo, api):
    (git_repo / "untracked.txt").write_text("x")
    task = _task(id="a", workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")], {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, db_path=api.db_path
    )
    assert wave.work_items == ()
    skipped = wave.skipped[0]
    assert skipped.reason_code == task_pipeline.REASON_NEEDS_CONFIRMATION
    assert skipped.warnings  # the exact validate_launch warnings, for the UI
    assert "вручную" in skipped.remediation


def test_blocked_workspace_is_skipped_with_fatal_messages(tmp_path, api):
    task = _task(id="a", workspace_path=str(tmp_path / "not-a-repo"))
    wave = task_pipeline.adapt_ready_entries([_entry("q1", "a")], {"a": task}, {}, db_path=api.db_path)
    assert wave.work_items == ()
    assert wave.skipped[0].reason_code == task_pipeline.REASON_LAUNCH_BLOCKED
    assert wave.skipped[0].explanation


def test_second_entry_for_the_same_task_is_skipped_as_duplicate(tmp_path, git_repo, api):
    task = _task(id="a", workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q2", "a", added_at="2026-07-02T00:00:00"), _entry("q1", "a", added_at="2026-07-01T00:00:00")],
        {"a": task},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    assert len(wave.work_items) == 1
    # The older entry wins deterministically regardless of input order.
    assert wave.entry_by_task == {"a": "q1"}
    assert [s.entry_id for s in wave.skipped] == ["q2"]
    assert wave.skipped[0].reason_code == task_pipeline.REASON_DUPLICATE_QUEUE_ENTRY


def test_ready_entry_is_skipped_while_completion_awaits_merge(tmp_path, git_repo, api):
    task = _task(id="a", workspace_path=str(git_repo))
    _seed_completion(
        api,
        git_repo,
        task_id="a",
        completion_state=completion_domain.CompletionState.AWAITING_MERGE,
    )

    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")],
        {"a": task},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )

    assert wave.work_items == ()
    assert wave.skipped[0].reason_code == task_pipeline.REASON_COMPLETION_IN_PROGRESS
    assert "AWAITING_MERGE" in wave.skipped[0].explanation
    assert "merge" in wave.skipped[0].remediation


def test_adaptation_order_is_stable_under_input_reversal(tmp_path, git_repo, api):
    repo_b = git_repo.parent / "repo-b"
    repo_b.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo_b, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo_b, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_b, check=True)
    (repo_b / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo_b, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_b, check=True)

    tasks = {
        "a": _task(id="a", workspace_path=str(git_repo)),
        "b": _task(id="b", workspace_path=str(repo_b)),
    }
    entries = [_entry("q1", "a", added_at="2026-07-01T00:00:00"), _entry("q2", "b", added_at="2026-07-02T00:00:00")]
    cfg = {"AIOS": {"repository_path": str(git_repo)}}
    forward = task_pipeline.adapt_ready_entries(entries, tasks, cfg, db_path=api.db_path)
    reverse = task_pipeline.adapt_ready_entries(list(reversed(entries)), tasks, cfg, db_path=api.db_path)
    assert [w.task_id for w in forward.work_items] == [w.task_id for w in reverse.work_items] == ["a", "b"]


def test_retry_history_is_read_from_terminal_runs(tmp_path, git_repo, api):
    from tests.completion_helpers import seed_completed_run

    run = seed_completed_run(
        api.db_path, repository_path=str(git_repo), branch="feature/x", state="FAILED"
    )
    task = _task(id=run["task_id"], workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", run["task_id"])],
        {run["task_id"]: task},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    item = wave.work_items[0]
    assert item.attempts_made == 1
    assert item.last_state == "FAILED"
    assert item.last_completed_at == "2026-07-22T12:00:00"


def test_active_run_is_not_counted_as_a_completed_attempt(tmp_path, git_repo, api):
    from tests.completion_helpers import seed_completed_run

    run = seed_completed_run(
        api.db_path, repository_path=str(git_repo), branch="feature/x", state="RUNNING"
    )
    task = _task(id=run["task_id"], workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", run["task_id"])],
        {run["task_id"]: task},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    assert wave.work_items[0].attempts_made == 0


def test_stale_ready_entry_for_a_blocked_task_is_not_dependency_met(tmp_path, git_repo, api):
    blocker = _task(id="dep", status="Backlog")
    task = _task(id="a", workspace_path=str(git_repo), depends_on=["dep"])
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")],
        {"a": task, "dep": blocker},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    assert wave.work_items[0].dependencies_met is False


# --------------------------------------------------------------------------
# AICC-DESKTOP-003 — decision -> queue entry mapping
# --------------------------------------------------------------------------


def test_decisions_are_mapped_back_onto_queue_entry_ids(tmp_path, git_repo, api):
    from command_center.runtime import scheduler

    task = _task(id="a", workspace_path=str(git_repo), title="Заголовок")
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")], {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, db_path=api.db_path
    )
    plan = api.plan_schedule(list(wave.work_items), now="2026-07-24T10:00:00")
    decisions = task_pipeline.map_decisions(plan, wave, {"a": task})
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.entry_id == "q1"
    assert decision.action == scheduler.ACTION_ASSIGN
    assert decision.agent_id == "claude_code"
    assert decision.attempt == 1
    assert decision.title == "Заголовок"
    assert decision.workspace == str(git_repo.resolve())
    assert decision.as_dict()["remediation"]


def test_skipped_entries_are_appended_to_the_mapped_decisions(tmp_path, api):
    from command_center.runtime import scheduler

    wave = task_pipeline.adapt_ready_entries([_entry("q1", "ghost")], {}, {}, db_path=api.db_path)
    plan = api.plan_schedule([], now="2026-07-24T10:00:00")
    decisions = task_pipeline.map_decisions(plan, wave, {})
    assert [d.entry_id for d in decisions] == ["q1"]
    assert decisions[0].action not in (
        scheduler.ACTION_ASSIGN,
        scheduler.ACTION_DEFER,
        scheduler.ACTION_BLOCKED,
    )


def test_global_capacity_limits_the_wave_and_explains_the_deferral(tmp_path, git_repo, api):
    from command_center.runtime import scheduler

    repo_b = git_repo.parent / "repo-b"
    repo_b.mkdir()
    import subprocess

    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo_b, check=True)
    (repo_b / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo_b, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_b, check=True)

    tasks = {
        "a": _task(id="a", workspace_path=str(git_repo), priority="Critical"),
        "b": _task(id="b", workspace_path=str(repo_b), priority="Low"),
    }
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a"), _entry("q2", "b")],
        tasks,
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    plan = api.plan_schedule(
        list(wave.work_items),
        config=scheduler.SchedulerConfig(max_global_concurrency=1),
        now="2026-07-24T10:00:00",
    )
    decisions = task_pipeline.map_decisions(plan, wave, tasks)
    by_entry = {d.entry_id: d for d in decisions}
    assert by_entry["q1"].action == scheduler.ACTION_ASSIGN
    assert by_entry["q2"].action == scheduler.ACTION_DEFER
    assert by_entry["q2"].reason_code == scheduler.REASON_GLOBAL_AT_CAPACITY
    assert "лимит" in by_entry["q2"].remediation


def test_every_reason_code_in_the_vocabulary_has_operator_remediation():
    """Every code a decision can carry — the planner's, the queue launcher's,
    and this module's own — must map to actionable advice, or the UI shows a
    bare code with no way forward."""
    from command_center import execution_queue
    from command_center.runtime import scheduler

    codes = {
        value
        for name, value in vars(scheduler).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    codes |= {
        value
        for name, value in vars(execution_queue).items()
        if name.startswith("LAUNCH_") and isinstance(value, str) and not name.endswith(("_FILE_NAME", "_TIMEOUT_SECONDS"))
    }
    codes |= {
        value
        for name, value in vars(task_pipeline).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    missing = codes - set(task_pipeline.REMEDIATION_BY_REASON)
    assert not missing, f"no remediation for: {sorted(missing)}"


# --------------------------------------------------------------------------
# AICC-DESKTOP-010 — auto-merge is an explicit, reversible opt-in
# --------------------------------------------------------------------------


def _seed_completion(
    api,
    git_repo,
    *,
    task_id="a",
    merge_mode=completion_domain.MERGE_MANUAL,
    completion_state=completion_domain.CompletionState.EXECUTION_FINISHED,
):
    # A task may legitimately have several runs (that is what a rework is), so
    # the runtime task row is created once and reused.
    if runtime_db.get_task(api.db_path, task_id) is None:
        runtime_db.create_task(
            api.db_path, project="AIOS", title="T", task_type="implementation", task_id=task_id
        )
    session = runtime_db.create_session(
        api.db_path, task_id=task_id, project="AIOS", repository_path=str(git_repo)
    )
    run = runtime_db.create_run(
        api.db_path,
        session_id=session["id"],
        task_id=task_id,
        project="AIOS",
        task_type="implementation",
        repository_path=str(git_repo),
        prompt="p",
        is_resume=False,
    )
    policy = CompletionPolicy(merge_mode=merge_mode)
    return runtime_db.create_completion(
        api.db_path,
        run_id=run["id"],
        task_id=task_id,
        project="AIOS",
        repository_path=str(git_repo),
        completion_state=completion_state,
        merge_mode=merge_mode,
        policy_json=policy.to_json(),
        last_reason_code="execution_ok",
    )


def test_merge_policy_left_manual_without_the_opt_in(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    updates = task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=False)
    assert updates == []
    fresh = runtime_db.get_completion(api.db_path, row["run_id"])
    assert CompletionPolicy.from_json(fresh["policy_json"]).merge_mode == completion_domain.MERGE_MANUAL


def test_merge_policy_upgraded_only_on_the_explicit_opt_in(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    updates = task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True)
    assert [u["to"] for u in updates] == [completion_domain.MERGE_AUTO_AFTER_CHECKS]
    fresh = runtime_db.get_completion(api.db_path, row["run_id"])
    assert fresh["merge_mode"] == completion_domain.MERGE_AUTO_AFTER_CHECKS
    assert CompletionPolicy.from_json(fresh["policy_json"]).is_auto_merge is True
    events = [e["event_type"] for e in runtime_db.list_completion_events(api.db_path, row["run_id"])]
    assert task_pipeline.EV_MERGE_POLICY_APPLIED in events


def test_enabling_auto_merge_clears_the_manual_wait_timer(tmp_path, git_repo, api):
    row = _seed_completion(
        api,
        git_repo,
        completion_state=completion_domain.CompletionState.AWAITING_MERGE,
    )
    waiting = runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"next_retry_at": "2026-07-28T17:43:59", "retry_count": 14},
    )

    task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True)

    fresh = runtime_db.get_completion(api.db_path, waiting["run_id"])
    assert fresh["merge_mode"] == completion_domain.MERGE_AUTO_AFTER_CHECKS
    assert fresh["next_retry_at"] is None
    assert fresh["retry_count"] == 0


def test_merge_policy_is_idempotent_across_repeated_ticks(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True)
    version_after_first = runtime_db.get_completion(api.db_path, row["run_id"])["version"]
    assert task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True) == []
    assert runtime_db.get_completion(api.db_path, row["run_id"])["version"] == version_after_first


def test_withdrawing_the_opt_in_reverts_the_row_to_manual(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True)
    updates = task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=False)
    assert [u["to"] for u in updates] == [completion_domain.MERGE_MANUAL]
    fresh = runtime_db.get_completion(api.db_path, row["run_id"])
    assert CompletionPolicy.from_json(fresh["policy_json"]).is_auto_merge is False


def test_explicit_stronger_project_policy_is_never_downgraded_by_the_opt_in(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    project_configs = {"AIOS": {"merge_mode": completion_domain.MERGE_AUTO_AFTER_CHECKS_AND_REVIEW}}
    task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, project_configs, opt_in=True)
    fresh = runtime_db.get_completion(api.db_path, row["run_id"])
    policy = CompletionPolicy.from_json(fresh["policy_json"])
    assert policy.merge_mode == completion_domain.MERGE_AUTO_AFTER_CHECKS_AND_REVIEW
    assert policy.requires_review_for_merge is True


def test_terminal_completion_rows_are_never_repolicied(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"completion_state": completion_domain.CompletionState.COMPLETED},
    )
    assert task_pipeline.apply_merge_policy(api.db_path, {"a": _task(id="a")}, {}, opt_in=True) == []


# --------------------------------------------------------------------------
# AICC-DESKTOP-011 — a verified merge is what makes a task Done
# --------------------------------------------------------------------------


def test_local_only_completion_moves_the_task_to_done_without_claiming_merge(
    tmp_path, git_repo, api
):
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"completion_state": completion_domain.CompletionState.COMPLETED},
    )
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])

    moved = task_pipeline.project_verified_completions(tmp_path, db_path=api.db_path)
    assert moved == ["a"]
    persisted = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
    assert persisted["a"]["status"] == "Done"
    assert persisted["a"]["launch_status"] == "Completed"
    assert persisted["a"]["current_stage"] == "Completed Locally"
    assert persisted["a"]["progress"] == 100
    assert persisted["a"].get("pull_request_status") != "merged"


def test_verified_completion_repairs_stale_execution_fields_on_done_task(
    tmp_path, git_repo, api
):
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={
            "completion_state": completion_domain.CompletionState.COMPLETED,
            # A genuinely *verified* completion reaches COMPLETED only after its
            # merge is confirmed in the target branch, so it always carries the
            # merged-PR evidence. Without it this would be an `allow_local_only`
            # completion, which must NOT be labelled "merged" (audit D4).
            "pull_request_url": "https://github.com/x/y/pull/1",
        },
    )
    task = _task(
        id="a",
        workspace_path=str(git_repo),
        status="Done",
        launch_status="Incomplete",
        current_stage="Implementation",
        progress=40,
    )
    tasks_repository.save_tasks(tmp_path, [task])

    moved = task_pipeline.project_verified_completions(tmp_path, db_path=api.db_path)

    assert moved == []
    persisted = tasks_repository.load_tasks(tmp_path)[0]
    assert persisted["status"] == "Done"
    assert persisted["launch_status"] == "Completed"
    assert persisted["current_stage"] == "Merged"
    assert persisted["progress"] == 100
    assert persisted["pull_request_status"] == "merged"


def test_unverified_completion_never_moves_the_task_to_done(tmp_path, git_repo, api):
    _seed_completion(api, git_repo)  # still EXECUTION_FINISHED
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])
    assert task_pipeline.project_verified_completions(tmp_path, db_path=api.db_path) == []
    assert tasks_repository.load_tasks(tmp_path)[0]["status"] != "Done"


def test_projecting_completions_is_idempotent(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"completion_state": completion_domain.CompletionState.COMPLETED},
    )
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])
    assert task_pipeline.project_verified_completions(tmp_path, db_path=api.db_path) == ["a"]
    assert task_pipeline.project_verified_completions(tmp_path, db_path=api.db_path) == []


def test_refresh_sync_projects_verified_completion_without_autopilot(
    tmp_path, git_repo, api
):
    """Audit DATA-D2: a verified (COMPLETED) merge must reach the board on the
    ordinary refresh tick, not only under the default-off autopilot. The refresh
    entry point (`sync_on_refresh`) both reconciles execution state and projects
    verified completions, so a genuinely merged task can never stay stuck in
    Backlog with its dependents blocked."""
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"completion_state": completion_domain.CompletionState.COMPLETED},
    )
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])

    tasks, moved = task_pipeline.sync_on_refresh(tmp_path, api)

    assert moved == ["a"]
    assert {t["id"]: t for t in tasks}["a"]["status"] == "Done"
    # Idempotent: a second refresh tick moves nothing and rewrites nothing.
    _tasks2, moved2 = task_pipeline.sync_on_refresh(tmp_path, api)
    assert moved2 == []


def test_unverified_done_ids_flags_done_without_a_verified_completion(
    tmp_path, git_repo, api
):
    """Audit DATA-D1: a task the board shows as `Done` but with no engine-
    verified (`COMPLETED`) completion — moved there by a manual lane change or a
    verbatim import — is reported as unverified, so the UI can mark it 'not
    verified' instead of rendering it as an indistinguishable real merged result.
    A Done task backed by a COMPLETED completion, and any non-Done task, are
    excluded."""
    row = _seed_completion(api, git_repo)
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={"completion_state": completion_domain.CompletionState.COMPLETED},
    )
    verified = _task(id="a", workspace_path=str(git_repo), status="Done")
    manual = _task(id="manual", status="Done")
    backlog = _task(id="b", status="Backlog")
    tasks_repository.save_tasks(tmp_path, [verified, manual, backlog])

    assert task_pipeline.unverified_done_ids(tmp_path, db_path=api.db_path) == ["manual"]


# --------------------------------------------------------------------------
# Result serialization (the audit trail / UI render model)
# --------------------------------------------------------------------------


def test_tick_result_is_json_serializable(tmp_path, api):
    result = task_pipeline.tick(tmp_path, api, {})
    assert json.loads(json.dumps(result.as_dict(), ensure_ascii=False))["status"] == task_pipeline.TICK_DISABLED


# --------------------------------------------------------------------------
# Rework: a failed validation becomes another attempt, not a dead end
# --------------------------------------------------------------------------


def _failed_validation(api, git_repo, *, task_id="a", summary="pytest: 1 failed"):
    row = _seed_completion(api, git_repo, task_id=task_id)
    return runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={
            "completion_state": completion_domain.CompletionState.VALIDATION_FAILED,
            "validation_summary": summary,
            "recommended_action": "Fix and re-run the task.",
        },
    )


def _rework_settings(**overrides):
    base = dict(enabled=True, auto_launch=True, auto_rework=True, max_rework_attempts=2)
    base.update(overrides)
    return PipelineSettings(**base)


def test_failed_validation_is_requeued_as_a_new_attempt(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo), goal="Сделай штуку")
    tasks_repository.save_tasks(tmp_path, [task])

    records = task_pipeline.plan_rework(
        tmp_path, {"a": task}, db_path=api.db_path, settings=_rework_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REWORK_REQUEUED]

    persisted = tasks_repository.load_tasks(tmp_path)[0]
    assert persisted[task_pipeline.REWORK_COUNT_FIELD] == 1
    assert "Доработка" in persisted["prompt"]
    assert "pytest: 1 failed" in persisted["prompt"]
    assert "Сделай штуку" in persisted["prompt"]
    # And it is back in the queue, ready to be planned into the next wave.
    assert [e["task_id"] for e in execution_queue.load_queue(tmp_path)] == ["a"]


def test_failed_remote_ci_is_requeued_as_a_new_attempt(tmp_path, git_repo, api):
    row = _seed_completion(api, git_repo, task_id="a")
    for state in (
        completion_domain.CompletionState.RESULT_VALID,
        completion_domain.CompletionState.PULL_REQUEST_OPEN,
        completion_domain.CompletionState.MERGE_BLOCKED,
    ):
        row = runtime_db.update_completion(
            api.db_path,
            row["run_id"],
            expected_version=row["version"],
            fields={"completion_state": state},
        )
    runtime_db.update_completion(
        api.db_path,
        row["run_id"],
        expected_version=row["version"],
        fields={
            "last_reason_code": completion_domain.ReasonCode.CHECKS_FAILING,
            "recommended_action": "Required CI checks are failing.",
        },
    )
    task = _task(id="a", workspace_path=str(git_repo), goal="Исправь функцию")
    tasks_repository.save_tasks(tmp_path, [task])

    records = task_pipeline.plan_rework(
        tmp_path, {"a": task}, db_path=api.db_path, settings=_rework_settings()
    )

    assert [r["outcome"] for r in records] == [task_pipeline.REWORK_REQUEUED]
    persisted = tasks_repository.load_tasks(tmp_path)[0]
    assert "Required CI checks are failing" in persisted["prompt"]
    assert [e["task_id"] for e in execution_queue.load_queue(tmp_path)] == ["a"]


def test_rework_does_nothing_without_the_opt_in(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])

    assert (
        task_pipeline.plan_rework(
            tmp_path, {"a": task}, db_path=api.db_path, settings=_rework_settings(auto_rework=False)
        )
        == []
    )
    assert execution_queue.load_queue(tmp_path) == []


def test_rework_requires_auto_launch_too(tmp_path, git_repo, api):
    """A rework *is* a launch, so "fix it again" without "start work for me" is
    a contradiction and resolves to the safe answer."""
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])

    settings = _rework_settings(auto_launch=False)
    assert settings.auto_rework_active is False
    assert task_pipeline.plan_rework(tmp_path, {"a": task}, db_path=api.db_path, settings=settings) == []


def test_the_same_failure_is_reworked_only_once(tmp_path, git_repo, api):
    """Several ticks observe the same VALIDATION_FAILED row before the new run
    starts; only the first may spend a budget unit."""
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    tasks_repository.save_tasks(tmp_path, [task])
    settings = _rework_settings()

    first = task_pipeline.plan_rework(tmp_path, {"a": task}, db_path=api.db_path, settings=settings)
    assert [r["outcome"] for r in first] == [task_pipeline.REWORK_REQUEUED]

    reloaded = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
    assert task_pipeline.plan_rework(tmp_path, reloaded, db_path=api.db_path, settings=settings) == []
    assert tasks_repository.load_tasks(tmp_path)[0][task_pipeline.REWORK_COUNT_FIELD] == 1


def test_rework_budget_is_enforced_and_reported_once(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    task[task_pipeline.REWORK_COUNT_FIELD] = 2  # budget already spent
    tasks_repository.save_tasks(tmp_path, [task])
    settings = _rework_settings(max_rework_attempts=2)

    records = task_pipeline.plan_rework(
        tmp_path, {"a": task}, db_path=api.db_path, settings=settings
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REWORK_BUDGET_EXHAUSTED]
    assert execution_queue.load_queue(tmp_path) == []

    # Reported once, not on every subsequent tick.
    reloaded = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
    assert task_pipeline.plan_rework(tmp_path, reloaded, db_path=api.db_path, settings=settings) == []


def test_rework_prompt_does_not_compound_across_attempts(tmp_path, git_repo, api):
    """Attempt 3 must see one objective and one current failure — not attempt
    1's output stacked on attempt 2's."""
    _failed_validation(api, git_repo, summary="первый провал")
    task = _task(id="a", workspace_path=str(git_repo), goal="Базовая цель")
    tasks_repository.save_tasks(tmp_path, [task])
    settings = _rework_settings(max_rework_attempts=3)

    task_pipeline.plan_rework(tmp_path, {"a": task}, db_path=api.db_path, settings=settings)
    after_first = tasks_repository.load_tasks(tmp_path)[0]["prompt"]

    # A second, different failure on a new run.
    second_row = _failed_validation(api, git_repo, task_id="a", summary="второй провал")
    assert second_row["run_id"]
    reloaded = {t["id"]: t for t in tasks_repository.load_tasks(tmp_path)}
    task_pipeline.plan_rework(tmp_path, reloaded, db_path=api.db_path, settings=settings)
    after_second = tasks_repository.load_tasks(tmp_path)[0]["prompt"]

    assert "первый провал" in after_first
    assert "второй провал" in after_second
    assert "первый провал" not in after_second, "failure context compounded across attempts"
    assert after_second.startswith("Базовая цель")


def test_a_done_task_is_never_reworked(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo), status="Done")
    tasks_repository.save_tasks(tmp_path, [task])
    assert (
        task_pipeline.plan_rework(
            tmp_path, {"a": task}, db_path=api.db_path, settings=_rework_settings()
        )
        == []
    )


# --------------------------------------------------------------------------
# Nothing silently stuck
# --------------------------------------------------------------------------


def _stuck(tasks, decisions=(), *, db_path, settings=None, active=frozenset()):
    return task_pipeline.find_stuck_tasks(
        tasks,
        tuple(decisions),
        db_path=db_path,
        settings=settings or PipelineSettings(),
        active_task_ids=active,
    )


def test_a_task_whose_completion_stopped_is_reported(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    reported = _stuck([task], db_path=api.db_path)
    assert [(s.task_id, s.kind) for s in reported] == [("a", task_pipeline.STUCK_KIND_COMPLETION)]


def test_a_task_that_never_starts_is_reported(tmp_path, git_repo, api):
    """The genuinely invisible case: it sits in the queue looking fine while
    every tick refuses it for the same structural reason."""
    task = _task(id="a", workspace_path=str(git_repo))
    decision = task_pipeline.EntryDecision(
        entry_id="q1",
        task_id="a",
        action=task_pipeline.ACTION_SKIPPED,
        reason_code=task_pipeline.REASON_NEEDS_CONFIRMATION,
        explanation="рабочее дерево не чистое",
    )
    reported = _stuck([task], [decision], db_path=api.db_path)
    assert [(s.task_id, s.kind) for s in reported] == [("a", task_pipeline.STUCK_KIND_NOT_STARTING)]
    assert reported[0].remediation


def test_a_transiently_deferred_task_is_not_reported_as_stuck(tmp_path, git_repo, api):
    """Capacity, workspace-busy and backoff clear on their own — reporting them
    would drown the real cases."""
    task = _task(id="a", workspace_path=str(git_repo))
    from command_center.runtime import scheduler as sched

    decision = task_pipeline.EntryDecision(
        entry_id="q1",
        task_id="a",
        action=sched.ACTION_DEFER,
        reason_code=sched.REASON_GLOBAL_AT_CAPACITY,
        explanation="capacity",
    )
    assert _stuck([task], [decision], db_path=api.db_path) == ()


def test_a_running_task_is_never_reported_as_stuck(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    assert _stuck([task], db_path=api.db_path, active=frozenset({"a"})) == ()


def test_a_done_task_is_never_reported_as_stuck(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo), status="Done")
    assert _stuck([task], db_path=api.db_path) == ()


def test_exhausted_rework_is_reported_distinctly(tmp_path, git_repo, api):
    _failed_validation(api, git_repo)
    task = _task(id="a", workspace_path=str(git_repo))
    task[task_pipeline.REWORK_COUNT_FIELD] = 2
    reported = _stuck([task], db_path=api.db_path, settings=_rework_settings(max_rework_attempts=2))
    assert reported[0].kind == task_pipeline.STUCK_KIND_REWORK_EXHAUSTED


def test_a_failed_launch_status_without_a_completion_row_is_reported(tmp_path, git_repo, api):
    task = _task(id="a", workspace_path=str(git_repo), launch_status="Failed")
    reported = _stuck([task], db_path=api.db_path)
    assert [(s.task_id, s.kind) for s in reported] == [("a", task_pipeline.STUCK_KIND_LAUNCH)]


def test_stuck_report_is_deduplicated_and_ordered(tmp_path, git_repo, api):
    _failed_validation(api, git_repo, task_id="b")
    a = _task(id="a", workspace_path=str(git_repo), launch_status="Failed")
    b = _task(id="b", workspace_path=str(git_repo))
    decision = task_pipeline.EntryDecision(
        entry_id="q1",
        task_id="b",
        action=task_pipeline.ACTION_SKIPPED,
        reason_code=task_pipeline.REASON_LAUNCH_BLOCKED,
        explanation="blocked",
    )
    reported = _stuck([b, a], [decision], db_path=api.db_path)
    assert [s.task_id for s in reported] == ["a", "b"]
    assert len({s.task_id for s in reported}) == 2


# --------------------------------------------------------------------------
# Workspace remediation — recoverable, and only where the pipeline owns the tree
# --------------------------------------------------------------------------


def _remediate_settings(**overrides):
    base = dict(enabled=True, auto_launch=True, auto_remediate_workspace=True)
    base.update(overrides)
    return PipelineSettings(**base)


def _linked_worktree(repo, path, branch="task/x"):
    """A real linked worktree of `repo` — what the pipeline provisions itself."""
    import subprocess

    subprocess.run(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], cwd=repo, check=True,
                   capture_output=True)
    return path


def test_leftovers_in_a_pipeline_owned_worktree_are_stashed_not_destroyed(tmp_path, git_repo, api):
    wt = _linked_worktree(git_repo, tmp_path / "wt")
    (wt / "leftover.txt").write_text("важная незакоммиченная работа\n")
    task = _task(id="a", workspace_path=str(wt), branch="task/x")

    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REMEDIATION_STASHED]

    # The tree is clean, so the task can now launch...
    from command_center import git_info

    assert not git_info.get_status(wt).get("dirty")
    # ...and the work is recoverable, not gone.
    listing = git_info.run_git_command(wt, ["stash", "list"])
    assert task_pipeline.STASH_MESSAGE_PREFIX in listing.stdout
    git_info.run_git_command(wt, ["stash", "pop"])
    assert (wt / "leftover.txt").read_text().startswith("важная")


def test_a_humans_primary_working_tree_is_never_touched(tmp_path, git_repo, api):
    """The safety boundary: uncommitted work in the repository a person actually
    works in is reported, never tidied."""
    (git_repo / "my-wip.txt").write_text("моя работа\n")
    task = _task(id="a", workspace_path=str(git_repo))

    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REMEDIATION_NOT_OWNED]
    assert (git_repo / "my-wip.txt").exists()


def test_an_unrelated_repository_is_never_touched(tmp_path, git_repo, api):
    import subprocess

    other = tmp_path / "other"
    other.mkdir()
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.com"],
                 ["git", "config", "user.name", "t"]):
        subprocess.run(args, cwd=other, check=True)
    (other / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=other, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=other, check=True)
    (other / "dirty.txt").write_text("чужая работа\n")

    task = _task(id="a", workspace_path=str(other))
    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REMEDIATION_NOT_OWNED]
    assert (other / "dirty.txt").exists()


def test_remediation_does_nothing_without_the_opt_in(tmp_path, git_repo, api):
    wt = _linked_worktree(git_repo, tmp_path / "wt")
    (wt / "leftover.txt").write_text("x\n")
    task = _task(id="a", workspace_path=str(wt), branch="task/x")
    assert (
        task_pipeline.remediate_workspaces(
            tmp_path, {"a": task},
            {"AIOS": {"repository_path": str(git_repo)}},
            settings=_remediate_settings(auto_remediate_workspace=False),
        )
        == []
    )
    assert (wt / "leftover.txt").exists()


def test_a_clean_worktree_is_left_alone(tmp_path, git_repo, api):
    wt = _linked_worktree(git_repo, tmp_path / "wt")
    task = _task(id="a", workspace_path=str(wt), branch="task/x")
    assert (
        task_pipeline.remediate_workspaces(
            tmp_path, {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, settings=_remediate_settings()
        )
        == []
    )


def test_a_done_task_is_never_remediated(tmp_path, git_repo, api):
    wt = _linked_worktree(git_repo, tmp_path / "wt")
    (wt / "leftover.txt").write_text("x\n")
    task = _task(id="a", workspace_path=str(wt), branch="task/x", status="Done")
    assert (
        task_pipeline.remediate_workspaces(
            tmp_path, {"a": task}, {"AIOS": {"repository_path": str(git_repo)}}, settings=_remediate_settings()
        )
        == []
    )
    assert (wt / "leftover.txt").exists()


def test_a_feature_task_on_the_primary_tree_is_given_its_own_worktree(tmp_path, git_repo, api):
    """The `isolated_worktree_required` dead end: with no workspace_path the
    resolution order lands on the project's primary tree, which the isolation
    gate refuses forever. The repair is additive — a path is assigned, the
    launch path provisions it, nothing existing is touched."""
    task = _task(id="a", workspace_path=None, branch="task/alpha")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}

    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, cfg, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REMEDIATION_REPOINTED]

    persisted = tasks_repository.load_tasks(tmp_path)[0]
    expected = task_pipeline.derive_worktree_path(str(git_repo), "task/alpha")
    assert persisted["workspace_path"] == str(expected)
    # Additive only: the primary tree is untouched and nothing was created yet.
    assert git_repo.is_dir()
    assert not expected.exists()


def test_derived_worktree_path_is_deterministic_and_outside_the_repository(git_repo):
    once = task_pipeline.derive_worktree_path(str(git_repo), "feature/x")
    twice = task_pipeline.derive_worktree_path(str(git_repo), "feature/x")
    assert once == twice
    assert once.name == "feature-x"
    assert git_repo.resolve() not in once.parents, "a worktree must never live inside its own repo"


def test_a_mainline_task_on_the_primary_tree_is_left_alone(tmp_path, git_repo, api):
    """`main` work legitimately runs in the primary tree — not a defect."""
    task = _task(id="a", workspace_path=None, branch="main")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}
    assert (
        task_pipeline.remediate_workspaces(tmp_path, {"a": task}, cfg, settings=_remediate_settings())
        == []
    )


def test_an_existing_derived_path_is_never_adopted(tmp_path, git_repo, api):
    """Adopting a directory that is already there would mean guessing its
    contents are ours."""
    target = task_pipeline.derive_worktree_path(str(git_repo), "task/alpha")
    target.mkdir(parents=True)
    (target / "чужое.txt").write_text("не наше\n")
    task = _task(id="a", workspace_path=None, branch="task/alpha")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}

    assert (
        task_pipeline.remediate_workspaces(tmp_path, {"a": task}, cfg, settings=_remediate_settings())
        == []
    )
    assert (target / "чужое.txt").exists()


def test_a_workspace_pointing_somewhere_unrelated_is_not_guessed_at(tmp_path, git_repo, api):
    other = tmp_path / "elsewhere"
    other.mkdir()
    task = _task(id="a", workspace_path=str(other), branch="task/alpha")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}
    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, cfg, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] != [task_pipeline.REMEDIATION_REPOINTED]
    assert tasks_repository.load_tasks(tmp_path)[0]["workspace_path"] == str(other)


# --------------------------------------------------------------------------
# Planning must not propose work the launcher is required to reject
# --------------------------------------------------------------------------


def test_planner_limits_candidates_to_providers_the_project_authorizes(
    tmp_path, git_repo, api, monkeypatch
):
    from command_center import project_config

    monkeypatch.setattr(
        project_config, "allowed_execution_providers", lambda pid: ("claude_code",)
    )
    task = _task(id="a", workspace_path=str(git_repo))
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")], {"a": task}, {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    assert wave.work_items[0].allowed_agents == frozenset({"claude_code"})
    assert wave.work_items[0].preferred_agent is None


def test_planner_keeps_multiple_authorized_providers_available_for_load_balancing(
    tmp_path, git_repo, api, monkeypatch
):
    from command_center import project_config

    monkeypatch.setattr(
        project_config,
        "allowed_execution_providers",
        lambda pid: ("claude_code", "codex"),
    )
    task = _task(id="a", workspace_path=str(git_repo), executor="claude_code")
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")],
        {"a": task},
        {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )

    assert wave.work_items[0].allowed_agents == frozenset(
        {"claude_code", "codex"}
    )
    assert wave.work_items[0].preferred_agent is None


def test_a_task_naming_a_forbidden_provider_is_not_silently_redirected(
    tmp_path, git_repo, api, monkeypatch
):
    """Redirecting it would run the work somewhere the operator did not choose.
    Keeping the request lets the authorization gate refuse it visibly."""
    from command_center import project_config

    monkeypatch.setattr(
        project_config, "allowed_execution_providers", lambda pid: ("claude_code",)
    )
    task = _task(id="a", workspace_path=str(git_repo), executor="codex")
    wave = task_pipeline.adapt_ready_entries(
        [_entry("q1", "a")], {"a": task}, {"AIOS": {"repository_path": str(git_repo)}},
        db_path=api.db_path,
    )
    assert wave.work_items[0].preferred_agent == "codex"


def test_an_already_checked_out_branch_reuses_its_worktree(tmp_path, git_repo, api):
    """git allows one worktree per branch. Deriving a second path would fail
    with "cannot attach an already-checked-out branch", leaving the task
    permanently unlaunchable for a reason it cannot fix itself."""
    import subprocess

    existing = tmp_path / "already-here"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/taken", str(existing), "HEAD"],
        cwd=git_repo, check=True, capture_output=True,
    )
    assert task_pipeline._worktree_holding_branch(str(git_repo), "feature/taken") == str(
        existing.resolve()
    )

    task = _task(id="a", workspace_path=None, branch="feature/taken")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}
    records = task_pipeline.remediate_workspaces(
        tmp_path, {"a": task}, cfg, settings=_remediate_settings()
    )
    assert [r["outcome"] for r in records] == [task_pipeline.REMEDIATION_REPOINTED]
    assert tasks_repository.load_tasks(tmp_path)[0]["workspace_path"] == str(existing.resolve())


def test_a_free_branch_still_gets_a_fresh_derived_worktree(tmp_path, git_repo, api):
    assert task_pipeline._worktree_holding_branch(str(git_repo), "feature/unused") is None
    task = _task(id="a", workspace_path=None, branch="feature/unused")
    tasks_repository.save_tasks(tmp_path, [task])
    cfg = {"AIOS": {"repository_path": str(git_repo), "default_branch": "main"}}
    task_pipeline.remediate_workspaces(tmp_path, {"a": task}, cfg, settings=_remediate_settings())
    expected = task_pipeline.derive_worktree_path(str(git_repo), "feature/unused")
    assert tasks_repository.load_tasks(tmp_path)[0]["workspace_path"] == str(expected)


def test_branch_lookup_degrades_quietly_on_a_bad_repository():
    assert task_pipeline._worktree_holding_branch("/nonexistent/repo", "any") is None


def test_run_attempt_budget_is_operator_configurable(tmp_path):
    """Attempts consumed by an external fault — an expired session, an
    unreachable daemon — leave a task `retry_exhausted` with no honest remedy
    unless the budget can be raised. Raising it grants a fresh attempt without
    rewriting the run history that recorded the failures."""
    assert PipelineSettings().max_run_attempts == pipeline_settings.DEFAULT_MAX_RUN_ATTEMPTS
    saved = pipeline_settings.save_settings(tmp_path, PipelineSettings(max_run_attempts=5))
    assert pipeline_settings.load_settings(tmp_path).max_run_attempts == 5
    assert saved.max_run_attempts == 5
    # Out of range falls back rather than clamping, so a typo is visible.
    assert PipelineSettings.from_dict({"max_run_attempts": 999}).max_run_attempts == (
        pipeline_settings.DEFAULT_MAX_RUN_ATTEMPTS
    )


def test_the_planner_receives_the_configured_run_attempt_budget(tmp_path, git_repo, api, monkeypatch):
    """A budget that never reaches `scheduler.plan` would be decorative."""
    from command_center.runtime import scheduler

    seen = {}
    real_plan = api.plan_schedule

    def capture(items, **kwargs):
        seen["max_attempts"] = kwargs.get("policy").max_attempts if kwargs.get("policy") else None
        return real_plan(items, **kwargs)

    monkeypatch.setattr(api, "plan_schedule", capture)
    pipeline_settings.save_settings(
        tmp_path, PipelineSettings(enabled=True, max_run_attempts=7)
    )
    task_pipeline.tick(tmp_path, api, {}, advance_wait_seconds=1)
    assert seen["max_attempts"] == 7
    assert scheduler.RetryPolicy().max_attempts != 7  # not merely the default


def test_run_timeout_is_a_bounded_setting():
    """900s was written for one interactive launch; an audit reading a whole
    repository needs more, and a run that hits the ceiling costs a full run's
    tokens for nothing."""
    assert PipelineSettings().run_timeout_seconds == pipeline_settings.DEFAULT_RUN_TIMEOUT_SECONDS
    assert PipelineSettings.from_dict({"run_timeout_seconds": 99}).run_timeout_seconds == (
        pipeline_settings.DEFAULT_RUN_TIMEOUT_SECONDS
    )
    assert PipelineSettings.from_dict({"run_timeout_seconds": 3600}).run_timeout_seconds == 3600


def test_the_dispatcher_passes_the_configured_timeout(tmp_path, git_repo, api, monkeypatch):
    """A setting nothing reads is a setting that lies to the operator."""
    from command_center import execution_queue as queue_module

    seen = {}

    def fake_launch(*args, **kwargs):
        seen.update(kwargs)
        return [], []

    monkeypatch.setattr(queue_module, "launch_ready", fake_launch)
    decision = task_pipeline.EntryDecision(
        entry_id="q1", task_id="a", action="ASSIGN", reason_code="assigned", explanation="x"
    )
    task_pipeline._dispatch(
        tmp_path, api, [], {}, {}, (decision,),
        PipelineSettings(enabled=True, auto_launch=True, run_timeout_seconds=3600),
    )
    assert seen["timeout_seconds"] == 3600
