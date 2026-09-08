"""Cost-per-meaningful-task reporting (VOYN-AGT-PERF-PAY).

FinOps asked for an agent's *hourly cost by quality*: how much a project spends
per agent for each **meaningful** task it lands, not per task it merely
attempts. Ranking agents on raw task count rewards the one that opens the most
cheap, low-value runs; this module ranks them on realized spend divided by
work that actually shipped.

"Meaningful" is defined the same way the completion pipeline already defines
"done": a run's task counts only once its `completion.completion_state`
reaches `completion.CompletionState.COMPLETED` — the sole state that means
"merged into the target branch and verified" (see `runtime.completion`'s
module docstring for the other candidate states this deliberately excludes,
e.g. `PULL_REQUEST_OPEN` or `REQUIRES_ATTENTION`). A run that burned real
provider spend but never reached that state still contributes to
`total_cost_usd` and `run_count` — its cost does not disappear — it just does
not count as a landed task in the denominator.

Cost itself is read from the same single truthful source
`task_pipeline.daily_spend_usd` already trusts for budget gating: each
provider's own reported `total_cost_usd` on its run's stream events. Nothing
here estimates a dollar figure from duration or token counts — a run whose
provider reported no cost contributes `0.0`, never a guess.

Two layers, mirroring the rest of `runtime`:

  * `fetch_agent_cost_rows` — the only place that touches SQL; reduces the
    `run`/`run_event`/`completion` tables to one `AgentCostRow` per run.
  * `build_agent_cost_report` / `AgentProjectCost` — pure aggregation and the
    report shape, fully unit-testable without a database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from command_center.runtime import completion as completion_domain
from command_center.runtime import db as runtime_db

_LOG = logging.getLogger(__name__)

# `run.provider_id` when a row predates the executor-provider migration (all
# rows created before VOYN-W0's provider fields shipped default to this
# value at the schema level too — see `_migration_9_add_execution_provider_fields`).
_DEFAULT_AGENT = "claude_code"


@dataclass(frozen=True, slots=True)
class AgentCostRow:
    """One run's cost inputs, already reduced to what the report needs."""

    project: str
    agent: str
    task_id: str
    total_cost_usd: float
    is_meaningful: bool


@dataclass(frozen=True, slots=True)
class AgentProjectCost:
    """One (project, agent) entry in the finished report."""

    project: str
    agent: str
    total_cost_usd: float
    run_count: int
    meaningful_task_count: int

    @property
    def cost_per_meaningful_task_usd(self) -> float | None:
        """`None` — not `0.0` and not `inf` — when this agent has not landed a
        single meaningful task on this project yet. A real "no data" distinct
        from "free", matching this codebase's fail-closed-on-unmeasured
        convention (see `dispatch.models.SpendMeasurement`): a report reader
        must never mistake "nothing has shipped" for "shipping costs nothing"."""
        if self.meaningful_task_count <= 0:
            return None
        return self.total_cost_usd / self.meaningful_task_count

    def as_dict(self) -> dict:
        cost_per_task = self.cost_per_meaningful_task_usd
        return {
            "project": self.project,
            "agent": self.agent,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "run_count": self.run_count,
            "meaningful_task_count": self.meaningful_task_count,
            "cost_per_meaningful_task_usd": (
                None if cost_per_task is None else round(cost_per_task, 4)
            ),
        }


def build_agent_cost_report(rows: list[AgentCostRow]) -> list[AgentProjectCost]:
    """Aggregate per-run cost rows into one entry per `(project, agent)`.

    A task counts as meaningful for a `(project, agent)` pair once, even if it
    took several runs (retries, resumes) by that same agent to land:
    `meaningful_task_count` counts distinct completed task ids, never raw
    completed-run rows, so retries don't inflate the numerator the report is
    meant to police (churn) into the denominator that's supposed to reward
    real delivery.

    Sorted by `(project, agent)` so the rendered report is deterministic.
    """
    totals: dict[tuple[str, str], dict] = {}
    completed_tasks: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (row.project, row.agent)
        bucket = totals.setdefault(key, {"total_cost_usd": 0.0, "run_count": 0})
        bucket["total_cost_usd"] += row.total_cost_usd
        bucket["run_count"] += 1
        if row.is_meaningful:
            completed_tasks.setdefault(key, set()).add(row.task_id)

    results = [
        AgentProjectCost(
            project=project,
            agent=agent,
            total_cost_usd=bucket["total_cost_usd"],
            run_count=bucket["run_count"],
            meaningful_task_count=len(completed_tasks.get((project, agent), ())),
        )
        for (project, agent), bucket in totals.items()
    ]
    return sorted(results, key=lambda entry: (entry.project, entry.agent))


