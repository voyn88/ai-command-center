"""Episode detection for the tick-stall watchdog (VOYN-W0-AICC-TICK-STALL-
WATCHDOG), on plain data: these tests need no database, because
``detect_stalls`` is a pure function over the ordered tick history — the
property the watchdog exists to defend (5+ consecutive identical skips is a
stall; anything else is the pipeline working) must hold before any SQL is
involved."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from command_center.orchestrator.watchdog import (
    StallEpisode,
    TickSnapshot,
    detect_stalls,
    escalation_task_id,
)

T0 = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)


def _ticks(*pair_sets: list[tuple[str, str]]) -> list[TickSnapshot]:
    """Oldest -> newest, five minutes apart, ids t0, t1, ..."""
    return [
        TickSnapshot(
            tick_id=f"t{index}",
            observed_at=T0 + timedelta(minutes=5 * index),
            pairs=frozenset(pairs),
        )
        for index, pairs in enumerate(pair_sets)
    ]


PAIR = ("VOYN-W0-X", "no_review_result_yet")


def test_five_consecutive_identical_skips_are_one_episode() -> None:
    ticks = _ticks(*[[PAIR]] * 5)
    episodes = detect_stalls("review_marker", ticks, threshold=5)
    assert episodes == [
        StallEpisode(
            tick_kind="review_marker",
            task_id="VOYN-W0-X",
            reason="no_review_result_yet",
            first_tick_id="t0",
            consecutive=5,
            first_seen_at=ticks[0].observed_at,
            last_seen_at=ticks[4].observed_at,
        )
    ]


def test_fewer_than_threshold_is_not_an_episode() -> None:
    assert detect_stalls("review_marker", _ticks(*[[PAIR]] * 4), threshold=5) == []


def test_an_interleaved_different_reason_resets_the_streak() -> None:
    # Four identical, one different, four identical: neither run reaches 5.
    ticks = _ticks(
        *[[PAIR]] * 4,
        [("VOYN-W0-X", "marker_already_posted")],
        *[[PAIR]] * 4,
    )
    assert detect_stalls("review_marker", ticks, threshold=5) == []


def test_a_tick_without_the_pair_resets_the_streak() -> None:
    ticks = _ticks(*[[PAIR]] * 4, [], *[[PAIR]] * 4)
    assert detect_stalls("review_marker", ticks, threshold=5) == []


def test_a_streak_that_broke_before_the_newest_tick_is_not_escalated() -> None:
    # Six identical skips, then the newest tick moved on: the stall resolved
    # itself, and escalating it would page about the past.
    ticks = _ticks(*[[PAIR]] * 6, [])
    assert detect_stalls("review_marker", ticks, threshold=5) == []


def test_the_episode_starts_where_the_streak_starts_not_at_the_window_edge() -> None:
    ticks = _ticks([], [], *[[PAIR]] * 6)
    (episode,) = detect_stalls("review_marker", ticks, threshold=5)
    assert episode.first_tick_id == "t2"
    assert episode.consecutive == 6
    assert episode.first_seen_at == ticks[2].observed_at
    assert episode.last_seen_at == ticks[-1].observed_at


def test_a_growing_streak_keeps_the_same_episode_identity() -> None:
    # The idempotency anchor: while the stall persists, first_tick_id (and
    # therefore the ledger's UNIQUE identity and the escalation task id) must
    # not move, or every watchdog run would file a fresh escalation.
    five = detect_stalls("review_marker", _ticks(*[[PAIR]] * 5), threshold=5)
    seven = detect_stalls("review_marker", _ticks(*[[PAIR]] * 7), threshold=5)
    assert five[0].first_tick_id == seven[0].first_tick_id == "t0"
    assert escalation_task_id(five[0]) == escalation_task_id(seven[0])
    assert seven[0].consecutive == 7


def test_independent_pairs_are_detected_independently() -> None:
    other = ("VOYN-W0-Y", "no_accept_marker_on_head")
    ticks = _ticks(*[[PAIR, other]] * 3, *[[PAIR]] * 3)
    (episode,) = detect_stalls("merge", ticks, threshold=5)
    assert (episode.task_id, episode.reason) == PAIR
    assert episode.consecutive == 6


def test_empty_history_detects_nothing() -> None:
    assert detect_stalls("review", [], threshold=5) == []


def test_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError):
        detect_stalls("review", _ticks([PAIR]), threshold=0)


def test_escalation_task_id_is_deterministic_and_backlog_shaped() -> None:
    (episode,) = detect_stalls("review", _ticks(*[[PAIR]] * 5), threshold=5)
    new_id = escalation_task_id(episode)
    assert new_id == "VOYN-W0-X-STALL-t0"
    # Must satisfy backlog_task_id_shape or the upsert refuses it.
    import re

    assert re.fullmatch(r"VOYN-[A-Za-z0-9][A-Za-z0-9._-]*", new_id)
