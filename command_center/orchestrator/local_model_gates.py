"""The benchmark-gated promotion ledger for free, local-model executors
(VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR): per task class, is the bounded-
implementation lane (today: `aider` driving a local Ollama model) allowed to
be the FIRST thing `routing.cascade_for` dispatches to, or must every
dispatch still lead with a paid, proven executor?

Promotion is data, not a switch an operator merely flips: `record_benchmark_
run` accrues one sample at a time from real benchmark runs
(`scripts/aicc_aider_benchmark.py`), and a class is promoted only once its
CURRENT PROMOTION WINDOW has both `MIN_SAMPLES_FOR_PROMOTION` samples and a
pass rate at or above `PASS_RATE_BAR`.

Demotion durability (fix for the defect found in independent review of PR
#700 at b280dfc2, chunk 1/6): an operator's `demote` call is the ONLY way
promotion is revoked, and it must STAY revoked until fresh evidence
independently re-clears the bar -- never flip back as a side effect of the
very next benchmark sample. The original shape kept a single cumulative
`sample_count`/`pass_count` pair for both bookkeeping and the promotion
decision: `demote` zeroed `promoted` but left those cumulative counters
untouched (by design -- they are also the class's lifetime track record), so
a class that had already cleared the bar once would almost always still
clear it on the very next sample (one more sample rarely swings a >=20-
sample average below 0.9), silently re-promoting a class an operator had
just pulled for a real incident and overwriting their `notes` in the
process.

The fix splits bookkeeping in two:

- `sample_count`/`pass_count` are the LIFETIME totals, kept for
  observability (an operator asking "how has this class ever performed")
  and never reset by `demote`.
- `window_sample_count`/`window_pass_count` are what `meets_promotion_bar`
  actually judges, and `demote` resets BOTH to zero. Re-promotion after a
  demote therefore requires `MIN_SAMPLES_FOR_PROMOTION` genuinely fresh
  samples, accrued after the demote, to independently clear the bar again --
  exactly the "a separate, explicit call an operator makes on purpose, never
  an automatic side effect of ordinary benchmarking" invariant this module
  promises.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from command_center import models, storage

__all__ = [
    "MIN_SAMPLES_FOR_PROMOTION",
    "PASS_RATE_BAR",
    "BenchmarkState",
    "meets_promotion_bar",
    "record_benchmark_run",
    "demote",
    "load_state",
    "is_promoted",
    "apply_benchmark_run",
    "apply_demote",
]

#: A window narrower than this cannot say anything statistically meaningful
#: about a task class's real pass rate -- and, just as importantly, cannot
#: be gamed by cherry-picking a lucky handful of easy fixtures.
MIN_SAMPLES_FOR_PROMOTION = 20
#: 90%: high enough that a class promoted into the free lane is genuinely
#: reliable (every dispatch still goes through the unchanged CI/independent-
#: acceptance gates regardless -- this bar is about executor QUALITY, not
#: about relaxing those gates), low enough that one or two hard fixtures in
#: a 20-sample window do not permanently block promotion.
PASS_RATE_BAR = 0.9

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
GATES_FILE = DATA_DIR / "local_model_gates.jsonl"
GATES_LOCK_FILE = DATA_DIR / "local_model_gates.jsonl.lock"


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkState:
    """The promotion ledger for one task class. Immutable: every transition
    (`record_benchmark_run`, `demote`) returns a NEW state rather than
    mutating this one, so a caller can never accidentally observe a
    half-applied transition."""

    task_class: str
    #: Lifetime totals -- observability only, never reset by `demote`.
    sample_count: int = 0
    pass_count: int = 0
    #: The promotion decision's OWN window -- reset to zero by `demote`, so
    #: re-promotion needs fresh evidence. Equal to the lifetime totals for a
    #: class that has never been demoted.
    window_sample_count: int = 0
    window_pass_count: int = 0
    promoted: bool = False
    #: Set by `demote`, cleared by nothing (a later promotion is recorded by
    #: `record_benchmark_run`, which does not touch this field -- it is a
    #: permanent marker of "this class was demoted at least once", useful for
    #: an operator auditing the ledger's history).
    demoted_at: str | None = None
    notes: str = ""
    updated_at: str | None = None


def meets_promotion_bar(state: BenchmarkState) -> bool:
    """Whether `state`'s CURRENT WINDOW alone clears the promotion bar.

    Deliberately reads only `window_sample_count`/`window_pass_count`, never
    the lifetime totals -- see the module docstring for why that split is
    the fix, not an incidental detail.
    """
    if state.window_sample_count < MIN_SAMPLES_FOR_PROMOTION:
        return False
    return (state.window_pass_count / state.window_sample_count) >= PASS_RATE_BAR


def record_benchmark_run(
    state: BenchmarkState, passed: bool, *, note: str = ""
) -> BenchmarkState:
    """Accrue one real benchmark sample and return the resulting state.

    Promotion is decided purely from the updated WINDOW counters
    (`meets_promotion_bar`), so a class only flips `promoted=False ->
    True` here when its post-demote (or lifetime, if never demoted) samples
    have independently cleared the bar -- never from stale cumulative
    history a `demote` call was supposed to have invalidated.
    """
    now = models.iso_now()
    updated = dataclasses.replace(
        state,
        sample_count=state.sample_count + 1,
        pass_count=state.pass_count + (1 if passed else 0),
        window_sample_count=state.window_sample_count + 1,
        window_pass_count=state.window_pass_count + (1 if passed else 0),
        updated_at=now,
        notes=note or state.notes,
    )
    if not updated.promoted and meets_promotion_bar(updated):
        updated = dataclasses.replace(
            updated,
            promoted=True,
            notes=note
            or (
                f"auto-promoted: {updated.window_pass_count}/"
                f"{updated.window_sample_count} samples in the current "
                "promotion window cleared the bar"
            ),
            updated_at=now,
        )
    return updated


def demote(state: BenchmarkState, reason: str) -> BenchmarkState:
    """An operator's explicit, on-purpose revocation of promotion.

    Resets the promotion WINDOW to zero (not the lifetime totals) so the
    very next `record_benchmark_run` cannot re-promote on stale cumulative
    history -- see the module docstring. `reason` always overwrites
    `notes`: an operator demoting a class is recording an incident, and that
    record must not be silently discarded by a later auto-promotion message
    the way the pre-fix code could overwrite it.
    """
    now = models.iso_now()
    return dataclasses.replace(
        state,
        promoted=False,
        window_sample_count=0,
        window_pass_count=0,
        demoted_at=now,
        notes=reason,
        updated_at=now,
    )


def _to_record(state: BenchmarkState) -> dict[str, Any]:
    return dataclasses.asdict(state)


def _from_record(record: dict[str, Any]) -> BenchmarkState:
    known = {f.name for f in dataclasses.fields(BenchmarkState)}
    return BenchmarkState(**{key: value for key, value in record.items() if key in known})


def load_state(task_class: str) -> BenchmarkState:
    """The latest recorded state for `task_class`, or an unpromoted zero
    state if the ledger has never seen it."""
    storage.ensure_seeded_jsonl(GATES_FILE)
    records = storage.read_jsonl(GATES_FILE)
    latest = storage.fold_latest_by_id(records, id_key="task_class")
    record = latest.get(task_class)
    return _from_record(record) if record is not None else BenchmarkState(task_class=task_class)


def is_promoted(task_class: str) -> bool:
    """Whether `routing.cascade_for` may currently lead with the free
    executor for `task_class` -- the one predicate `routing.py` needs from
    this module."""
    return load_state(task_class).promoted


def apply_benchmark_run(task_class: str, passed: bool, *, note: str = "") -> BenchmarkState:
    """Record one real benchmark sample for `task_class`, durably.

    The read-modify-write cycle (current state -> `record_benchmark_run` ->
    append) is held under `GATES_LOCK_FILE` for its whole span, so two
    benchmark runs racing on the same task class cannot both read the same
    pre-write state and silently discard one sample (see
    `command_center.storage.file_lock`).
    """
    with storage.file_lock(GATES_LOCK_FILE):
        current = load_state(task_class)
        updated = record_benchmark_run(current, passed, note=note)
        storage.append_jsonl(GATES_FILE, _to_record(updated))
    return updated


def apply_demote(task_class: str, reason: str) -> BenchmarkState:
    """Durable counterpart to `demote` -- the entry point an operator's
    tooling calls to pull a task class out of the free lane."""
    with storage.file_lock(GATES_LOCK_FILE):
        current = load_state(task_class)
        updated = demote(current, reason)
        storage.append_jsonl(GATES_FILE, _to_record(updated))
    return updated
