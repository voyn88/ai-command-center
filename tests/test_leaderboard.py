"""Agent leaderboard: tiers computed from run history, with a trend behind
the tier — not a static one-shot rank.

Mirrors `read_model.py`'s testing style: pure functions over plain dicts
shaped like `runtime.runs_read.list_unified_runs()` rows, no Streamlit, no
database.
"""

from __future__ import annotations

from command_center import leaderboard


def _run(agent: str, state: str, started_at: str, *, run_id: str | None = None) -> dict:
    return {
        "id": run_id or f"{agent}-{started_at}",
        "agent": agent,
        "state": state,
        "started_at": started_at,
        "created_at": started_at,
    }


def test_empty_run_list_yields_empty_leaderboard():
    board = leaderboard.compute_leaderboard([])
    assert board.entries == []


def test_unknown_agent_runs_are_excluded_entirely():
    runs = [_run("—", "COMPLETED", "2026-01-01T00:00:00")] * 10
    board = leaderboard.compute_leaderboard(runs)
    assert board.entries == []


def test_agent_below_min_runs_is_newcomer_even_if_all_succeeded():
    runs = [_run("codex", "COMPLETED", f"2026-01-0{i}T00:00:00") for i in range(1, 5)]
    board = leaderboard.compute_leaderboard(runs)
    assert len(board.entries) == 1
    entry = board.entries[0]
    assert entry.agent == "codex"
    assert entry.tier == leaderboard.TIER_NEWCOMER
    assert entry.scored_runs == 4


def test_agent_below_min_runs_is_newcomer_even_if_all_failed():
    runs = [_run("codex", "FAILED", f"2026-01-0{i}T00:00:00") for i in range(1, 4)]
    board = leaderboard.compute_leaderboard(runs)
    assert board.entries[0].tier == leaderboard.TIER_NEWCOMER


def test_high_stable_success_rate_is_top():
    runs = [_run("claude", "COMPLETED", f"2026-01-{i:02d}T00:00:00") for i in range(1, 7)]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.scored_runs == 6
    assert entry.success_rate == 1.0
    assert entry.trend == leaderboard.TREND_FLAT
    assert entry.tier == leaderboard.TIER_TOP


def test_low_success_rate_is_risky_regardless_of_trend():
    states = ["COMPLETED", "FAILED", "FAILED", "COMPLETED", "FAILED", "FAILED"]
    runs = [_run("flaky", s, f"2026-01-{i:02d}T00:00:00") for i, s in enumerate(states, start=1)]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.success_rate < leaderboard.RISKY_SUCCESS_RATE
    assert entry.tier == leaderboard.TIER_RISKY


def test_sharp_decline_forces_risky_even_with_decent_overall_rate():
    # 4 earlier COMPLETED, then 4 recent mostly FAILED: overall rate stays
    # >= RISKY_SUCCESS_RATE, but the trend is the risk signal.
    states = ["COMPLETED"] * 4 + ["FAILED", "FAILED", "FAILED", "COMPLETED"]
    runs = [_run("regressing", s, f"2026-01-{i:02d}T00:00:00") for i, s in enumerate(states, start=1)]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.success_rate >= leaderboard.RISKY_SUCCESS_RATE
    assert entry.trend == leaderboard.TREND_DOWN
    assert entry.tier == leaderboard.TIER_RISKY


def test_middling_stable_rate_is_challenger():
    states = ["COMPLETED", "FAILED", "COMPLETED", "FAILED", "COMPLETED", "COMPLETED"]
    runs = [_run("mid", s, f"2026-01-{i:02d}T00:00:00") for i, s in enumerate(states, start=1)]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert leaderboard.RISKY_SUCCESS_RATE <= entry.success_rate < leaderboard.TOP_SUCCESS_RATE
    assert entry.tier == leaderboard.TIER_CHALLENGER


def test_improving_trend_is_up():
    states = ["FAILED", "FAILED", "COMPLETED", "COMPLETED"]
    runs = [_run("improving", s, f"2026-01-{i:02d}T00:00:00") for i, s in enumerate(states, start=1)]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.trend == leaderboard.TREND_UP
    assert entry.trend_delta == 1.0


