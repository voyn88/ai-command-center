"""ADR 0007 step 1 — the execution queue's SQLite mirror and divergence check.

JSON stays authoritative throughout the dual-write phases. What these tests
defend is that the mirror can never make the real queue worse, and that the
divergence check cannot report agreement it has not actually verified — the
migration's "stop writing JSON" step is gated on that count, so a check that
under-reports would advance the migration on a false basis.
"""

from __future__ import annotations

import pytest

from command_center import execution_queue
from command_center.runtime import db as runtime_db


@pytest.fixture
def root(tmp_path):
    runtime_db.migrate(runtime_db.resolve_db_path(tmp_path))
    return tmp_path


def _entry(entry_id, **overrides):
    entry = {
        "id": entry_id,
        "task_id": f"task-{entry_id}",
        "project": "AICC",
        "state": execution_queue.STATE_READY,
        "reason": None,
        "run_id": None,
        "added_at": "2026-07-24T10:00:00",
        "evaluated_at": None,
        "launched_at": None,
    }
    entry.update(overrides)
    return entry


def test_saving_writes_both_stores(root):
    execution_queue.save_queue(root, [_entry("q1"), _entry("q2")])
    assert [e["id"] for e in execution_queue.load_queue(root)] == ["q1", "q2"]
    mirrored = runtime_db.list_queue_entries(runtime_db.resolve_db_path(root))
    assert [e["id"] for e in mirrored] == ["q1", "q2"]


def test_list_order_is_preserved_in_the_mirror(root):
    """Queue order is load-bearing — it is the display and planning order — so a
    set-shaped mirror that silently reordered would be a real defect."""
    execution_queue.save_queue(root, [_entry("c"), _entry("a"), _entry("b")])
    mirrored = runtime_db.list_queue_entries(runtime_db.resolve_db_path(root))
    assert [e["id"] for e in mirrored] == ["c", "a", "b"]


def test_matching_stores_report_no_divergence(root):
    execution_queue.save_queue(root, [_entry("q1")])
    assert execution_queue.queue_divergence(root) == []


def test_a_mirror_failure_never_breaks_the_real_write(root, monkeypatch):
    """During dual-write the mirror is not load-bearing. Letting it raise would
    mean a migration step could take down the queue it is migrating."""
    monkeypatch.setattr(
        runtime_db, "replace_queue_entries", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    execution_queue.save_queue(root, [_entry("q1")])
    assert [e["id"] for e in execution_queue.load_queue(root)] == ["q1"]


def test_an_absent_mirror_is_reported_not_silently_treated_as_agreement(tmp_path):
    """The dangerous answer would be `[]`: "stop writing JSON" is gated on a
    session with no divergence, and a mirror that was never written has nothing
    to disagree with — the migration would advance on a store that does not
    exist."""
    divergence = execution_queue.queue_divergence(tmp_path)
    assert [d["entry_id"] for d in divergence] == [execution_queue.MIRROR_UNAVAILABLE]


def test_a_stale_mirror_row_is_reported(root):
    execution_queue.save_queue(root, [_entry("q1")])
    # Mirror keeps an entry the authoritative store no longer has.
    runtime_db.replace_queue_entries(
        runtime_db.resolve_db_path(root), [_entry("q1"), _entry("ghost")]
    )
    divergence = execution_queue.queue_divergence(root)
    assert [d["entry_id"] for d in divergence] == ["ghost"]
    assert divergence[0]["json"] is None


def test_a_changed_field_is_reported_with_its_name(root):
    execution_queue.save_queue(root, [_entry("q1")])
    runtime_db.replace_queue_entries(
        runtime_db.resolve_db_path(root), [_entry("q1", state=execution_queue.STATE_WAITING)]
    )
    divergence = execution_queue.queue_divergence(root)
    assert divergence[0]["entry_id"] == "q1"
    assert "state" in divergence[0]["fields"]


def test_an_entry_missing_from_the_mirror_is_reported(root):
    execution_queue.save_queue(root, [_entry("q1")])
    runtime_db.replace_queue_entries(runtime_db.resolve_db_path(root), [])
    divergence = execution_queue.queue_divergence(root)
    assert [d["entry_id"] for d in divergence] == ["q1"]
    assert divergence[0]["mirror"] is None


def test_unknown_keys_do_not_break_the_mirror(root):
    """The JSON store accepts extra keys; a mirror stricter than the
    authoritative store would fail on data that is by definition valid."""
    execution_queue.save_queue(root, [{**_entry("q1"), "future_field": "x"}])
    assert execution_queue.queue_divergence(root) == []


# --------------------------------------------------------------------------
# ADR 0007 step 2 — backfill and the surfaced divergence count
# --------------------------------------------------------------------------


def test_backfill_imports_existing_entries(root):
    """The JSON store predates the mirror, so the mirror starts empty and must
    be seeded from what is already there."""
    execution_queue.save_queue(root, [_entry("q1"), _entry("q2")])
    runtime_db.replace_queue_entries(runtime_db.resolve_db_path(root), [])
    assert len(execution_queue.queue_divergence(root)) == 2

    assert execution_queue.backfill_mirror(root) == 2
    assert execution_queue.queue_divergence(root) == []


def test_backfill_is_idempotent(root):
    """It is expected to run more than once — per operator, and again after a
    rollback and re-advance. A backfill that appended on the second run would
    manufacture the divergence it exists to remove."""
    execution_queue.save_queue(root, [_entry("q1"), _entry("q2")])
    first = execution_queue.backfill_mirror(root)
    second = execution_queue.backfill_mirror(root)
    assert first == second == 2
    assert execution_queue.queue_divergence(root) == []


def test_divergence_reaches_the_tick_result(root, tmp_path):
    """The count is gating: ADR 0007 step 4 may not proceed while it is
    non-zero, so it has to travel to where an operator can see it."""
    from command_center import pipeline_settings, task_pipeline
    from command_center.pipeline_settings import PipelineSettings
    from command_center.runtime import api as runtime_api

    pipeline_settings.save_settings(root, PipelineSettings(enabled=True))
    execution_queue.save_queue(root, [_entry("q1")])
    # Mirror drifts behind the authoritative store.
    runtime_db.replace_queue_entries(runtime_db.resolve_db_path(root), [])

    api = runtime_api.ExecutionCenterAPI(db_path=root / "runtime.db")
    result = task_pipeline.tick(root, api, {}, advance_wait_seconds=0.1)
    assert result.ran is True
    assert [d["entry_id"] for d in result.queue_divergence] == ["q1"]
    assert "queue_divergence" in result.as_dict()

    # VOYN-W0-AICC-SRV-07c: the same tick's check is also remembered, so a
    # later clean tick does not make this one invisible.
    assert result.queue_divergence_window is not None
    assert result.queue_divergence_window.clean is False
    assert result.queue_divergence_window.divergent_checks == 1
    assert result.as_dict()["queue_divergence_window"]["clean"] is False
