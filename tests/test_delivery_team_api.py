from contextlib import contextmanager, nullcontext

import pytest
from fastapi.testclient import TestClient

from command_center.api import delivery_team_routes as team
from command_center.webapi.owner_app import create_app


class Cursor:
    def __init__(self, ready=True):
        self.ready = ready
        self.sql = ''
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=()):
        self.sql = sql
        self.queries.append((sql, params))

    def fetchone(self):
        if 'to_regclass' in self.sql:
            return ('delivery_team_stage', 'f', 'f', 'f') if self.ready else (None, None, None, None)
        return (42, 1, 2, 7)

    def fetchall(self):
        return [('task-1', 'password=private', 'AICC', 4, 'DONE', False,
                 [{'stage': 'architect', 'status': 'blocked', 'reason': 'Bearer private',
                   'current': True, 'updated_at': '2026-11-04T00:00:00Z'},
                  {'stage': 'qa_plan', 'status': 'passed', 'reason': None,
                   'current': False, 'updated_at': '2026-11-04T00:00:00Z'},
                  {'stage': 'security', 'status': 'pending', 'reason': None, 'waived': True,
                   'current': True, 'updated_at': '2026-11-04T00:00:00Z'}],
                 [{'task_id': 'VOYN-FIX-1', 'owner': 'developer', 'action': 'token=private',
                   'due_at': '2026-11-01T00:00:00+00:00', 'policy': True, 'closed': False},
                  {'task_id': 'VOYN-FIX-2', 'owner': 'devops', 'action': 'Fix deploy check',
                   'due_at': '2026-11-01T00:00:00+00:00', 'policy': False, 'closed': True},
                  {'task_id': 'VOYN-FIX-3', 'owner': 'qa', 'action': 'Add check',
                   'due_at': None, 'policy': False, 'closed': False}])]


def connection(cursor):
    class Conn:
        def transaction(self):
            return nullcontext()

        def cursor(self):
            return cursor
    return nullcontext(Conn())


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / 'index.html').write_text('app')
    monkeypatch.setenv('AICC_WEB_DIST', str(tmp_path))
    monkeypatch.setenv('AICC_MASTER_BACKLOG', str(tmp_path / 'missing.md'))
    monkeypatch.setenv('AICC_OWNER_UPSTREAM_KEY', 'k' * 40)
    monkeypatch.delenv('AICC_OWNER_COMMAND_KEY', raising=False)
    monkeypatch.delenv('AICC_PG_HOST', raising=False)
    monkeypatch.setenv('AICC_DELIVERY_TEAM_ENABLED', '1')
    monkeypatch.delenv('AICC_DELIVERY_TEAM_STOP', raising=False)
    return TestClient(create_app())


HEADERS = {'Authorization': 'Bearer ' + 'k' * 40}


def test_auth_precedes_team_database_access(client, monkeypatch):
    def forbidden():
        raise AssertionError('unauthenticated request reached database')
    monkeypatch.setattr(team.pool, 'connection', forbidden)
    assert client.get('/api/team').status_code == 401
    assert client.get('/api/team', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.post('/api/team', headers=HEADERS).status_code == 405


def test_canonical_page_is_bounded_redacted_and_not_fake_acceptance(client, monkeypatch):
    cursor = Cursor()
    monkeypatch.setattr(team.pool, 'connection', lambda: connection(cursor))
    response = client.get('/api/team?limit=10&offset=20', headers=HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data['source'] == 'canonical_postgresql'
    assert data['total'] == 42 and len(data['tasks']) == 1
    assert cursor.queries[-1][1] == (10, 20)
    assert 's.revision=b.revision' in cursor.queries[-1][0]
    assert data['counts']['merged'] is None
    assert data['tasks'][0]['backlog_status'] == 'DONE'
    assert data['tasks'][0]['production_accepted'] is False
    assert data['tasks'][0]['title'] == '[REDACTED]'
    assert data['tasks'][0]['assignments'][0]['blocker'] == '[REDACTED]'
    assert data['tasks'][0]['assignments'][0]['due_at'] is None
    assert data['tasks'][0]['assignments'][1]['status'] == 'stale'
    assert 'private' not in response.text
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('path', ['/api/team?limit=0', '/api/team?limit=101', '/api/team?offset=-1'])
def test_page_bounds(client, path):
    assert client.get(path, headers=HEADERS).status_code == 422


def test_absent_ledger_and_disabled_flags_are_explicit(client, monkeypatch):
    monkeypatch.setenv('AICC_DELIVERY_TEAM_ENABLED', '0')
    monkeypatch.setenv('AICC_DELIVERY_TEAM_STOP', '1')
    monkeypatch.setattr(team.pool, 'connection', lambda: connection(Cursor(ready=False)))
    data = client.get('/api/team', headers=HEADERS).json()
    assert data['available'] is False
    assert data['enabled'] is False and data['stopped'] is True
    assert data['counts'] is None and data['total'] is None
    assert data['reason'] == 'ledger_unavailable'


def test_absent_pool_and_database_failure_do_not_leak_details(client, monkeypatch):
    @contextmanager
    def no_pool():
        raise team.pool.PoolNotOpenError('private connection details')
        yield
    monkeypatch.setattr(team.pool, 'connection', no_pool)
    response = client.get('/api/team', headers=HEADERS)
    assert response.json()['reason'] == 'database_unavailable'
    assert response.json()['counts'] is None

    @contextmanager
    def failed():
        raise RuntimeError('password=private')
        yield
    monkeypatch.setattr(team.pool, 'connection', failed)
    response = client.get('/api/team', headers=HEADERS)
    assert response.status_code == 503
    assert 'private' not in response.text


def test_waived_roles_and_corrective_due_dates_are_explicit(client, monkeypatch):
    monkeypatch.setattr(team.pool, 'connection', lambda: connection(Cursor()))
    task = client.get('/api/team', headers=HEADERS).json()['tasks'][0]
    assert task['assignments'][2]['status'] == 'waived'
    first, closed, unknown = task['corrections']
    assert (first['assigned_by'], first['overdue'], first['action']) == ('cto_policy', True, '[REDACTED]')
    assert (closed['assigned_by'], closed['overdue'], closed['effectiveness_checked']) == ('operator', False, True)
    assert unknown['due_at'] is None and unknown['overdue'] is False
