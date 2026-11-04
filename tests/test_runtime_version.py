from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from command_center.deployment.runtime_version import capture_runtime_version
from command_center.webapi import owner_app


SHA = 'a' * 40
HEADERS = {'Authorization': 'Bearer ' + 'k' * 40}


def release(tmp_path: Path, sha=SHA):
    root = tmp_path.resolve() / 'releases' / sha
    module = root / 'command_center' / 'webapi' / 'owner_app.py'
    module.parent.mkdir(parents=True)
    module.write_text('# deployed module\n')
    (root / '.aicc-release-sha').write_text(sha + '\n')
    return module


def client_for(monkeypatch, tmp_path, module):
    dist = tmp_path / 'dist'
    dist.mkdir(exist_ok=True)
    (dist / 'index.html').write_text('app')
    monkeypatch.setenv('AICC_WEB_DIST', str(dist))
    monkeypatch.setenv('AICC_MASTER_BACKLOG', str(tmp_path / 'missing.md'))
    monkeypatch.setenv('AICC_OWNER_UPSTREAM_KEY', 'k' * 40)
    monkeypatch.delenv('AICC_OWNER_COMMAND_KEY', raising=False)
    monkeypatch.delenv('AICC_PG_HOST', raising=False)
    monkeypatch.setattr(owner_app, '_OWNER_MODULE_FILE', module)
    return TestClient(owner_app.create_app())


def test_release_marker_and_physical_path_agree(tmp_path):
    assert capture_runtime_version(release(tmp_path)).sha == SHA


@pytest.mark.parametrize('marker', ['', 'b' * 40, SHA + '\nextra', SHA + '\n' * 100])
def test_malformed_or_foreign_marker_is_unknown(tmp_path, marker):
    module = release(tmp_path)
    (module.parents[2] / '.aicc-release-sha').write_text(marker)
    assert capture_runtime_version(module).sha is None


def test_missing_or_symlink_marker_is_unknown(tmp_path):
    module = release(tmp_path)
    marker = module.parents[2] / '.aicc-release-sha'
    marker.unlink()
    assert capture_runtime_version(module).sha is None
    other = tmp_path / 'other-marker'
    other.write_text(SHA)
    marker.symlink_to(other)
    assert capture_runtime_version(module).sha is None


def test_directory_name_alone_is_not_provenance(tmp_path):
    module = release(tmp_path, sha='not-a-sha')
    assert capture_runtime_version(module).sha is None


