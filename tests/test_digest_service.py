"""Unit tests for the morning-digest engine (``command_center.digest.service``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox,
so the runtime db the service writes is throwaway. The *sources* are stubbed via
monkeypatch, so assembly, ordering, idempotent rebuild and actionable-ref
guarantees are tested against fixed inputs with no real git/db/task read.

Fixtures use only invented ids and the generic project code ``AICC`` — no real
names or paths, keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest

from command_center.digest import service as digest_service
from command_center.digest.service import (
    CATEGORY_ADVISOR,
    CATEGORY_ATTENTION,
    CATEGORY_OVERNIGHT,
    CATEGORY_STATUS,
    DigestService,
)
from command_center.events import DigestReady, default_bus


@pytest.fixture
def stub_sources(monkeypatch) -> None:
    """Replace every source adapter with a fixed, deterministic payload."""
    src = digest_service.sources
    monkeypatch.setattr(
        src, "overnight_runs",
        lambda **_: [
            {"ref": "run:r1", "title": "implementation", "detail": "COMPLETED",
             "project": "AICC", "ts": "2026-08-12T02:00:00"},
        ],
    )
    monkeypatch.setattr(
        src, "recent_commits",
        lambda **_: [
            {"ref": "commit:abc1234", "title": "fix: thing", "detail": "abc1234",
             "project": None, "ts": "2026-08-12T01:00:00"},
        ],
    )
    monkeypatch.setattr(
        src, "open_proposals",
        lambda **_: [
            {"ref": "proposal:p1", "title": "Adopt X", "detail": "trend · new",
             "project": "AICC", "ts": "2026-08-12T00:00:00"},
        ],
    )
    monkeypatch.setattr(
        src, "attention_items",
        lambda **_: [
            {"ref": "task:t1", "title": "Broken deploy", "detail": "Requires Attention",
             "project": "AICC", "ts": "2026-08-11T23:00:00"},
        ],
    )
    monkeypatch.setattr(
        src, "agent_status",
        lambda **_: {"running": 1, "queued": 2, "attention": 0, "total": 3, "available": True},
    )


@pytest.fixture
def digest_events() -> list[DigestReady]:
    captured: list[DigestReady] = []
    bus = default_bus()
    bus.clear()
    off = bus.subscribe(DigestReady, captured.append)
    try:
        yield captured
    finally:
        off()
        bus.clear()


def test_build_assembles_in_section_order(stub_sources) -> None:
    rows = DigestService().build(day="2026-08-12")

    # overnight (run + commit), advisor, attention, status — in that fixed order.
    assert [r["category"] for r in rows] == [
        CATEGORY_OVERNIGHT, CATEGORY_OVERNIGHT,
        CATEGORY_ADVISOR, CATEGORY_ATTENTION, CATEGORY_STATUS,
    ]
    # position is dense and ascending, matching list order.
    assert [r["position"] for r in rows] == [0, 1, 2, 3, 4]
    # project-scoped lines are prefixed with the project code.
    assert rows[0]["title"] == "[AICC] implementation"


def test_every_entry_is_actionable(stub_sources) -> None:
    rows = DigestService().build(day="2026-08-12")
    # Each entry carries at least one ref (a link/action target) — that is what
    # "actionable" means on this surface.
    assert all(r["refs"] for r in rows)
    assert "run:r1" in rows[0]["refs"]
    assert any("action:/api/v1/proposals" in r["refs"] for r in rows)
    assert rows[-1]["refs"] == ["action:/api/v1/agents"]


def test_build_emits_digest_ready_per_entry(stub_sources, digest_events) -> None:
    rows = DigestService().build(day="2026-08-12")
    assert len(digest_events) == len(rows)
    assert {e.digest_id for e in digest_events} == {r["id"] for r in rows}
    assert {e.category for e in digest_events} == {r["category"] for r in rows}


def test_rebuild_is_idempotent_replaces_not_duplicates(stub_sources) -> None:
    first = DigestService().build(day="2026-08-12")
    second = DigestService().build(day="2026-08-12")

    today = DigestService().today(day="2026-08-12")
    # Same count and same ordered content — no duplication across rebuilds.
    assert len(today) == len(first) == len(second)
    assert [(r["title"], r["category"], r["position"]) for r in today] == [
        (r["title"], r["category"], r["position"]) for r in first
    ]
    # Rebuild replaces the rows: the first build's ids are gone.
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in today})


def test_rebuild_does_not_touch_other_days(stub_sources) -> None:
    other = DigestService().build(day="2026-08-11")
    DigestService().build(day="2026-08-12")
    DigestService().build(day="2026-08-12")  # rebuild today

    kept = DigestService().today(day="2026-08-11")
    assert {r["id"] for r in kept} == {r["id"] for r in other}


def test_today_is_read_only(stub_sources) -> None:
    # Nothing built yet for this day → empty, and no write happened.
    assert DigestService().today(day="2026-08-09") == []
