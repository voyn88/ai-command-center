"""Server-side review and merge — the loop closes without a human (BO-S3b 2/3, 3/3).

Part 1 (publish.py) turns a finished run into a PR and ingest records the
pr/sha evidence, moving the task to READY_TO_REVIEW. This module is the rest:

- ``review_once``: for each READY_TO_REVIEW task carrying pr evidence and no
  verdict yet, enqueue one adversarial review run (read-only profile) whose
  prompt names the PR. The verdict lands in the work result like any outcome;
  the acceptance marker itself is published by the control-plane script
  (voyn-acceptance app), invoked here by path so the app key never enters
  this process.
- ``merge_once``: for each READY_TO_REVIEW task with pr evidence, hand the PR
  to ``merge_gateway.merge_pr`` (VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY) and,
  on success, move the task READY_TO_REVIEW→DONE with the merged sha as
  evidence (via the existing backlog_transition gate).

  This module used to decide mergeability itself and call ``gh pr merge``
  directly, through the same ambient ``gh`` credential ``publish.py`` uses to
  push branches and open pull requests — one credential, three roles (open,
  accept, merge) that are supposed to be independent. It no longer holds any
  merge credential at all: every mergeability check (PR open, exact head sha,
  independent verdict, required checks, no active reject) and the merge call
  itself now live in ``merge_gateway``, which reads its own credential from a
  distinct environment variable this process never sets or reads. A worker or
  planner process — this one included — cannot merge a pull request no matter
  what its application logic decides, because it holds no credential capable
  of it; see ``tests/architecture/test_merge_gateway_boundary.py``.

Both are refusal-as-data, driven by oneshot timers, and idempotent: a task
already reviewed is skipped, an already-merged PR closes the task once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from command_center.orchestrator import merge_gateway

__all__ = ["LoopReport", "ReviewConfig", "merge_once", "review_once"]


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    reviewer: str = "server-reviewer"
    marker_tool: str = ""  # path to the acceptance-marker publisher; "" = skip
    queue: str = "execution"
    review_timeout: int = 900
    max_per_tick: int = 8
    gateway_token_env: str = merge_gateway.GATEWAY_TOKEN_ENV
    gateway_policy_version: str = merge_gateway.POLICY_VERSION


@dataclass
class LoopReport:
    reviewed: list[tuple[str, str]] = field(default_factory=list)
    merged: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _rows(factory: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


# -- Part 2: review -----------------------------------------------------------

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


# -- Part 3: merge ------------------------------------------------------------


def merge_once(factory: Any, repo_path: str, cfg: ReviewConfig | None = None) -> LoopReport:
    """Hand every READY_TO_REVIEW task's PR to the merge gateway; on success,
    close it DONE with the merged sha as evidence.

    Every mergeability judgement — PR open, exact head sha, independent
    verdict, required checks, no active reject, policy version — is made
    inside ``merge_gateway.merge_pr``, under its own credential. This
    function makes none of those judgements itself and holds no merge
    credential; it is a queue-to-gateway adapter, not a second decision
    point."""
    cfg = cfg or ReviewConfig()
    report = LoopReport()
    tasks = _rows(
        factory,
        "SELECT t.task_id, e.value FROM backlog_task t "
        "JOIN backlog_evidence e ON e.task_id = t.task_id AND e.kind = 'pr' "
        "WHERE t.status = 'READY_TO_REVIEW' ORDER BY t.updated_at LIMIT %s",
        (cfg.max_per_tick,),
    )
    gateway_cfg = merge_gateway.GatewayConfig(
        repo_path=repo_path,
        token_env=cfg.gateway_token_env,
        policy_version=cfg.gateway_policy_version,
    )
    for task_id, pr_url in tasks:
        result = merge_gateway.merge_pr(pr_url, gateway_cfg)
        if not result.ok:
            report.skipped.append((task_id, result.reason))
            continue
        head = result.head_sha
        # Evidence and the DONE transition are one act: the sha row and the
        # status move commit together or not at all (an explicit transaction,
        # since the app factory is autocommit). backlog_transition's third
        # argument is the optimistic-lock revision (bigint), read here (a plain SELECT — the app role writes only through
        # functions, so no row lock is taken; the optimistic revision below is
        # the concurrency guard); the
        # actor is session_user inside the function, not an argument.
        with factory() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT revision FROM backlog_task WHERE task_id = %s",
                        (task_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        conn.rollback()
                        report.skipped.append((task_id, "task_vanished"))
                        continue
                    revision = row[0]
                    cur.execute(
                        "SELECT backlog_record_evidence(%s, 'sha', %s)", (task_id, head)
                    )
                    cur.execute(
                        "SELECT ok, reason FROM backlog_transition(%s, 'DONE', %s)",
                        (task_id, revision),
                    )
                    ok, reason = cur.fetchone()
                if ok:
                    conn.commit()
                    report.merged.append((task_id, head))
                else:
                    conn.rollback()
                    report.skipped.append((task_id, f"transition:{reason}"))
            finally:
                conn.autocommit = True
    return report