def fetch_agent_cost_rows(db_path: Path, *, project: str | None = None) -> list[AgentCostRow]:
    """Read the raw per-run inputs the report needs.

    Mirrors `task_pipeline.daily_spend_usd`'s cost extraction exactly (same
    `LIKE '%total_cost_usd%'` prefilter, same summed-across-matching-events
    read of the provider-reported figure, same tolerance for a `payload_json`
    that a jsonb-backed mirror hands back already decoded) so the two numbers
    a FinOps reader compares — "spend today" and "spend per meaningful task"
    — are never computed by two subtly different cost readers.
    """
    clauses: list[str] = []
    params: list[str] = []
    if project:
        clauses.append("run.project = ?")
        params.append(project)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with runtime_db.connect(db_path) as conn:
        run_rows = conn.execute(
            f"""
            SELECT run.id AS run_id, run.project AS project,
                   run.provider_id AS agent, run.task_id AS task_id
            FROM run
            {where}
            """,
            params,
        ).fetchall()
        run_ids = [row["run_id"] for row in run_rows]
        if not run_ids:
            return []

        placeholders = ", ".join("?" for _ in run_ids)
        cost_rows = conn.execute(
            f"""
            SELECT run_event.run_id AS run_id, run_event.payload_json AS payload
            FROM run_event
            WHERE run_event.run_id IN ({placeholders})
              AND CAST(run_event.payload_json AS TEXT) LIKE '%total_cost_usd%'
            """,
            run_ids,
        ).fetchall()
        completion_rows = conn.execute(
            f"""
            SELECT run_id, completion_state FROM completion
            WHERE run_id IN ({placeholders})
            """,
            run_ids,
        ).fetchall()

    cost_by_run: dict[str, float] = {}
    for row in cost_rows:
        payload = row["payload"]
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except ValueError:
                _LOG.warning(
                    "fetch_agent_cost_rows: skipping run_event with unparseable "
                    "payload_json for run %s",
                    row["run_id"],
                )
                continue
        if not isinstance(payload, dict):
            _LOG.warning(
                "fetch_agent_cost_rows: skipping run_event whose payload is a "
                "%s, not an object, for run %s",
                type(payload).__name__,
                row["run_id"],
            )
            continue
        cost = payload.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_by_run[row["run_id"]] = cost_by_run.get(row["run_id"], 0.0) + float(cost)

    completion_state_by_run = {row["run_id"]: row["completion_state"] for row in completion_rows}

    return [
        AgentCostRow(
            project=row["project"],
            agent=row["agent"] or _DEFAULT_AGENT,
            task_id=row["task_id"],
            total_cost_usd=cost_by_run.get(row["run_id"], 0.0),
            is_meaningful=(
                completion_state_by_run.get(row["run_id"])
                == completion_domain.CompletionState.COMPLETED
            ),
        )
        for row in run_rows
    ]


def build_agent_cost_report_from_db(
    db_path: Path, *, project: str | None = None
) -> list[AgentProjectCost]:
    """Convenience wrapper: fetch + aggregate in one call for callers (the CLI
    report, an eventual API route) that don't need the intermediate rows."""
    return build_agent_cost_report(fetch_agent_cost_rows(db_path, project=project))


def render_agent_cost_report_markdown(rows: list[AgentProjectCost]) -> str:
    """Render the acceptance-criteria artifact: one table row per agent per
    project, ranked by realized cost per meaningful task within each project
    (cheapest-and-most-reliable first; agents with no landed task yet sort
    last within their project since `None` cannot be ranked against a price)."""
    lines = [
        "# Стоимость решений по агентам и проектам",
        "",
        "| Проект | Агент | Запусков | Значимых задач | Общая стоимость, $ | $/значимая задача |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for entry in sorted(
        rows,
        key=lambda e: (
            e.project,
            e.cost_per_meaningful_task_usd is None,
            e.cost_per_meaningful_task_usd
            if e.cost_per_meaningful_task_usd is not None
            else 0.0,
        ),
    ):
        cost_per_task = entry.cost_per_meaningful_task_usd
        cost_per_task_str = f"{cost_per_task:.4f}" if cost_per_task is not None else "—"
        lines.append(
            f"| {entry.project} | {entry.agent} | {entry.run_count} | "
            f"{entry.meaningful_task_count} | {entry.total_cost_usd:.4f} | "
            f"{cost_per_task_str} |"
        )
    if len(lines) == 4:
        lines.append("| _нет данных_ | | | | | |")
    return "\n".join(lines) + "\n"
