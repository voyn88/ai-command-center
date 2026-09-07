"""The per-task-class benchmark ledger gating the aider+Ollama executor lane
(AICC Fleet decision 2026-09-03: bounded-implementation task classes may
dispatch to the free local-model executor only after its measured quality
clears a bar, never on install alone).

Storage: `data/local_model_gates.jsonl`, append-only JSON Lines (the same
convention as `data/runs.jsonl` -- see `command_center.storage`'s module
docstring). Each line is a full `GateRecord` snapshot for one `task_class`;
the "current" state is the last line seen for that class (last-write-wins
fold, `current_record`). The ledger ships empty, so an unbenchmarked class
folds to the safe default: not promoted, zero samples.

Two, and only two, ways a record's `promoted` flag changes:

- `record_benchmark_run` accrues one more sample into the cumulative
  `sample_count`/`pass_count` and flips `promoted` to `True` the moment the
  cumulative rate clears `meets_promotion_bar` -- an automatic, ordinary
  side effect of benchmarking, never reversed by this function.
- `demote` is the only way `promoted` goes back to `False`: a separate,
  explicit call an operator makes on purpose after a real regression, never
  a side effect of ordinary benchmarking. It resets `sample_count` and
  `pass_count` to zero along with `promoted`, so re-promotion after a
  demotion requires `PROMOTION_SAMPLE_FLOOR` genuinely fresh samples to
  independently clear the bar again -- not one more sample added on top of a
  cumulative total that was already well past it. Leaving the historical
  counts in place after a demotion was tried and rejected: with a
  20-sample/90%-pass bar, one additional sample essentially never swings a
  cumulative average that has already cleared it, so the very next ordinary
  benchmark run would silently flip `promoted` back to `True` and overwrite
  the operator's incident `reason`/`notes` with an auto-promotion message --
  defeating the entire point of an operator-triggered demotion.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from command_center import storage

__all__ = [
    "PROMOTION_SAMPLE_FLOOR",
    "PROMOTION_PASS_RATE_FLOOR",
    "LEDGER_FILE",
    "LEDGER_LOCK_FILE",
    "GateRecord",
    "meets_promotion_bar",
    "current_record",
    "is_promoted",
    "record_benchmark_run",
    "demote",
]

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
LEDGER_FILE = DATA_DIR / "local_model_gates.jsonl"
#: A dedicated lock file, never the ledger itself (see `storage.file_lock`):
#: a lock holder must never block a plain unlocked `current_record` read.
LEDGER_LOCK_FILE = DATA_DIR / "local_model_gates.lock"

#: The promotion bar (AICC Fleet decision 2026-09-03): at least this many
#: samples, AND a cumulative pass rate at or above the floor below. Both
#: conditions are required -- a 100% pass rate on 3 samples proves nothing
#: about a free local model's reliability, and a 90% rate on 1000 samples is
#: a genuinely different (better-supported) claim than the same rate on 20.
PROMOTION_SAMPLE_FLOOR = 20
PROMOTION_PASS_RATE_FLOOR = 0.9


@dataclasses.dataclass(frozen=True, slots=True)
class GateRecord:
    task_class: str
    promoted: bool = False
    sample_count: int = 0
    pass_count: int = 0
    #: Machine tag for why the record is in this state ("auto_promoted", or
    #: an operator's own short code for a demotion). Never inferred.
    reason: str = ""
    #: Free-text operator/benchmark context (an incident link, a benchmark
    #: run id). Only ever set by the call that produced this exact snapshot.
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GateRecord":
        return cls(
            task_class=str(data.get("task_class", "")),
            promoted=bool(data.get("promoted", False)),
            sample_count=int(data.get("sample_count", 0)),
            pass_count=int(data.get("pass_count", 0)),
            reason=str(data.get("reason", "")),
            notes=str(data.get("notes", "")),
        )


def meets_promotion_bar(record: GateRecord) -> bool:
    if record.sample_count < PROMOTION_SAMPLE_FLOOR:
        return False
    return (record.pass_count / record.sample_count) >= PROMOTION_PASS_RATE_FLOOR


def current_record(task_class: str) -> GateRecord:
    """Last-write-wins fold over the ledger for `task_class`. A class with no
    line in the ledger at all -- never benchmarked -- folds to the safe
    default: not promoted, zero samples."""
    storage.ensure_seeded_jsonl(LEDGER_FILE)
    latest: GateRecord | None = None
    for raw in storage.read_jsonl(LEDGER_FILE):
        if raw.get("task_class") != task_class:
            continue
        latest = GateRecord.from_dict(raw)
    return latest or GateRecord(task_class=task_class)


def is_promoted(task_class: str) -> bool:
    return current_record(task_class).promoted


def record_benchmark_run(task_class: str, passed: bool) -> GateRecord:
    """Append one benchmark sample for `task_class` and fold it into the
    cumulative promotion decision.

    Held under `LEDGER_LOCK_FILE` across its whole read-modify-write span:
    without the lock, two concurrent benchmark runs reading the same
    pre-write sample/pass counts would each append a new line computed from
    that stale snapshot, and the second write would silently discard the
    first caller's sample from the cumulative total -- the exact
    read-modify-write hazard `command_center.storage.file_lock` documents
    and exists to close.
    """
    with storage.file_lock(LEDGER_LOCK_FILE):
        record = current_record(task_class)
        updated = dataclasses.replace(
            record,
            sample_count=record.sample_count + 1,
            pass_count=record.pass_count + (1 if passed else 0),
        )
        if not updated.promoted and meets_promotion_bar(updated):
            updated = dataclasses.replace(
                updated,
                promoted=True,
                reason="auto_promoted",
                notes=(
                    f"{updated.pass_count}/{updated.sample_count} cumulative "
                    "benchmark samples cleared the promotion bar"
                ),
            )
        storage.append_jsonl(LEDGER_FILE, updated.to_dict())
        return updated


def demote(task_class: str, *, reason: str, notes: str = "") -> GateRecord:
    """An operator's explicit, on-purpose revocation of promotion.

    Resets `sample_count`/`pass_count` to zero along with `promoted=False` --
    see the module docstring for why leaving the cumulative counts in place
    is unsafe. `reason` is required (never a silent no-op default) because a
    demotion with no recorded cause defeats the entire point of keeping an
    auditable incident trail in the ledger.
    """
    if not reason:
        raise ValueError("demote requires a non-empty reason")
    with storage.file_lock(LEDGER_LOCK_FILE):
        updated = GateRecord(
            task_class=task_class,
            promoted=False,
            sample_count=0,
            pass_count=0,
            reason=reason,
            notes=notes,
        )
        storage.append_jsonl(LEDGER_FILE, updated.to_dict())
        return updated
