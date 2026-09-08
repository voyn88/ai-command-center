"""Cost-per-meaningful-task reporting, grouped by agent (VOYN-MIN-AGT-COST-METRIC).

FinOps acceptance: "по каждому агенту есть cost-per-meaningful-task отчет" —
every agent must have a report that measures cost against *finished, verified
work*, never raw throughput (runs launched, tasks touched) and never raw
spend alone. A cheap agent that never finishes anything and an expensive
agent that finishes everything are both misjudged by counting runs or
dollars in isolation — this module divides one by the other, per agent.

Two existing, already-truthful primitives are combined; nothing here is
estimated or fabricated:

* **Cost** — the same source `task_pipeline.daily_spend_usd` uses: each run's
  own terminal `result` stream event's `total_cost_usd`, the only cost figure
  a provider itself reports for its own run. A run whose provider reported no
  cost contributes $0. This report is not windowed to a trailing 24h like the
  daily spend gate — it is a lifetime (or caller-filtered) rate.
* **Meaningful task** — a run whose `completion.completion_state` reached
  `runtime.completion.CompletionState.COMPLETED`
  (`runtime.completion.SUCCESS_STATE`): the single state that means the
  engineering task is actually done (merged and target-branch-verified), not
  merely attempted and not merely opened as a PR. `VALIDATION_FAILED`,
  `REVIEW_REJECTED`, `REQUIRES_ATTENTION` and `RECOVERY_FAILED` are all
  terminal too, but none of them are meaningful completions, so none count.

**Agent identity** is a run's own `provider_id` — the executor the run
actually executed on. This is the same notion `dispatch.service.
active_by_executor` already uses as "the agent" for concurrency accounting,
so this reuses it rather than inventing a second identity for the same
concept.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from command_center.runtime import db as runtime_db
from command_center.runtime.completion import SUCCESS_STATE

_LOG = logging.getLogger(__name__)

# provider_id used when a run row somehow has none (schema default is
# 'claude_code', but historical/imported rows are not guaranteed to).
_UNKNOWN_AGENT = "unknown"


@dataclass(frozen=True)
class AgentCostReport:
    """One agent's cost-per-meaningful-task figure.

    `cost_per_meaningful_task_usd` is `None` — never a fabricated `0.0` or an
    infinity — when `meaningful_task_count` is 0: an agent that has not yet
    finished a single meaningful task has no rate to report, and dividing by
    zero to invent one would silently misreport an idle or brand-new agent as
    "free"."""

    agent_id: str
    total_cost_usd: float
    run_count: int
    meaningful_task_count: int

    @property
    def cost_per_meaningful_task_usd(self) -> float | None:
        if self.meaningful_task_count <= 0:
            return None
        return self.total_cost_usd / self.meaningful_task_count

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "total_cost_usd": self.total_cost_usd,
            "run_count": self.run_count,
            "meaningful_task_count": self.meaningful_task_count,
            "cost_per_meaningful_task_usd": self.cost_per_meaningful_task_usd,
        }


def _parse_total_cost_usd(payload) -> float | None:
    """Extract `total_cost_usd` from one `run_event.payload_json`, tolerating
    a `jsonb`-decoded `dict` (the PostgreSQL mirror) alongside JSON text —
    mirrors `task_pipeline.daily_spend_usd`'s tolerance exactly, including
    logging (rather than silently dropping) a genuinely malformed text
    payload."""
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except ValueError:
            _LOG.warning(
                "cost_per_meaningful_task: skipping run_event with unparseable "
                "payload_json: %r",
                payload[:200] if isinstance(payload, str) else payload,
            )
            return None
    if not isinstance(payload, dict):
        _LOG.warning(
            "cost_per_meaningful_task: skipping run_event whose payload is a "
            "%s, not an object",
            type(payload).__name__,
        )
        return None
    cost = payload.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        return None
    return float(cost)


def _agent_of(provider_id: str | None) -> str:
    return provider_id or _UNKNOWN_AGENT


def _run_count_by_agent(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with runtime_db.connect(db_path) as conn:
        rows = conn.execute("SELECT provider_id FROM run").fetchall()
    for row in rows:
        agent_id = _agent_of(row["provider_id"])
        counts[agent_id] = counts.get(agent_id, 0) + 1
    return counts


def _cost_by_agent(db_path: Path) -> dict[str, float]:
    totals: dict[str, float] = {}
    with runtime_db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT run.provider_id AS provider_id, run_event.payload_json AS payload
              FROM run_event
              JOIN run ON run.id = run_event.run_id
             WHERE CAST(run_event.payload_json AS TEXT) LIKE '%total_cost_usd%'
            """
        ).fetchall()
    for row in rows:
        cost = _parse_total_cost_usd(row["payload"])
        if cost is None:
            continue
        agent_id = _agent_of(row["provider_id"])
        totals[agent_id] = totals.get(agent_id, 0.0) + cost
    return totals


