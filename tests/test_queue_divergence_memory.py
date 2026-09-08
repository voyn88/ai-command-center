"""VOYN-W0-AICC-SRV-07c — windowed memory of ADR 0007 queue-divergence checks.

`execution_queue.queue_divergence` (covered by `test_queue_dual_write.py`) only
ever answers "right now". These tests defend the memory layered on top of it:
a divergence recorded on one tick must still be visible to the window summary
even after a later, clean tick — that is the whole point of remembering it —
and must fall out of the window once it is old enough that it no longer says
anything about the current session.
"""

from __future__ import annotations

import pytest

from command_center import queue_divergence_memory
from command_center.runtime import db as runtime_db


@pytest.fixture
def db_path(tmp_path):
    path = runtime_db.resolve_db_path(tmp_path)
    runtime_db.migrate(path)
    return path


def _divergence(*entry_ids):
    return [{"entry_id": entry_id, "fields": ["state"]} for entry_id in entry_ids]


def test_a_clean_check_summarizes_as_clean(db_path, tmp_path):
    summary = queue_divergence_memory.record_and_summarize(
        tmp_path, [], db_path=db_path, now="2026-08-26T10:00:00"
    )
    assert summary.clean is True
    assert summary.checks == 1
    assert summary.divergent_checks == 0
    assert summary.total_divergences == 0
    assert summary.last_divergence_at is None


def test_a_divergent_check_summarizes_as_not_clean(db_path, tmp_path):
    summary = queue_divergence_memory.record_and_summarize(
        tmp_path, _divergence("q1", "q2"), db_path=db_path, now="2026-08-26T10:00:00"
    )
    assert summary.clean is False
    assert summary.checks == 1
    assert summary.divergent_checks == 1
    assert summary.total_divergences == 2
    assert summary.last_divergence_at == "2026-08-26T10:00:00"


def test_a_divergence_survives_a_later_clean_tick(db_path, tmp_path):
    """The reason this module exists: a divergence that has already cleared by
    the time the operator looks must still be visible in the session's memory,
    not just this instant's (empty) result."""
    queue_divergence_memory.record_and_summarize(
        tmp_path, _divergence("q1"), db_path=db_path, now="2026-08-26T10:00:00"
    )
    summary = queue_divergence_memory.record_and_summarize(
        tmp_path, [], db_path=db_path, now="2026-08-26T10:05:00"
    )
    assert summary.clean is False
    assert summary.checks == 2
    assert summary.divergent_checks == 1
    assert summary.last_divergence_at == "2026-08-26T10:00:00"


def test_a_divergence_ages_out_of_the_window(db_path, tmp_path):
    """Once the divergent check is older than the window, it no longer says
    anything about the current session and must not keep the panel warning
    forever on the strength of something that happened a day ago."""
    queue_divergence_memory.record_and_summarize(
        tmp_path,
        _divergence("q1"),
        db_path=db_path,
        window_hours=24,
        now="2026-08-25T09:00:00",
    )
    summary = queue_divergence_memory.record_and_summarize(
        tmp_path, [], db_path=db_path, window_hours=24, now="2026-08-26T10:00:00"
    )
    assert summary.clean is True
    assert summary.checks == 1  # only the fresh clean check remains in the window
    assert summary.last_divergence_at is None


def test_pruning_actually_deletes_the_aged_out_row(db_path, tmp_path):
    """Not just excluded from the summary — removed, so the memory stays
    windowed rather than growing forever."""
    queue_divergence_memory.record_and_summarize(
        tmp_path,
        _divergence("q1"),
        db_path=db_path,
        window_hours=24,
        now="2026-08-25T09:00:00",
    )
    queue_divergence_memory.record_and_summarize(
        tmp_path, [], db_path=db_path, window_hours=24, now="2026-08-26T10:00:00"
    )
    rows = runtime_db.list_queue_divergence_checks(db_path)
    assert [row["checked_at"] for row in rows] == ["2026-08-26T10:00:00"]


def test_recording_never_raises_even_when_the_store_is_unwritable(tmp_path, monkeypatch):
    """Remembering the divergence must never be able to break the tick it is
    observing — the same posture `queue_divergence` itself takes toward the
    mirror it reads."""
    monkeypatch.setattr(
        runtime_db,
        "record_queue_divergence_check",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    summary = queue_divergence_memory.record_and_summarize(tmp_path, _divergence("q1"))
    assert summary.clean is True
    assert summary.checks == 0
