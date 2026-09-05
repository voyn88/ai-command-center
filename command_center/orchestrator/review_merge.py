"""Server-side review — the loop closes without a human (BO-S3b 2/3).

Part 1 (publish.py) turns a finished run into a PR and ingest records the
pr/sha evidence, moving the task to READY_TO_REVIEW. This module is the
next step:

- ``review_once``: for each READY_TO_REVIEW task carrying pr evidence and no
  verdict yet, enqueue one adversarial review run (read-only profile) whose
  prompt names the PR. The verdict lands in the work result like any outcome;
  the acceptance marker itself is published by the control-plane script
  (voyn-acceptance app), invoked here by path so the app key never enters
  this process.

Part 3, merging, is deliberately not here: ``orchestrator.merge_gateway`` is
the only component in this pipeline allowed to spend `gh pr merge`
capability (VOYN-W0-AICC-MERGE-GATEWAY). Before that module existed this file
also read a PR's reviews and merged it in the same process, through the same
ambient `gh` credential this module still uses to enqueue reviews — the same
credential the worker uses to push and open pull requests. A reviewer's
verdict and the credential that acts on it need to be genuinely different
identities, not two steps run one after another by whichever process holds
both; splitting them into separate modules is what makes "review_merge
cannot merge anything" a fact about the code, not a convention.

``review_once`` is refusal-as-data, driven by a oneshot timer, and
idempotent: a task already under review returns the same queued work item,
never a second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["LoopReport", "ReviewConfig", "review_once"]


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    reviewer: str = "server-reviewer"
    marker_tool: str = ""  # path to the acceptance-marker publisher; "" = skip
    queue: str = "execution"
    review_timeout: int = 900
    max_per_tick: int = 8


@dataclass
class LoopReport:
    reviewed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _rows(factory: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


_REVIEW_PROMPT = (
    "Adversarially review pull request {pr} for task {task}. Read the diff. "
    "Hunt for defects that make it wrong, unsafe, or a regression — a control "
    "that reads wider than it acts, a test that passes on broken code. State a "
    "verdict as the last line, exactly: VERDICT: ACCEPT or VERDICT: REJECT, "
    "then HEAD_SHA: <the PR head sha>."
)


def review_once(factory: Any, enqueue: Any, cfg: ReviewConfig | None = None) -> LoopReport:
    """Enqueue a review run for each READY_TO_REVIEW task with a pr and no
    review queued yet. ``enqueue(queue, key, payload)`` is the queue writer
    (control-plane privilege); passing it in keeps this composable and
    testable without a live queue."""
    cfg = cfg or ReviewConfig()
    report = LoopReport()
    # Idempotency is the queue's: enqueue keys on review:<task>, so a task
    # already under review returns the same work item, never a second run.
    tasks = _rows(
        factory,
        "SELECT DISTINCT t.task_id, e.value FROM backlog_task t "
        "JOIN backlog_evidence e ON e.task_id = t.task_id AND e.kind = 'pr' "
        "WHERE t.status = 'READY_TO_REVIEW' "
        "ORDER BY t.task_id LIMIT %s",
        (cfg.max_per_tick,),
    )
    for task_id, pr_url in tasks:
        payload = {
            "kind": "agent_run", "v": 1, "project_id": task_id,
            "repository_path": "", "task_type": "review",
            "prompt": _REVIEW_PROMPT.format(pr=pr_url, task=task_id),
            "timeout_seconds": cfg.review_timeout, "untrusted": False,
        }
        enqueue(cfg.queue, f"review:{task_id}", payload)
        report.reviewed.append((task_id, pr_url))
    return report