def _meaningful_task_count_by_agent(db_path: Path) -> dict[str, int]:
    """Distinct completed tasks per agent. Grouped by `task_id` (not
    `run_id`/completion row) so a task that was reworked and completed more
    than once — should the state machine ever allow re-entry into
    `COMPLETED` for the same task — is still counted once as one meaningful
    task per agent that touched it, matching what "a task got done" means to
    an operator reading this report."""
    counted: dict[str, set[str]] = {}
    with runtime_db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT run.provider_id AS provider_id, completion.task_id AS task_id
              FROM completion
              JOIN run ON run.id = completion.run_id
             WHERE completion.completion_state = ?
            """,
            (SUCCESS_STATE,),
        ).fetchall()
    for row in rows:
        agent_id = _agent_of(row["provider_id"])
        counted.setdefault(agent_id, set()).add(row["task_id"])
    return {agent_id: len(task_ids) for agent_id, task_ids in counted.items()}


def cost_per_meaningful_task(db_path: Path) -> list[AgentCostReport]:
    """One `AgentCostReport` per agent (`run.provider_id`) that has ever
    appeared in this database — runs with $0 reported cost and agents with
    zero meaningful tasks are still listed (with `cost_per_meaningful_task_usd`
    `None` for the latter), so a silent agent cannot simply be absent from the
    report. Sorted by `agent_id` for a stable, diffable report."""
    cost_by_agent = _cost_by_agent(db_path)
    run_count_by_agent = _run_count_by_agent(db_path)
    meaningful_by_agent = _meaningful_task_count_by_agent(db_path)
    agent_ids = set(cost_by_agent) | set(run_count_by_agent) | set(meaningful_by_agent)
    return [
        AgentCostReport(
            agent_id=agent_id,
            total_cost_usd=cost_by_agent.get(agent_id, 0.0),
            run_count=run_count_by_agent.get(agent_id, 0),
            meaningful_task_count=meaningful_by_agent.get(agent_id, 0),
        )
        for agent_id in sorted(agent_ids)
    ]


def render_markdown(reports: list[AgentCostReport]) -> str:
    """Render the per-agent table an operator reads directly."""
    lines = [
        "# Cost per meaningful task, by agent",
        "",
        "| Agent | Total cost (USD) | Runs | Meaningful tasks | Cost / meaningful task (USD) |",
        "|---|---:|---:|---:|---:|",
    ]
    if not reports:
        lines.append("| _no runs recorded_ | — | — | — | — |")
    for report in reports:
        rate = report.cost_per_meaningful_task_usd
        rate_str = f"{rate:.4f}" if rate is not None else "— (no meaningful tasks yet)"
        lines.append(
            f"| {report.agent_id} | {report.total_cost_usd:.4f} | {report.run_count} | "
            f"{report.meaningful_task_count} | {rate_str} |"
        )
    return "\n".join(lines) + "\n"
