"""Benchmark-driven promotion gate for the aider+Ollama executor lane
(VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR).

Owner decision (2026-09-03): aider over a local Ollama model is a free
bounded-implementation executor for low-risk task classes, but "quality is
benchmarked per task class before promotion" — a task class must not start
routing to a free local model just because the plumbing exists. This module
is the single writer of that promotion ledger
(`data/local_model_gates.json`), mirroring `dispatch.policy_config`'s
atomic-write-under-lock shape so a concurrent benchmark run and a live read
from `orchestrator.routing.cascade_for` can never tear the file or lose an
update.

Fail closed by construction: `is_promoted` treats an unknown task class, a
missing file, or a record that has not yet cleared the sample-size/pass-rate
bar as **not promoted** — the aider link is then filtered out of the cascade
by `routing.cascade_for` and the task falls through to the proven claude /
codex / copilot chain. A class earns promotion only by accumulating evidence
through `record_benchmark_run` (see `ops/aicc_aider_benchmark.py`, which runs
a fixed task suite through the aider executor and calls this module once per
result); nothing here ever promotes a class from prompt text or task
metadata alone.

Promotion is a ratchet, not a live score: once a class clears the bar it
stays promoted through `record_benchmark_run` even if a later sample fails
(one flaky benchmark task must not silently revert live routing back to the
cloud cascade under an operator's nose). Revoking promotion after a real
regression is `demote` — a separate, explicit call an operator makes on
purpose, never an automatic side effect of ordinary benchmarking.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from command_center import models, storage

__all__ = [
    "GateRecord",
    "MIN_BENCHMARK_SAMPLES",
    "MIN_PASS_RATE",
    "demote",
    "gates_file_path",
    "is_promoted",
    "load_gates",
    "meets_promotion_bar",
    "record_benchmark_run",
]

GATES_FILE_NAME = "local_model_gates.json"
GATES_LOCK_FILE_NAME = "local_model_gates.lock"

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05

#: A task class must accumulate at least this many benchmark samples before
#: it can promote at all — a handful of lucky runs proves nothing at the
#: volume this lane is meant to serve.
MIN_BENCHMARK_SAMPLES = 20
#: ...and its cumulative pass rate (through the SAME unchanged gates every
#: other executor is judged by — CI, independent acceptance) must be at or
#: above this to promote. Deliberately high: a free executor whose failures
#: still cost a human a review cycle is not actually free.
MIN_PASS_RATE = 0.9


def gates_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / GATES_FILE_NAME


def gates_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / GATES_LOCK_FILE_NAME


@dataclasses.dataclass(frozen=True)
class GateRecord:
    task_class: str
    sample_count: int = 0
    pass_count: int = 0
    promoted: bool = False
    last_benchmark_at: str | None = None
    promoted_at: str | None = None
    updated_by: str | None = None
    notes: str = ""

    @property
    def pass_rate(self) -> float:
        return self.pass_count / self.sample_count if self.sample_count else 0.0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, task_class: str, data: object) -> "GateRecord":
        if not isinstance(data, dict):
            return cls(task_class=task_class)
        return cls(
            task_class=task_class,
            sample_count=max(0, _safe_int(data.get("sample_count"))),
            pass_count=max(0, _safe_int(data.get("pass_count"))),
            promoted=bool(data.get("promoted")),
            last_benchmark_at=_safe_str_or_none(data.get("last_benchmark_at")),
            promoted_at=_safe_str_or_none(data.get("promoted_at")),
            updated_by=_safe_str_or_none(data.get("updated_by")),
            notes=str(data.get("notes") or ""),
        )


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _safe_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def meets_promotion_bar(record: GateRecord) -> bool:
    return (
        record.sample_count >= MIN_BENCHMARK_SAMPLES
        and record.pass_rate >= MIN_PASS_RATE
    )


def load_gates(root: Path) -> dict[str, GateRecord]:
    """Read the persisted ledger, or an empty one if nothing was benchmarked
    yet. Unlocked by design (a plain read of an atomically-written file)."""
    raw = storage.read_json(gates_file_path(root), {})
    if not isinstance(raw, dict):
        return {}
    return {
        task_class: GateRecord.from_dict(task_class, data)
        for task_class, data in raw.items()
        if isinstance(task_class, str) and task_class
    }


def is_promoted(task_class: str, root: Path) -> bool:
    """Whether `task_class` has cleared the benchmark bar. Fail closed: a
    task class with no record at all (never benchmarked) is not promoted,
    exactly the same as one that was benchmarked and fell short."""
    return load_gates(root).get(task_class, GateRecord(task_class=task_class)).promoted


def _write_gates(root: Path, gates: dict[str, GateRecord]) -> None:
    storage.atomic_write_json(
        gates_file_path(root), {key: record.as_dict() for key, record in gates.items()}
    )


def record_benchmark_run(
    root: Path, task_class: str, *, passed: bool, actor: str | None = None
) -> GateRecord:
    """Append one benchmark result and recompute promotion under the lock.

    Read-modify-write under `file_lock` so two benchmark runs (or a benchmark
    run racing a manual `demote`) cannot silently discard one another's
    sample count — the same lost-update hazard `policy_config.update_policy`
    guards against for the dispatch policy file.
    """
    with storage.file_lock(
        gates_lock_path(root), timeout=_LOCK_TIMEOUT_SECONDS, poll_seconds=_LOCK_POLL_SECONDS
    ):
        gates = load_gates(root)
        current = gates.get(task_class, GateRecord(task_class=task_class))
        updated = dataclasses.replace(
            current,
            sample_count=current.sample_count + 1,
            pass_count=current.pass_count + (1 if passed else 0),
            last_benchmark_at=models.iso_now(),
            updated_by=actor or current.updated_by,
        )
        if not updated.promoted and meets_promotion_bar(updated):
            updated = dataclasses.replace(
                updated,
                promoted=True,
                promoted_at=models.iso_now(),
                notes=(
                    f"auto-promoted after {updated.sample_count} benchmark "
                    f"samples ({updated.pass_rate:.0%} pass rate)"
                ),
            )
        gates[task_class] = updated
        _write_gates(root, gates)
        return updated


def demote(root: Path, task_class: str, *, reason: str, actor: str | None = None) -> GateRecord:
    """Explicit operator action: revoke a class's promotion (regression /
    incident), independent of `record_benchmark_run`'s accrual so a single
    incident can pull a class back to the cloud cascade immediately without
    waiting for the running sample count to fall below the bar (it never
    would, since accrual never un-counts prior passes)."""
    with storage.file_lock(
        gates_lock_path(root), timeout=_LOCK_TIMEOUT_SECONDS, poll_seconds=_LOCK_POLL_SECONDS
    ):
        gates = load_gates(root)
        current = gates.get(task_class, GateRecord(task_class=task_class))
        updated = dataclasses.replace(
            current, promoted=False, promoted_at=None, notes=reason, updated_by=actor
        )
        gates[task_class] = updated
        _write_gates(root, gates)
        return updated
