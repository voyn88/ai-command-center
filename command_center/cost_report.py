"""Report recorded runtime cost against verified completed tasks, per executor.

This is a read-only projection over the existing runtime records, not a new
execution or accounting engine. It does not include queue-only jobs or estimate
prices from tokens. Missing provider cost is unknown, including for failed runs.
For each run, use its latest valid terminal result's cumulative USD cost once;
replayed results must not double-count that run. Each completed task counts once
per executor whose run reached the existing verified COMPLETED state.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from command_center.runtime import db as runtime_db
from command_center.runtime.completion import SUCCESS_STATE


@dataclass(frozen=True)
class AgentCostReport:
    agent_id: str
    known_cost_usd: float
    run_count: int
    covered_run_count: int
    meaningful_task_count: int

    @property
    def unknown_cost_run_count(self) -> int:
        return self.run_count - self.covered_run_count

    @property
    def cost_coverage(self) -> float:
        return self.covered_run_count / self.run_count if self.run_count else 0.0

    @property
    def total_cost_usd(self) -> float | None:
        return self.known_cost_usd if not self.unknown_cost_run_count else None

    @property
    def cost_per_meaningful_task_usd(self) -> float | None:
        if self.unknown_cost_run_count or not self.meaningful_task_count:
            return None
        return self.known_cost_usd / self.meaningful_task_count

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "known_cost_usd": self.known_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "run_count": self.run_count,
            "covered_run_count": self.covered_run_count,
            "unknown_cost_run_count": self.unknown_cost_run_count,
            "cost_coverage": self.cost_coverage,
            "meaningful_task_count": self.meaningful_task_count,
            "cost_per_meaningful_task_usd": self.cost_per_meaningful_task_usd,
        }


def _parse_result_cost(payload) -> float | None:
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return None
    cost = payload.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    try:
        value = float(cost)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def cost_per_meaningful_task(db_path: Path) -> list[AgentCostReport]:
    """Read one consistent snapshot; never migrate or create a missing database.

    Failed/retried runs contribute their known costs. Runs without a valid result
    contribute to the unknown count, so an incomplete subtotal is never a full rate.
    The monotonic per-run event sequence determines the latest valid result.
    """
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    with runtime_db.connect(db_path) as conn:
        conn.execute("BEGIN")
        runs = conn.execute("SELECT id, provider_id FROM run").fetchall()
        events = conn.execute(
            "SELECT run_id, payload_json FROM run_event "
            "WHERE event_type = 'stream_event' ORDER BY run_id, seq"
        ).fetchall()
        completions = conn.execute(
            "SELECT r.provider_id, c.task_id FROM completion c "
            "JOIN run r ON r.id = c.run_id AND r.task_id = c.task_id "
            "WHERE c.completion_state = ?",
            (SUCCESS_STATE,),
        ).fetchall()

    cost_by_run: dict[str, float] = {}
    for event in events:
        cost = _parse_result_cost(event["payload_json"])
        if cost is not None:
            cost_by_run[event["run_id"]] = cost
    runs_by_agent: dict[str, list[str]] = {}
    for run in runs:
        runs_by_agent.setdefault(run["provider_id"] or "unknown", []).append(run["id"])
    tasks_by_agent: dict[str, set[str]] = {}
    for row in completions:
        tasks_by_agent.setdefault(row["provider_id"] or "unknown", set()).add(
            row["task_id"]
        )
    reports = []
    for agent_id, run_ids in sorted(runs_by_agent.items()):
        costs = [cost_by_run[run_id] for run_id in run_ids if run_id in cost_by_run]
        reports.append(
            AgentCostReport(
                agent_id=agent_id,
                known_cost_usd=math.fsum(costs),
                run_count=len(run_ids),
                covered_run_count=len(costs),
                meaningful_task_count=len(tasks_by_agent.get(agent_id, set())),
            )
        )
    return reports


def render_markdown(reports: list[AgentCostReport]) -> str:
    lines = [
        "# Recorded runtime cost per verified task, by agent",
        "",
        "Scope: recorded runtime runs only; queue-only jobs are not included.",
        "Known cost is a subtotal; UNKNOWN means cost coverage is incomplete.",
        "",
        (
            "| Agent | Known cost (USD) | Cost coverage (runs) | Verified tasks | "
            "Cost / verified task (USD) |"
        ),
        "|---|---:|---:|---:|---:|",
    ]
    if not reports:
        lines.append("| _no runs recorded_ | — | — | — | — |")
    for report in reports:
        rate = report.cost_per_meaningful_task_usd
        if rate is not None:
            rate_text = f"{rate:.4f}"
        elif report.unknown_cost_run_count:
            rate_text = "UNKNOWN (missing cost)"
        else:
            rate_text = "N/A (no verified tasks)"
        agent = (
            report.agent_id.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        )
        lines.append(
            f"| {agent} | {report.known_cost_usd:.4f} | "
            f"{report.covered_run_count}/{report.run_count} | "
            f"{report.meaningful_task_count} | {rate_text} |"
        )
    return "\n".join(lines) + "\n"
