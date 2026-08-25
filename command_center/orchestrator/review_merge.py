"""Server-side review and merge — the loop closes without a human (BO-S3b 2/3, 3/3).

Part 1 (publish.py) turns a finished run into a PR and ingest records the
pr/sha evidence, moving the task to READY_TO_REVIEW. This module is the rest:

- ``review_once``: for each READY_TO_REVIEW task carrying pr evidence and no
  verdict yet, enqueue one adversarial review run (read-only profile) whose
  prompt names the PR. The verdict lands in the work result like any outcome;
  the acceptance marker itself is published by the control-plane script
  (voyn-acceptance app), invoked here by path so the app key never enters
  this process.
- ``merge_once``: for each PR that carries an ACCEPT marker AND whose required
  checks are green, ``gh pr merge`` it and move the task READY_TO_REVIEW→DONE
  with the merged sha as evidence (via the existing backlog_transition gate).

Independence of the verdict is not this module's own judgement. Merging used to
be a self-check — any review body anywhere containing the marker text let the
merger proceed, including a marker the merging account had published itself —
so the loop could close on evidence it produced. The decision now goes through
the same gateway the CI acceptance gate runs
(:mod:`scripts.assert_independent_acceptance`), which reads the marker only as a
review's opening line, only from a submitted, undismissed review, only for the
exact head being merged, and only from a login that is neither the pull
request's author nor the authenticated identity about to merge it. One
implementation, so a verdict that would fail the branch gate cannot pass the
merge loop.

Both are refusal-as-data, driven by oneshot timers, and idempotent: a task
already reviewed is skipped, an already-merged PR closes the task once.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from scripts.assert_independent_acceptance import AcceptanceError, evaluate

__all__ = ["LoopReport", "ReviewConfig", "merge_once", "review_once"]


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
    merged: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _gh(argv: list[str], repo_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *argv], cwd=repo_path, capture_output=True, text=True,
        check=False, timeout=120,
    )


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

# The one check this loop may disregard: `acceptance-gate.yml` is red for the
# whole window between opening the pull request and the verdict landing, and it
# re-runs only on a review event, so a merge tick that waited for it could wait
# on a run that has already been superseded. The loop re-derives that gate's
# conclusion below, from the same reviews and the same gateway code, under a
# stricter rule (it also excludes the merger), so skipping the check is not
# skipping the judgement. Matched on the gate's own check name rather than on
# the substring "acceptance", which would also have silenced a red test job
# named for what it tests.
_ACCEPTANCE_CHECK = "Acceptance gate"

_GREEN = (None, "SUCCESS", "NEUTRAL", "SKIPPED")


def _pr_is_mergeable(repo_path: str, pr_url: str) -> tuple[bool, str]:
    """A PR is ready iff checks pass and an independent reviewer accepts it.

    The authenticated GitHub identity is the prospective merger. The shared
    acceptance gateway validates review state, marker position, exact head, and
    independence from both that identity and the pull-request author — this
    function decides nothing about the verdict itself, so the merge loop and the
    branch gate cannot drift apart.

    A merger whose own identity cannot be read is a refusal, not a pass:
    independence from an unknown account is unprovable.
    """
    view = _gh(
        ["pr", "view", pr_url, "--json",
         "author,reviews,statusCheckRollup,mergeStateStatus,state,headRefOid"],
        repo_path,
    )
    if view.returncode != 0:
        return False, f"gh_view_failed: {view.stderr.strip()[:100]}"
    data = json.loads(view.stdout or "{}")
    if data.get("state") != "OPEN":
        return False, f"pr_{str(data.get('state')).lower()}"
    head = data.get("headRefOid", "")
    merger = _gh(["api", "user", "--jq", ".login"], repo_path)
    if merger.returncode != 0 or not merger.stdout.strip():
        return False, "merger_identity_unavailable"
    author = data.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    try:
        evaluate(data.get("reviews"), head, author_login, merger.stdout.strip())
    except AcceptanceError as error:
        return False, f"acceptance_refused: {error}"
    rollup = data.get("statusCheckRollup") or []
    bad = [
        c.get("name", "?") for c in rollup
        if c.get("conclusion") not in _GREEN
        and not str(c.get("name", "")).startswith(_ACCEPTANCE_CHECK)
    ]
    if bad:
        return False, f"checks_not_green: {bad[:3]}"
    return True, head


def merge_once(factory: Any, repo_path: str, cfg: ReviewConfig | None = None) -> LoopReport:
    """Merge every READY_TO_REVIEW task whose PR carries an ACCEPT marker and
    green checks, then close it DONE with the merged sha as evidence."""
    cfg = cfg or ReviewConfig()
    report = LoopReport()
    tasks = _rows(
        factory,
        "SELECT t.task_id, e.value FROM backlog_task t "
        "JOIN backlog_evidence e ON e.task_id = t.task_id AND e.kind = 'pr' "
        "WHERE t.status = 'READY_TO_REVIEW' ORDER BY t.updated_at LIMIT %s",
        (cfg.max_per_tick,),
    )
    for task_id, pr_url in tasks:
        ready, detail = _pr_is_mergeable(repo_path, pr_url)
        if not ready:
            report.skipped.append((task_id, detail))
            continue
        merged = _gh(["pr", "merge", pr_url, "--squash"], repo_path)
        if merged.returncode != 0:
            report.skipped.append((task_id, f"merge_failed: {merged.stderr.strip()[:100]}"))
            continue
        head = detail  # _pr_is_mergeable returned the head sha
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
