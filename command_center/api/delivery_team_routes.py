"""Read model for the authenticated owner surface; no public API router.

Only canonical PostgreSQL rows leave this boundary, with an explicit allowlist.
Role routing is not a completed review, and a recorded SHA is not a merge receipt.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import HTTPException

from command_center.db import pool
from native_gateway.redaction import sanitize_value

STAGE_ROLES = {
    'ba': 'Бизнес-аналитик', 'business_technologist': 'Технолог бизнеса',
    'architect': 'Главный архитектор', 'qa_plan': 'QA: план проверок',
    'security': 'Безопасность', 'ux': 'UX/UI', 'cto': 'CTO',
    'independent_reviewer': 'Независимый ревьюер', 'devops': 'DevOps / SRE',
    'production_qa': 'QA: production', 'auditor': 'Аудитор процесса',
    'cto_rca': 'CTO: устранение причин',
}


def _flags() -> tuple[bool, bool]:
    return (os.environ.get('AICC_DELIVERY_TEAM_ENABLED', '') == '1',
            os.environ.get('AICC_DELIVERY_TEAM_STOP', '') == '1')


def read_team(*, limit: int = 30, offset: int = 0) -> dict:
    """Read a bounded task page and whole-ledger counts in one DB snapshot."""
    enabled, stopped = _flags()
    base = {'source': 'canonical_postgresql', 'read_only': True,
            'enabled': enabled, 'stopped': stopped,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'limit': limit, 'offset': offset,
            'roles': [{'stage': stage, 'name': name} for stage, name in STAGE_ROLES.items()]}
    try:
        with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SELECT to_regclass('delivery_team_stage'), "
                        "to_regprocedure('delivery_team_accepted(text,bigint)'), "
                        "to_regprocedure('delivery_team_stage_current(text,bigint,text)'), "
                        "to_regprocedure('delivery_team_stage_waived(text,bigint,text)')")
            ready = cur.fetchone()
            if not ready or not all(ready):
                return {**base, 'available': False, 'reason': 'ledger_unavailable',
                        'total': None, 'counts': None, 'tasks': []}
            cur.execute("""
                WITH current_tasks AS (
                    SELECT b.task_id, b.revision FROM backlog_task b
                    WHERE EXISTS (SELECT 1 FROM delivery_team_stage s
                                  WHERE s.task_id=b.task_id AND s.revision=b.revision)
                )
                SELECT count(*), count(*) FILTER (WHERE delivery_team_accepted(t.task_id,t.revision)),
                    count(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM delivery_team_stage s WHERE s.task_id=t.task_id
                        AND s.revision=t.revision AND s.status='blocked'
                        AND delivery_team_stage_current(s.task_id,s.revision,s.stage))),
                    count(*) FILTER (WHERE EXISTS (
                        SELECT 1 FROM backlog_evidence e WHERE e.task_id=t.task_id AND e.kind='sha'))
                FROM current_tasks t
            """)
            total, accepted, blocked, sha_recorded = cur.fetchone()
            cur.execute("""
                SELECT b.task_id,b.title,b.repo,b.revision,b.status,
                    delivery_team_accepted(b.task_id,b.revision),
                    (SELECT jsonb_agg(jsonb_build_object(
                        'stage',s.stage,'status',s.status,'reason',s.reason,
                        'current',delivery_team_stage_current(s.task_id,s.revision,s.stage),
                        'waived',delivery_team_stage_waived(s.task_id,s.revision,s.stage),
                        'updated_at',s.updated_at) ORDER BY s.ordinal)
                     FROM delivery_team_stage s WHERE s.task_id=b.task_id AND s.revision=b.revision),
                    (SELECT jsonb_agg(jsonb_build_object(
                        'task_id',a.task_id,'owner',a.assignment->>'owner','action',a.assignment->>'action',
                        'due_at',a.due_at,'policy',a.recorded_by LIKE 'policy:%%',
                        'closed',EXISTS (SELECT 1 FROM delivery_team_corrective_check c
                            WHERE c.parent_task_id=a.parent_task_id AND c.incident_key=a.incident_key
                            AND c.task_id=a.task_id)) ORDER BY a.due_at NULLS LAST,a.task_id)
                     FROM delivery_team_corrective_assignment a WHERE a.parent_task_id=b.task_id)
                FROM backlog_task b WHERE EXISTS (
                    SELECT 1 FROM delivery_team_stage s WHERE s.task_id=b.task_id AND s.revision=b.revision)
                ORDER BY b.task_id LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()
    except pool.PoolNotOpenError:
        return {**base, 'available': False, 'reason': 'database_unavailable',
                'total': None, 'counts': None, 'tasks': []}
    except Exception:
        # Do not expose SQL, host details, credentials or driver traces.
        raise HTTPException(503, 'Данные команды временно недоступны') from None
    tasks = []
    now = datetime.now(timezone.utc)
    for task_id, title, repo, revision, backlog_status, is_accepted, stages, corrective in rows:
        assignments = []
        for stage in stages or []:
            state = stage['status'] if stage['current'] else 'stale'
            if state == 'pending' and stage.get('waived'):
                state = 'waived'
            assignments.append({
                'stage': stage['stage'], 'role': STAGE_ROLES.get(stage['stage'], 'Неизвестная роль'),
                'status': state, 'blocker': sanitize_value(str(stage['reason'] or '')) if state == 'blocked' else None,
                'next_action': {'pending': 'Ожидает назначения', 'queued': 'Ожидает результата исполнителя',
                                'blocked': 'Разобрать блокер и назначить исправление', 'passed': 'Результат этапа записан',
                                'stale': 'Повторить оценку изменившихся входных данных',
                                'waived': 'Роль признана неприменимой архитектором; проверка не выполнялась'}.get(state, 'Уточнить состояние'),
                'due_at': None, 'updated_at': stage['updated_at'],
            })
        corrections = []
        for item in corrective or []:
            due = item.get('due_at')
            overdue = bool(due and not item['closed'] and datetime.fromisoformat(due) < now)
            corrections.append({
                'task_id': sanitize_value(item['task_id']), 'owner': sanitize_value(item['owner'] or ''),
                'action': sanitize_value(item['action'] or ''), 'due_at': due,
                'assigned_by': 'cto_policy' if item['policy'] else 'operator',
                # A recorded effectiveness check, not proof that CTO-RCA closed.
                'effectiveness_checked': bool(item['closed']), 'overdue': overdue,
            })
        tasks.append({'id': sanitize_value(task_id), 'title': sanitize_value(title or task_id),
                      'project': sanitize_value(repo or 'Не указан'), 'revision': revision,
                      'backlog_status': backlog_status, 'merged': None,
                      'production_accepted': bool(is_accepted), 'assignments': assignments,
                      'corrections': corrections})
    return {**base, 'available': True, 'reason': None if enabled else 'not_enabled',
            'total': total, 'counts': {'production_accepted': accepted, 'blocked': blocked,
                                     'sha_recorded': sha_recorded, 'merged': None}, 'tasks': tasks}
