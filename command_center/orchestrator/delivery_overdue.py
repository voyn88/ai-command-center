"""Overdue corrective-action escalation for the native delivery team.

Escalation is data, exactly once per assignment. SQL lists the overdue
assignments that still lack a recorded effectiveness check, and the claim
function re-derives that condition itself before writing the append-only
ledger row. In the same transaction an OPEN canonical backlog task addressed
to the CTO is filed through the ordinary ``upsert_task`` protocol, the inbox
the tick-stall watchdog already uses. Nothing here reassigns, extends a due
date, retries, closes or dispatches work.
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

_BODY = (
    "Delivery-team overdue corrective action, escalated to the CTO.\n\n"
    "- parent task: {parent}\n"
    "- incident: {incident}\n"
    "- corrective task: {task}\n"
    "- owner: {owner}\n"
    "- action: {action}\n"
    "- due: {due:%Y-%m-%d %H:%M:%S%z}\n\n"
    "No independently recorded effectiveness check exists for this assignment "
    "after its due date. Decide the next bounded action: unblock the owner, "
    "reassign through operator confirmation, or record why the correction is "
    "no longer applicable. Do not mark the correction effective without that "
    "check; this escalation does not change the assignment or its due date."
)


@dataclass
class OverdueReport:
    escalated: list = field(default_factory=list)
    refused: list = field(default_factory=list)


def escalation_task_id(parent: str, incident: str, task: str) -> str:
    """Deterministic, so a crashed run's retry files the same task."""
    digest = hashlib.sha256("\0".join((parent, incident, task)).encode()).hexdigest()[:16]
    return f"{task}-OVERDUE-{digest}"


def escalate_overdue(connection_factory: Any, *, limit: int = 20) -> OverdueReport:
    """Escalate each overdue unproven assignment once; call under the planner lease."""
    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    report = OverdueReport()
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM delivery_team_overdue(%s)", (limit,))
        overdue = cur.fetchall()
    for parent, incident, task, owner, action, due, priority, wave, repo in overdue:
        new_id = escalation_task_id(parent, incident, task)
        try:
            with connection_factory() as conn, conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT delivery_team_claim_overdue(%s,%s,%s,%s)", (parent, incident, task, new_id))
                    row = cur.fetchone()
                if not row or row[0] is not True:
                    raise _Refused(task, "claim_refused")
                ok, reason, _changed = BacklogStore(lambda: nullcontext(conn)).upsert_task(ParsedTask(
                    task_id=new_id, wave=wave, priority=priority, status="OPEN", kind="task",
                    title=f"Overdue corrective action {task} for {parent}",
                    body=_BODY.format(parent=parent, incident=incident, task=task, owner=owner,
                                      action=action, due=due),
                    repo=repo, line_no=0,
                ))
                if not ok:
                    # Roll back the ledger claim too: an escalation without its
                    # task would silently suppress every later attempt.
                    raise _Refused(task, reason)
        except _Refused as refusal:
            report.refused.append((refusal.task, refusal.reason))
            continue
        report.escalated.append((task, new_id))
    return report


class _Refused(RuntimeError):
    def __init__(self, task: str, reason: str):
        self.task, self.reason = task, reason
        super().__init__(f"overdue escalation for {task} refused: {reason}")
