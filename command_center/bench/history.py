"""Agent hard-bench-set history (VOYN-AGT-HARD-BENCH).

Persists weekly bench runs as append-only JSON Lines, the same shape
`runs.jsonl`/`activity.jsonl` already use in this codebase (see
`command_center.storage`'s module docstring: every write is one appended
line, never a rewrite of prior content, so a crash mid-write cannot corrupt
history already on disk). This module deliberately opens no database
connection of its own — every durable write goes through
`command_center.storage`, the shared primitive every JSONL-backed module here
already delegates to, rather than a new store standing up its own engine.

Every case result a weekly pass produced is persisted, not just the rolled-up
score, so a leaderboard is always reconstructable from raw evidence rather
than trusted as an unverifiable number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from command_center import storage
from command_center.bench.types import CaseResult

ROOT = Path(__file__).resolve().parent.parent.parent


class BenchHistoryError(Exception):
    pass


class WeekAlreadyRecorded(BenchHistoryError):
    """A bench run for this `week_of` has already been recorded."""


def resolve_bench_dir(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "bench"


def _runs_path(bench_dir: Path) -> Path:
    return bench_dir / "runs.jsonl"


def _case_results_path(bench_dir: Path) -> Path:
    return bench_dir / "case_results.jsonl"


def _agent_scores_path(bench_dir: Path) -> Path:
    return bench_dir / "agent_scores.jsonl"


def _reports_path(bench_dir: Path) -> Path:
    return bench_dir / "reports.jsonl"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def record_run(bench_dir: Path, week_of: str) -> str:
    """Start a new weekly bench run. One run per `week_of`, ever."""
    existing = storage.read_jsonl(_runs_path(bench_dir))
    if any(row.get("week_of") == week_of for row in existing):
        raise WeekAlreadyRecorded(week_of)
    run_id = uuid.uuid4().hex
    storage.append_jsonl(
        _runs_path(bench_dir),
        {"id": run_id, "week_of": week_of, "created_at": _utcnow()},
    )
    return run_id


def record_case_result(bench_dir: Path, run_id: str, category: str, result: CaseResult) -> None:
    storage.append_jsonl(
        _case_results_path(bench_dir),
        {
            "run_id": run_id,
            "agent_id": result.agent_id,
            "case_id": result.case_id,
            "category": category,
            "passed": result.passed,
            "score": result.score,
            "evidence": result.evidence,
            "created_at": _utcnow(),
        },
    )


def record_agent_score(
    bench_dir: Path,
    run_id: str,
    agent_id: str,
    *,
    raw_score: float,
    stable_score: float,
    provisional: bool,
) -> None:
    storage.append_jsonl(
        _agent_scores_path(bench_dir),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "raw_score": raw_score,
            "stable_score": stable_score,
            "provisional": provisional,
            "created_at": _utcnow(),
        },
    )


def record_report(bench_dir: Path, run_id: str, week_of: str, markdown: str) -> None:
    storage.append_jsonl(
        _reports_path(bench_dir),
        {"run_id": run_id, "week_of": week_of, "markdown": markdown, "created_at": _utcnow()},
    )


def _week_by_run_id(bench_dir: Path) -> dict[str, str]:
    return {row["id"]: row["week_of"] for row in storage.read_jsonl(_runs_path(bench_dir))}


def latest_stable_scores(bench_dir: Path) -> dict[str, float]:
    """Each agent's stable score from its most recent prior run, by week."""
    week_by_run = _week_by_run_id(bench_dir)
    latest_week: dict[str, str] = {}
    latest_score: dict[str, float] = {}
    for row in storage.read_jsonl(_agent_scores_path(bench_dir)):
        agent_id = row["agent_id"]
        week = week_by_run.get(row["run_id"])
        if week is None:
            continue
        if agent_id not in latest_week or week > latest_week[agent_id]:
            latest_week[agent_id] = week
            latest_score[agent_id] = row["stable_score"]
    return latest_score


def run_counts(bench_dir: Path) -> dict[str, int]:
    """Number of weekly runs recorded so far for each agent."""
    counts: dict[str, int] = {}
    for row in storage.read_jsonl(_agent_scores_path(bench_dir)):
        counts[row["agent_id"]] = counts.get(row["agent_id"], 0) + 1
    return counts


def get_report(bench_dir: Path, week_of: str) -> str | None:
    for row in reversed(storage.read_jsonl(_reports_path(bench_dir))):
        if row.get("week_of") == week_of:
            return row["markdown"]
    return None


def list_runs(bench_dir: Path, *, limit: int = 20) -> list[dict]:
    runs = storage.read_jsonl(_runs_path(bench_dir))
    runs.sort(key=lambda row: row["week_of"], reverse=True)
    return runs[:limit]


def list_case_results(bench_dir: Path, run_id: str, agent_id: str) -> list[dict]:
    return [
        row
        for row in storage.read_jsonl(_case_results_path(bench_dir))
        if row.get("run_id") == run_id and row.get("agent_id") == agent_id
    ]
