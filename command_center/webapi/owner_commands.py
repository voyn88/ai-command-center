"""Single-owner web commands, admitted by the authenticated edge.

Separate, revocable command credential; explicit owner grants and same-origin
requests. Effects and idempotency receipts commit in one PostgreSQL transaction.
No shell commands, direct queue writes or client-supplied agent prompts/routes.
The platform-token API and native gateway retain their own authentication.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from command_center.db import pool
from command_center.orchestrator.planner import PlanLimits, plan_once
from native_gateway.redaction import sanitize_value

POLICY = 'owner-web-v1'
SOURCE = 'owner-command:v1'


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str = Field(min_length=1, max_length=300)
    expected_revision: int = Field(ge=0)
    idempotency_key: UUID
    device_id: UUID
    action: Literal['launch', 'decision']
    decision: Literal['resume', 'reject', 'refine'] | None = None
    note: str = Field(default='', max_length=4000)


def grants(owner: str) -> frozenset[str]:
    try:
        data = json.loads(Path(os.environ['AICC_OWNER_GRANTS_FILE']).read_text())
        scopes = data.get(owner, [])
        if not isinstance(scopes, list) or any(x not in {'launch', 'decision', 'read'} for x in scopes):
            raise ValueError('invalid scopes')
        return frozenset(scopes)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'Права доступа временно недоступны') from None


def _row(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn, owner: str, command: Command, allowed: frozenset[str], wip: int) -> dict:
    payload = command.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    key = str(command.idempotency_key)
    # Lock is transaction-scoped; concurrent retries cannot both pass the lookup.
    _row(conn, 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('owner:' + owner + ':' + key,))
    previous = _row(conn, "SELECT detail FROM backlog_event WHERE event='provenance' AND reason=%s "
                    "AND detail->>'owner'=%s AND detail->>'key'=%s ORDER BY event_id LIMIT 1", (SOURCE, owner, key))
    if previous:
        if previous[0]['digest'] != digest:
            return {'ok': False, 'reason': 'idempotency_conflict'}
        return previous[0]['result']

    # Lock the same planner lease row before the task (the scheduler's lock
    # order), retaining it until commit. The planner id remains canonical so
    # existing terminal-result reconciliation can release repository leases.
    acquired = False
    tick_owner = 'owner-web:' + key
    if command.action == 'launch' and 'launch' in allowed:
        acquired = bool(_row(conn, "SELECT * FROM backlog_lease_acquire('planner:global', %s, 300)", (tick_owner,))[0])
    task = _row(conn, 'SELECT * FROM backlog_owner_lock(%s)', (command.task_id,))
    if task is None:
        if acquired:
            _row(conn, "SELECT * FROM backlog_lease_release('planner:global', %s)", (tick_owner,))
        return {'ok': False, 'reason': 'unknown_task'}
    if command.action not in allowed:
        result = {'ok': False, 'reason': 'forbidden'}
    elif task[0] != command.expected_revision:
        result = {'ok': False, 'reason': 'revision_conflict'}
    elif command.action == 'launch' and not acquired:
        result = {'ok': False, 'reason': 'planner_busy'}
    elif command.action == 'decision':
        ok, reason, revision = _row(conn, 'SELECT * FROM backlog_owner_decision(%s,%s,%s,%s)',
            (command.task_id, command.expected_revision, command.decision, command.note))
        result = {'ok': bool(ok), 'reason': reason, 'revision': revision}
    elif task[1] != 'OPEN' or task[2] != 'task':
        result = {'ok': False, 'reason': 'not_eligible'}
    else:
        report = plan_once(lambda: nullcontext(conn), PlanLimits(
            only_task_id=command.task_id, tick_owner=tick_owner, max_dispatches_per_tick=1,
            max_resumes_per_tick=0, wip_limit=wip,
        ))
        # plan_once released the logical lease. PostgreSQL keeps its row lock
        # until this outer transaction (including the receipt) commits.
        acquired = False
        if report.dispatched:
            result = {'ok': True, 'reason': 'queued', 'work_item_id': report.dispatched[0][1]}
        else:
            reasons = report.refused + report.skipped_by_wave_gate + report.undispatchable
            reason = next((r for task_id, r in reasons if task_id == command.task_id),
                          'planner_busy' if report.planner_busy else 'review_backlog_full' if report.fenced else 'not_eligible')
            result = {'ok': False, 'reason': reason}
    if acquired:
        _row(conn, "SELECT * FROM backlog_lease_release('planner:global', %s)", (tick_owner,))
    receipt = {'owner': owner, 'device': str(command.device_id), 'key': key,
               'digest': digest, 'policy': POLICY, 'action': command.action,
               'request': payload, 'result': result}
    recorded = _row(conn, 'SELECT backlog_record_provenance(%s,%s,%s::jsonb)',
                    (command.task_id, SOURCE, json.dumps(receipt)))
    if not recorded or not recorded[0]:
        raise RuntimeError('Command receipt not persisted')
    return result


def create_router() -> APIRouter:
    router = APIRouter(prefix='/api/owner')

    def owner(request: Request) -> str:
        identity = request.headers.get('x-aicc-owner', '')
        if not identity or identity != os.environ.get('AICC_OWNER_ID') or 'read' not in grants(identity):
            raise HTTPException(403, 'Нет доступа к действиям')
        return identity

    @router.get('/task')
    def task(request: Request, task_id: str):
        identity = owner(request)
        with pool.connection() as conn:
            row = _row(conn, 'SELECT revision,status,kind,body FROM backlog_task WHERE task_id=%s', (task_id,))
        if not row:
            raise HTTPException(404, 'Задача не найдена')
        allowed = grants(identity)
        return {'revision': row[0], 'status': row[1], 'body': sanitize_value(row[3] or ''),
                'can_launch': row[1] == 'OPEN' and row[2] == 'task' and 'launch' in allowed,
                'can_decide': row[1] in {'DEFER_TO_USER', 'NEEDS_REFINEMENT', 'UNTRIAGED'} and row[2] == 'task' and 'decision' in allowed}

    @router.post('/commands')
    def command(request: Request, body: Command):
        identity = owner(request)
        origins = os.environ.get('AICC_OWNER_ORIGINS', '').split(',')
        if request.headers.get('origin') not in origins or request.headers.get('x-aicc-request') != 'owner-command-v1':
            raise HTTPException(403, 'Недопустимый источник запроса')
        allowed = grants(identity)
        wip = int(os.environ.get('AICC_OWNER_WIP_LIMIT', '2'))
        if wip < 1 or wip > 4:
            raise HTTPException(503, 'Лимит запусков не настроен')
        with pool.connection() as conn, conn.transaction():
            result = execute(conn, identity, body, allowed, wip)
        # Return refusals as data after the audit transaction commits.
        return result

    return router
