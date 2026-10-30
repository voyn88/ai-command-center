import os
import time

import pytest
from fastapi.testclient import TestClient

from command_center.db.backlog_export import render_projection
from command_center.webapi.owner_app import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    projection = tmp_path / 'backlog.md'
    projection.write_text(render_projection([
        {'task_id': 'A-1', 'title': 'Ship UI', 'status': 'IN_PROGRESS', 'repo': 'ai-command-center'},
        {'task_id': 'B-2', 'title': 'password=private', 'status': 'DEFER_TO_USER', 'repo': '/home/private/repo'},
    ]), encoding='utf-8')
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('AICC web', encoding='utf-8')
    monkeypatch.setenv('AICC_MASTER_BACKLOG', str(projection))
    monkeypatch.setenv('AICC_WEB_DIST', str(dist))
    monkeypatch.setenv('AICC_OWNER_UPSTREAM_KEY', 'k' * 40)
    return TestClient(create_app()), projection


def test_all_paths_require_upstream_auth(setup):
    client, _ = setup
    for path in ['/', '/api/backlog', '/healthz', '/unknown']:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_canonical_rows_and_redaction_without_local_database(setup):
    client, _ = setup
    response = client.get('/api/backlog', headers={'Authorization': 'Bearer ' + 'k' * 40})
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 2
    assert body['counts'] == {'IN_PROGRESS': 1, 'DEFER_TO_USER': 1}
    assert body['tasks'][0]['project'] == 'AICC'
    assert body['tasks'][1]['title'] == '[REDACTED]'
    assert body['tasks'][1]['project'] == 'Не указан'
    assert '/home/' not in response.text
    assert response.headers['cache-control'] == 'no-store'


def test_mutation_is_unavailable(setup):
    client, _ = setup
    for method in ['post', 'put', 'delete', 'patch']:
        response = getattr(client, method)('/api/v1/queue/audit', headers={'Authorization': 'Bearer ' + 'k' * 40})
        assert response.status_code == 405


def test_stale_and_missing_projection_are_explicit(setup):
    client, path = setup
    headers = {'Authorization': 'Bearer ' + 'k' * 40}
    os.utime(path, (time.time() - 600, time.time() - 600))
    assert client.get('/api/backlog', headers=headers).json()['stale']
    path.unlink()
    assert client.get('/api/backlog', headers=headers).status_code == 503


def test_missing_key_prevents_start(monkeypatch):
    monkeypatch.delenv('AICC_OWNER_UPSTREAM_KEY', raising=False)
    with pytest.raises(RuntimeError):
        create_app()