def test_trend_is_insufficient_data_below_min_runs_for_trend():
    runs = [_run("new", "COMPLETED", "2026-01-01T00:00:00"), _run("new", "FAILED", "2026-01-02T00:00:00")]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.trend == leaderboard.TREND_INSUFFICIENT_DATA
    assert entry.trend_delta is None
    assert entry.earlier_success_rate is None
    assert entry.recent_success_rate is None


def test_cancelled_and_in_flight_runs_count_toward_total_not_scored():
    runs = [
        _run("busy", "COMPLETED", "2026-01-01T00:00:00"),
        _run("busy", "COMPLETED", "2026-01-02T00:00:00"),
        _run("busy", "CANCELLED", "2026-01-03T00:00:00"),
        _run("busy", "RUNNING", "2026-01-04T00:00:00"),
        _run("busy", "QUEUED", "2026-01-05T00:00:00"),
    ]
    board = leaderboard.compute_leaderboard(runs)
    entry = board.entries[0]
    assert entry.total_runs == 5
    assert entry.scored_runs == 2
    # Still below MIN_RUNS_FOR_RATED, so Newcomer despite a perfect scored rate.
    assert entry.tier == leaderboard.TIER_NEWCOMER
    assert entry.last_run_at == "2026-01-05T00:00:00"


def test_board_orders_top_before_challenger_before_newcomer_before_risky():
    top_runs = [_run("top-agent", "COMPLETED", f"2026-01-{i:02d}T00:00:00") for i in range(1, 7)]
    challenger_states = ["COMPLETED", "FAILED", "COMPLETED", "FAILED", "COMPLETED", "COMPLETED"]
    challenger_runs = [
        _run("challenger-agent", s, f"2026-02-{i:02d}T00:00:00") for i, s in enumerate(challenger_states, start=1)
    ]
    newcomer_runs = [_run("newcomer-agent", "COMPLETED", "2026-03-01T00:00:00")]
    risky_states = ["FAILED"] * 5 + ["COMPLETED"]
    risky_runs = [_run("risky-agent", s, f"2026-04-{i:02d}T00:00:00") for i, s in enumerate(risky_states, start=1)]

    board = leaderboard.compute_leaderboard(top_runs + challenger_runs + newcomer_runs + risky_runs)
    tiers_in_order = [entry.tier for entry in board.entries]
    assert tiers_in_order == [
        leaderboard.TIER_TOP,
        leaderboard.TIER_CHALLENGER,
        leaderboard.TIER_NEWCOMER,
        leaderboard.TIER_RISKY,
    ]


def test_within_tier_sorted_by_success_rate_then_agent_name():
    # Two Top-tier agents, both above the TOP threshold but at different rates.
    runs = [_run("aaa", "COMPLETED", f"2026-01-{i:02d}T00:00:00") for i in range(1, 7)]
    # "bbb"'s one failure sits in its earlier half, so the recent half is a
    # clean run of successes: rate 7/8 = 0.875 clears TOP_SUCCESS_RATE and the
    # trend is improving, not declining (isolates the sort-order assertion
    # from the decline-forces-Risky rule covered elsewhere).
    runs.append(_run("bbb", "FAILED", "2026-02-01T00:00:00"))
    runs += [_run("bbb", "COMPLETED", f"2026-02-{i:02d}T00:00:00") for i in range(2, 9)]
    board = leaderboard.compute_leaderboard(runs)
    assert [e.agent for e in board.entries] == ["aaa", "bbb"]
    assert board.entries[0].tier == leaderboard.TIER_TOP
    assert board.entries[1].tier == leaderboard.TIER_TOP


def test_every_agent_with_a_run_gets_exactly_one_entry():
    runs = [_run("solo", "RUNNING", "2026-01-01T00:00:00")]
    board = leaderboard.compute_leaderboard(runs)
    assert len(board.entries) == 1
    entry = board.entries[0]
    assert entry.total_runs == 1
    assert entry.scored_runs == 0
    assert entry.success_rate is None
    assert entry.tier == leaderboard.TIER_NEWCOMER
