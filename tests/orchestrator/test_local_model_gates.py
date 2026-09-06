"""The benchmark promotion ledger (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR).

Centered on the defect independent review found in PR #700 (b280dfc2,
chunk 1/6): `demote` was not durable -- the very next benchmark sample
could silently re-promote a class an operator had just pulled for an
incident, overwriting their `notes` in the process. These tests pin the
fix: promotion after a demote requires fresh, post-demote samples to
independently clear the bar.
"""

from __future__ import annotations

from command_center.orchestrator import local_model_gates as gates


def _promote_fresh(task_class: str = "bounded_implementation") -> gates.BenchmarkState:
    """A freshly-promoted state: exactly the promotion bar, all passes."""
    state = gates.BenchmarkState(task_class=task_class)
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION):
        state = gates.record_benchmark_run(state, True)
    assert state.promoted
    return state


# -- meets_promotion_bar -------------------------------------------------


def test_meets_promotion_bar_requires_the_minimum_sample_count():
    state = gates.BenchmarkState(
        task_class="x",
        window_sample_count=gates.MIN_SAMPLES_FOR_PROMOTION - 1,
        window_pass_count=gates.MIN_SAMPLES_FOR_PROMOTION - 1,
    )
    assert not gates.meets_promotion_bar(state)


def test_meets_promotion_bar_requires_the_pass_rate():
    state = gates.BenchmarkState(
        task_class="x",
        window_sample_count=gates.MIN_SAMPLES_FOR_PROMOTION,
        window_pass_count=int(gates.MIN_SAMPLES_FOR_PROMOTION * gates.PASS_RATE_BAR) - 1,
    )
    assert not gates.meets_promotion_bar(state)


def test_meets_promotion_bar_true_at_exactly_the_bar():
    state = gates.BenchmarkState(
        task_class="x",
        window_sample_count=gates.MIN_SAMPLES_FOR_PROMOTION,
        window_pass_count=int(gates.MIN_SAMPLES_FOR_PROMOTION * gates.PASS_RATE_BAR),
    )
    assert gates.meets_promotion_bar(state)


# -- record_benchmark_run: normal accrual and promotion -------------------


def test_record_benchmark_run_accrues_both_lifetime_and_window_counts():
    state = gates.BenchmarkState(task_class="x")
    state = gates.record_benchmark_run(state, True)
    state = gates.record_benchmark_run(state, False)
    assert state.sample_count == 2 and state.pass_count == 1
    assert state.window_sample_count == 2 and state.window_pass_count == 1


def test_record_benchmark_run_promotes_once_the_window_clears_the_bar():
    state = gates.BenchmarkState(task_class="x")
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION - 1):
        state = gates.record_benchmark_run(state, True)
        assert not state.promoted
    state = gates.record_benchmark_run(state, True)
    assert state.promoted


def test_record_benchmark_run_never_promotes_below_the_pass_rate():
    state = gates.BenchmarkState(task_class="x")
    # Alternate pass/fail for well past the minimum sample count -- a 50%
    # pass rate must never clear a 90% bar no matter how many samples pile
    # up.
    for i in range(gates.MIN_SAMPLES_FOR_PROMOTION * 3):
        state = gates.record_benchmark_run(state, i % 2 == 0)
    assert not state.promoted


# -- demote: the actual fix -----------------------------------------------


def test_demote_clears_promoted_and_stamps_a_reason():
    state = _promote_fresh()
    demoted = gates.demote(state, "regression: incident VOYN-W0-FOO")
    assert demoted.promoted is False
    assert demoted.notes == "regression: incident VOYN-W0-FOO"
    assert demoted.demoted_at is not None


def test_demote_preserves_lifetime_counters_for_observability():
    state = _promote_fresh()
    demoted = gates.demote(state, "incident")
    assert demoted.sample_count == state.sample_count
    assert demoted.pass_count == state.pass_count


def test_demote_resets_the_promotion_window():
    state = _promote_fresh()
    demoted = gates.demote(state, "incident")
    assert demoted.window_sample_count == 0
    assert demoted.window_pass_count == 0


def test_the_very_next_sample_after_a_demote_cannot_re_promote():
    """The core regression this module exists to fix: a demote followed by
    exactly ONE more benchmark sample (pass or fail) must never flip
    `promoted` back to True. Under the pre-fix behaviour (cumulative
    sample_count/pass_count left untouched by demote), a class that had
    already cleared a >=20-sample bar would almost always still clear it on
    the 21st sample -- this asserts that no longer happens."""
    state = _promote_fresh()
    demoted = gates.demote(state, "regression found in prod")
    reaccrued = gates.record_benchmark_run(demoted, True)
    assert reaccrued.promoted is False
    assert reaccrued.notes == "regression found in prod", (
        "a single post-demote sample must not overwrite the operator's "
        "incident note with an auto-promotion message"
    )


def test_many_samples_after_a_demote_still_do_not_promote_until_the_window_clears():
    state = _promote_fresh()
    demoted = gates.demote(state, "regression")
    current = demoted
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION - 1):
        current = gates.record_benchmark_run(current, True)
        assert not current.promoted
    # One sample short of the fresh window's own bar: still demoted.
    assert current.promoted is False


def test_a_demoted_class_re_promotes_once_fresh_samples_independently_clear_the_bar():
    """The other half of the fix: demote is not a permanent ban -- it is a
    genuine reset. Enough FRESH, passing samples after the demote must be
    able to re-promote, proving the fix is a reset, not a one-way gate."""
    state = _promote_fresh()
    demoted = gates.demote(state, "regression")
    current = demoted
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION):
        current = gates.record_benchmark_run(current, True)
    assert current.promoted is True
    assert current.window_sample_count == gates.MIN_SAMPLES_FOR_PROMOTION


def test_demote_is_the_only_thing_that_revokes_promotion():
    """record_benchmark_run itself must never flip promoted True -> False --
    only an explicit `demote` call does. A failing sample after promotion
    just accrues into the window; it does not auto-demote."""
    state = _promote_fresh()
    for _ in range(5):
        state = gates.record_benchmark_run(state, False)
    assert state.promoted is True


# -- persistence: load/apply round-trip through the JSONL ledger ---------


def test_apply_benchmark_run_persists_and_reloads():
    gates.apply_benchmark_run("persist-class", True)
    gates.apply_benchmark_run("persist-class", True)
    state = gates.load_state("persist-class")
    assert state.sample_count == 2 and state.pass_count == 2


def test_load_state_for_an_unknown_class_is_a_fresh_unpromoted_state():
    state = gates.load_state("never-seen-this-class")
    assert state.sample_count == 0 and state.promoted is False


def test_is_promoted_reflects_the_ledger():
    assert gates.is_promoted("promotable-class") is False
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION):
        gates.apply_benchmark_run("promotable-class", True)
    assert gates.is_promoted("promotable-class") is True


def test_apply_demote_is_durable_through_persistence_too():
    """The same regression test as `test_the_very_next_sample_after_a_demote_
    cannot_re_promote`, exercised through the real durable read-modify-write
    entry points rather than the pure functions directly."""
    for _ in range(gates.MIN_SAMPLES_FOR_PROMOTION):
        gates.apply_benchmark_run("durable-class", True)
    assert gates.is_promoted("durable-class") is True

    gates.apply_demote("durable-class", "incident")
    assert gates.is_promoted("durable-class") is False

    gates.apply_benchmark_run("durable-class", True)
    assert gates.is_promoted("durable-class") is False, (
        "one benchmark sample after a real demote must not silently "
        "re-promote the class"
    )