def test_mutable_checkout_and_env_do_not_guess_running_version(tmp_path, monkeypatch):
    module = tmp_path / 'checkout' / 'command_center' / 'webapi' / 'owner_app.py'
    module.parent.mkdir(parents=True)
    module.write_text('# mutable checkout')
    (module.parents[2] / '.aicc-release-sha').write_text(SHA)
    monkeypatch.setenv('AICC_RELEASE_SHA', SHA)
    monkeypatch.setenv('GITHUB_SHA', SHA)
    client = client_for(monkeypatch, tmp_path, module)
    response = client.get('/api/version', headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {'status': 'unknown', 'sha': None}
    assert client.get('/healthz', headers=HEADERS).status_code == 200


def test_version_is_authenticated_and_read_only(tmp_path, monkeypatch):
    client = client_for(monkeypatch, tmp_path, release(tmp_path))
    assert client.get('/api/version').status_code == 401
    assert client.get('/api/version', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.post('/api/version', headers=HEADERS).status_code == 405
    response = client.get('/api/version', headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        'status': 'known', 'sha': SHA, 'source': 'immutable_release_at_startup',
    }
    assert response.headers['Cache-Control'] == 'no-store'


def test_current_switch_after_import_does_not_change_running_snapshot(tmp_path, monkeypatch):
    old_module = release(tmp_path)
    new_module = release(tmp_path, sha='b' * 40)
    current = tmp_path / 'current'
    current.symlink_to(old_module.parents[2])
    import_path = (current / 'command_center/webapi/owner_app.py').resolve()
    current.unlink()
    current.symlink_to(new_module.parents[2])
    client = client_for(monkeypatch, tmp_path, import_path)
    assert client.get('/api/version', headers=HEADERS).json()['sha'] == SHA
    (old_module.parents[2] / '.aicc-release-sha').unlink()
    assert client.get('/api/version', headers=HEADERS).json()['sha'] == SHA


def test_unresolved_selector_is_not_accepted(tmp_path):
    module = release(tmp_path)
    current = tmp_path / 'current'
    current.symlink_to(module.parents[2])
    assert capture_runtime_version(current / 'command_center/webapi/owner_app.py').sha is None


def artifact_bytes(**overrides):
    import json
    value = {
        'schema': 'delivery-release-observation/v1', 'task_id': 'task-1',
        'revision': 1, 'repo': 'owner/repo', 'environment': 'production',
        'sha': SHA, 'version_url': 'https://example.invalid/api/version',
        'health_url': 'https://example.invalid/healthz', 'health': 'ok',
        'criteria': ['criterion-1'], 'started_at': '2026-11-04T00:00:00Z',
        'observed_at': '2026-11-04T00:00:01Z', 'business_acceptance': False,
    }
    value.update(overrides)
    return (json.dumps(value) + '\n').encode()


def artifact_file(root, raw):
    import hashlib
    root.mkdir(exist_ok=True)
    path = root / (hashlib.sha256(raw).hexdigest() + '.json')
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def test_artifact_read_preserves_bytes_and_auth(tmp_path, monkeypatch):
    raw = artifact_bytes()
    root = tmp_path / 'artifacts'
    path = artifact_file(root, raw)
    monkeypatch.setenv('AICC_TEAM_ARTIFACT_DIR', str(root))
    client = client_for(monkeypatch, tmp_path, release(tmp_path))
    url = '/api/team/artifacts/' + path.name
    assert client.get(url).status_code == 401
    assert client.post(url, headers=HEADERS).status_code == 405
    response = client.get(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers['content-type'] == 'application/json'
    assert response.headers['cache-control'] == 'no-store'
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('configured', [False, True])
def test_artifact_missing_is_generic_404(tmp_path, monkeypatch, configured):
    if configured:
        monkeypatch.setenv('AICC_TEAM_ARTIFACT_DIR', str(tmp_path / 'absent'))
    else:
        monkeypatch.delenv('AICC_TEAM_ARTIFACT_DIR', raising=False)
    client = client_for(monkeypatch, tmp_path, release(tmp_path))
    response = client.get('/api/team/artifacts/' + 'a' * 64 + '.json', headers=HEADERS)
    assert response.status_code == 404
    assert response.json() == {'detail': 'Artifact not found'}


@pytest.mark.parametrize('name', ['../secret.json', 'A' * 64 + '.json',
                                  'a' * 64 + '.txt', 'a' * 63 + '.json'])
def test_artifact_filename_is_strict(tmp_path, name):
    assert owner_app._read_team_artifact(tmp_path, name) is None


@pytest.mark.parametrize('kind', ['root_link', 'file_link', 'directory', 'fifo'])
def test_artifact_rejects_symlinks_and_nonregular_files(tmp_path, monkeypatch, kind):
    import os
    root = tmp_path / 'artifacts'
    path = artifact_file(root, artifact_bytes())
    configured_root = root
    if kind == 'root_link':
        configured_root = tmp_path / 'linked-artifacts'
        configured_root.symlink_to(root)
    else:
        path.unlink()
        if kind == 'file_link':
            target = tmp_path / 'secret'
            target.write_bytes(artifact_bytes())
            path.symlink_to(target)
        elif kind == 'directory':
            path.mkdir()
        else:
            os.mkfifo(path)
    monkeypatch.setenv('AICC_TEAM_ARTIFACT_DIR', str(configured_root))
    client = client_for(monkeypatch, tmp_path, release(tmp_path))
    assert client.get('/api/team/artifacts/' + path.name, headers=HEADERS).status_code == 404


@pytest.mark.parametrize('raw', [
    b'not json', b'[]', b'{"schema":"delivery-release-observation/v1"}',
    artifact_bytes(schema='other/v1'), artifact_bytes(secret='must not serve'),
    artifact_bytes(business_acceptance=True), b' ' * 65537,
])
def test_artifact_rejects_unexpected_content(tmp_path, raw):
    path = artifact_file(tmp_path, raw)
    assert owner_app._read_team_artifact(tmp_path, path.name) is None


def test_artifact_hash_is_checked_on_actual_bytes(tmp_path):
    path = artifact_file(tmp_path, artifact_bytes())
    path.write_bytes(artifact_bytes(task_id='changed'))
    assert owner_app._read_team_artifact(tmp_path, path.name) is None


def test_artifact_rejects_duplicate_schema(tmp_path):
    raw = artifact_bytes().replace(
        b'{', b'{"schema":"delivery-release-observation/v1",', 1,
    )
    path = artifact_file(tmp_path, raw)
    assert owner_app._read_team_artifact(tmp_path, path.name) is None
