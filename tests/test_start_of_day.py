"""Unit tests for the start-of-day priority list
(``command_center.digest.start_of_day``, VOYN-IOS-AUTO-HOME).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox,
so owner items and the digest are written to a throwaway runtime db. Digest
*sources* are stubbed via monkeypatch (as in ``test_digest_service.py``), so
the merge/order/bound logic is tested against fixed inputs.

Fixtures use only invented ids and the generic project code ``AICC`` — no real
names or paths, keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest

from command_center.api.wave1_service import ROOT
from command_center.digest import service as digest_service
from command_center.digest.service import DigestService
from command_center.digest.start_of_day import (
    MAX_CRITICAL,
    build_start_of_day_snapshot,
)
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

DAY = "2026-08-12"


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def no_digest_sources(monkeypatch) -> None:
    """No overnight/advisor/attention activity and an idle agent line — the
    digest still assembles (the status line is unconditional), but it
    contributes nothing to the critical list."""
    src = digest_service.sources
    monkeypatch.setattr(src, "overnight_runs", lambda **_: [])
    monkeypatch.setattr(src, "recent_commits", lambda **_: [])
    monkeypatch.setattr(src, "open_proposals", lambda **_: [])
    monkeypatch.setattr(src, "attention_items", lambda **_: [])
    monkeypatch.setattr(
        src, "agent_status",
        lambda **_: {"running": 0, "queued": 0, "attention": 0, "total": 0, "available": True},
    )


@pytest.fixture
def one_attention_item(no_digest_sources, monkeypatch) -> None:
    src = digest_service.sources
    monkeypatch.setattr(
        src, "attention_items",
        lambda **_: [
            {"ref": "task:t1", "title": "Broken deploy", "detail": "Requires Attention",
             "project": "AICC", "ts": "2026-08-11T23:00:00"},
        ],
    )


def _owner_item(**overrides):
    defaults = dict(title="Do the thing")
    defaults.update(overrides)
    return db.create_owner_item(resolve_db_path(ROOT), **defaults)


def test_empty_state_returns_empty_snapshot(no_digest_sources) -> None:
    built = DigestService(root=ROOT).build(day=DAY)
    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)
    assert snapshot["day"] == DAY
    assert snapshot["critical"] == []
    assert snapshot["critical_truncated"] is False
    # The unconditional agent-status line is the only digest entry — no
    # overnight/advisor/attention activity was stubbed in.
    assert [r["category"] for r in snapshot["digest"]] == ["status"]
    assert {r["id"] for r in snapshot["digest"]} == {r["id"] for r in built}


def test_due_owner_item_outranks_undue_item_and_attention_digest_entry(
    one_attention_item,
) -> None:
    DigestService(root=ROOT).build(day=DAY)
    undue = _owner_item(title="No deadline")
    due = _owner_item(title="Ship the report", due="2026-08-12T09:00:00")

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    kinds_and_ids = [(e["kind"], e["id"]) for e in snapshot["critical"]]
    assert kinds_and_ids[0] == ("owner_item", due["id"])
    # The undue owner item and the attention digest entry both land after the
    # due item; order between them follows "newest first" on created_at.
    assert {k for k, _ in kinds_and_ids[1:]} == {"owner_item", "digest_item"}
    assert undue["id"] in {i for _, i in kinds_and_ids}


def test_earlier_due_date_ranks_before_later_due_date(no_digest_sources) -> None:
    DigestService(root=ROOT).build(day=DAY)
    later = _owner_item(title="Later", due="2026-09-01T00:00:00")
    earlier = _owner_item(title="Earlier", due="2026-08-13T00:00:00")

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    assert [e["id"] for e in snapshot["critical"]] == [earlier["id"], later["id"]]


def test_done_owner_items_are_excluded(no_digest_sources) -> None:
    DigestService(root=ROOT).build(day=DAY)
    done = _owner_item(title="Already handled", done=True)

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    assert done["id"] not in {e["id"] for e in snapshot["critical"]}


def test_sensitive_project_owner_item_is_excluded(no_digest_sources) -> None:
    DigestService(root=ROOT).build(day=DAY)
    hidden = _owner_item(title="Bank thing", project_ref="BANK")

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    assert hidden["id"] not in {e["id"] for e in snapshot["critical"]}


def test_critical_list_is_bounded_and_flags_truncation(no_digest_sources) -> None:
    DigestService(root=ROOT).build(day=DAY)
    for i in range(MAX_CRITICAL + 5):
        _owner_item(title=f"item {i}")

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    assert len(snapshot["critical"]) == MAX_CRITICAL
    assert snapshot["critical_truncated"] is True


def test_non_attention_digest_entries_are_returned_as_context(monkeypatch) -> None:
    src = digest_service.sources
    monkeypatch.setattr(
        src, "overnight_runs",
        lambda **_: [
            {"ref": "run:r1", "title": "implementation", "detail": "COMPLETED",
             "project": "AICC", "ts": "2026-08-12T02:00:00"},
        ],
    )
    monkeypatch.setattr(src, "recent_commits", lambda **_: [])
    monkeypatch.setattr(src, "open_proposals", lambda **_: [])
    monkeypatch.setattr(src, "attention_items", lambda **_: [])
    monkeypatch.setattr(
        src, "agent_status",
        lambda **_: {"running": 0, "queued": 0, "attention": 0, "total": 0, "available": True},
    )
    DigestService(root=ROOT).build(day=DAY)

    snapshot = build_start_of_day_snapshot(root=ROOT, day=DAY)

    assert snapshot["critical"] == []
    assert any(r["category"] == "overnight" for r in snapshot["digest"])
    assert any(r["category"] == "status" for r in snapshot["digest"])
