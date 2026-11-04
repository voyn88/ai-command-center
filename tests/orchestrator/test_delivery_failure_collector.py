"""Failure observations must bind real provider/file evidence without leaking it."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os

from types import SimpleNamespace

import pytest

from command_center.orchestrator import delivery_failure_collector as collector


TASK = 'VOYN-TEST-FAILURE'
REPO = 'voyn88/ai-command-center'
SHA = 'a' * 40
OLD_SHA = 'b' * 40
AT = '2026-11-04T12:00:00Z'
SECRET = 'provider-title token=private-do-not-copy'


class Connection:
    def __init__(self):
        self.repo = REPO
        self.queries = []
        self.transactions = 0

    @contextmanager
    def cursor(self):
        connection = self

        class Cursor:
            def execute(self, sql, params):
                connection.queries.append((sql, params))

            def fetchone(self):
                return (connection.repo,) if connection.repo else None

        yield Cursor()

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


@pytest.fixture
def evidence(monkeypatch):
    connection = Connection()
    calls = []
    monkeypatch.setattr(collector, 'enabled', lambda: True)
    monkeypatch.setattr(collector.gh_access, "current_quota", lambda: SimpleNamespace(
        identity=collector.gh_access.FLEET, allow_ambient_fallback=False, degraded=False))
    monkeypatch.setattr(collector.planner, 'repo_route', lambda repo: ('project', '/trusted/repo'))
    monkeypatch.setattr(collector.review_merge, '_origin_owner_repo', lambda path: tuple(REPO.split('/')))

    def record(conn, **kwargs):
        assert conn is connection
        calls.append(kwargs)
        return True

    monkeypatch.setattr(collector, 'record_incident', record)
    return connection, calls


def ci_run(**overrides):
    row = {
        'id': 42, 'run_attempt': 2, 'repository': {'full_name': REPO},
        'head_sha': SHA, 'status': 'completed', 'conclusion': 'failure',
        'updated_at': AT, 'display_title': SECRET,
        'html_url': 'https://attacker.invalid/' + SECRET,
    }
    row.update(overrides)
    return row


def observe_ci(monkeypatch, connection, row=None, status='ok', **overrides):
    row = ci_run() if row is None else row
    requests = []

    def rest(path, endpoint):
        requests.append((path, endpoint))
        return status, row

    monkeypatch.setattr(collector.review_merge, '_rest_json', rest)
    args = dict(task_id=TASK, revision=3, expected_sha=SHA, run_id=42, attempt=2)
    args.update(overrides)
    result = collector.collect_ci_failure(connection, **args)
    return result, requests


def deploy_row(**overrides):
    row = {'at': AT, 'outcome': 'failed', 'detail': SECRET,
           'previous_sha': OLD_SHA, 'target_sha': SHA}
    row.update(overrides)
    return (json.dumps(row) + '\n').encode()


def source_file(tmp_path, raw=None):
    raw = deploy_row() if raw is None else raw
    path = tmp_path / 'provenance.jsonl'
    path.write_bytes(raw)
    return collector.DeploymentSource(REPO, 'host-1', 'self-deploy', path), hashlib.sha256(raw).hexdigest()


def collect_deploy(connection, source, digest, **overrides):
    args = dict(task_id=TASK, revision=3, expected_sha=SHA, source=source,
                offset=0, sha256=digest)
    args.update(overrides)
    return collector.collect_deploy_failure(connection, **args)


def test_exact_ci_attempt_and_canonical_scope_are_admitted(evidence, monkeypatch):
    connection, calls = evidence
    result, requests = observe_ci(monkeypatch, connection)
    assert result.outcome == 'admitted'
    assert requests == [('/trusted/repo', f'repos/{REPO}/actions/runs/42/attempts/2')]
    assert connection.transactions == 1
    assert len(connection.queries) == 2
    assert all(params == (TASK, 3, SHA) for _, params in connection.queries)
    assert all("e.kind='sha'" in sql and 'delivery_team_stage' in sql
               and 'b.revision=%s' in sql for sql, _ in connection.queries)
    assert calls == [{
        'task_id': TASK, 'revision': 3, 'repo': REPO, 'sha': SHA,
        'run_ref': f'github-actions:{REPO}:42:2',
        'signature': 'workflow_attempt:failure', 'kind': 'ci_failure',
        'reference': f'https://github.com/{REPO}/actions/runs/42/attempts/2',
        'observed_at': AT,
        'detail': 'GitHub Actions run 42 attempt 2 completed with failure.',
    }]
    assert SECRET not in repr(calls) + repr(result)


@pytest.mark.parametrize('field,value', [
    ('id', 43), ('id', True), ('run_attempt', 1), ('run_attempt', '2'),
    ('repository', {'full_name': 'foreign/repo'}), ('repository', None),
    ('head_sha', OLD_SHA),
])
def test_ci_foreign_observation_never_records(evidence, monkeypatch, field, value):
    connection, calls = evidence
    result, _ = observe_ci(monkeypatch, connection, ci_run(**{field: value}))
    assert (result.outcome, result.reason) == ('refused', 'foreign_ci_observation')
    assert not calls


@pytest.mark.parametrize('arguments', [
    {'run_id': True}, {'run_id': 0}, {'run_id': '42'}, {'attempt': False},
    {'attempt': -1}, {'revision': True}, {'revision': 0},
    {'task_id': 'arbitrary-task'}, {'expected_sha': 'short'},
])
def test_invalid_ci_request_has_no_database_or_provider_io(evidence, monkeypatch, arguments):
    connection, calls = evidence
    result, requests = observe_ci(monkeypatch, connection, **arguments)
    assert result.reason == 'invalid_request'
    assert not requests and not connection.queries and not calls


@pytest.mark.parametrize('status,row', [('error', ci_run()), ('quota', ci_run()), ('ok', [])])
def test_api_errors_are_not_ci_failures(evidence, monkeypatch, status, row):
    connection, calls = evidence
    result, _ = observe_ci(monkeypatch, connection, row, status)
    assert result.reason == 'ci_observation_unavailable'
    assert not calls and connection.transactions == 0


@pytest.mark.parametrize('overrides,reason', [
    ({'conclusion': 'cancelled'}, 'ci_non_failure'),
    ({'conclusion': 'skipped'}, 'ci_non_failure'),
    ({'conclusion': 'success'}, 'ci_non_failure'),
    ({'conclusion': 'neutral'}, 'ci_non_failure'),
    ({'conclusion': 'action_required'}, 'ci_non_failure'),
    ({'status': 'queued', 'conclusion': None}, 'ci_not_completed'),
    ({'status': 'in_progress'}, 'ci_not_completed'),
])
def test_nonfailure_workflow_states_are_ignored(evidence, monkeypatch, overrides, reason):
    connection, calls = evidence
    result, _ = observe_ci(monkeypatch, connection, ci_run(**overrides))
    assert (result.outcome, result.reason) == ('ignored', reason)
    assert not calls


@pytest.mark.parametrize('overrides', [
    {'conclusion': None}, {'conclusion': 'unknown'}, {'conclusion': []},
    {'updated_at': 'yesterday'}, {'updated_at': '2026-11-04T12:00:00'},
])
def test_malformed_ci_fields_are_refused(evidence, monkeypatch, overrides):
    connection, calls = evidence
    result, _ = observe_ci(monkeypatch, connection, ci_run(**overrides))
    assert result.outcome == 'refused'
    assert not calls


def test_ci_replay_key_stable_but_new_attempt_is_distinct(evidence, monkeypatch):
    connection, calls = evidence
    first, _ = observe_ci(monkeypatch, connection)
    replay, _ = observe_ci(monkeypatch, connection, ci_run(updated_at='2026-11-04T12:01:00Z'))
    next_attempt, _ = observe_ci(monkeypatch, connection, ci_run(run_attempt=3), attempt=3)
    assert first.key == replay.key and first.key != next_attempt.key
    assert len(calls) == 3


def test_timed_out_is_a_failure(evidence, monkeypatch):
    connection, calls = evidence
    result, _ = observe_ci(monkeypatch, connection, ci_run(conclusion='timed_out'))
    assert result.outcome == 'admitted'
    assert calls[0]['signature'] == 'workflow_attempt:timed_out'


def test_unbound_task_never_fetches_provider(evidence, monkeypatch):
    connection, calls = evidence
    connection.repo = None
    result, requests = observe_ci(monkeypatch, connection)
    assert result.reason == 'unbound_task_or_sha'
    assert not requests and not calls


def test_scope_changed_during_provider_read_is_refused(evidence, monkeypatch):
    connection, calls = evidence

    def rest(*args):
        connection.repo = 'foreign/repo'
        return 'ok', ci_run()

    monkeypatch.setattr(collector.review_merge, '_rest_json', rest)
    result = collector.collect_ci_failure(connection, task_id=TASK, revision=3,
                                          expected_sha=SHA, run_id=42, attempt=2)
    assert result.reason == 'scope_changed'
    assert not calls


def test_database_admission_refusal_is_not_success(evidence, monkeypatch):
    connection, _ = evidence
    monkeypatch.setattr(collector, 'record_incident', lambda *args, **kwargs: False)
    result, _ = observe_ci(monkeypatch, connection)
    assert (result.outcome, result.reason, result.key) == ('refused', 'database_refused', None)


def test_disabled_collectors_perform_zero_io(evidence, monkeypatch, tmp_path):
    connection, calls = evidence
    monkeypatch.setattr(collector, 'enabled', lambda: False)

    def forbidden(*args, **kwargs):
        raise AssertionError('disabled collector performed IO')

    monkeypatch.setattr(collector, '_scope', forbidden)
    monkeypatch.setattr(collector, '_read_record', forbidden)
    monkeypatch.setattr(collector.review_merge, '_rest_json', forbidden)
    ci = collector.collect_ci_failure(connection, task_id=TASK, revision=3,
                                      expected_sha=SHA, run_id=42, attempt=2)
    deploy = collect_deploy(connection, collector.DeploymentSource(
        REPO, 'host-1', 'source', tmp_path / 'does-not-exist'), '0' * 64)
    assert ci.reason == deploy.reason == 'disabled'
    assert not calls and not connection.queries


def test_deploy_reads_exact_offset_and_hash_without_copying_detail(evidence, tmp_path):
    connection, calls = evidence
    prefix = deploy_row(outcome='deployed')
    raw = deploy_row(outcome='rolled_back')
    source, _ = source_file(tmp_path, prefix + raw)
    digest = hashlib.sha256(raw).hexdigest()
    result = collect_deploy(connection, source, digest, offset=len(prefix))
    assert result.outcome == 'admitted'
    assert calls[0]['kind'] == 'deployment_failure'
    assert calls[0]['reference'] == f'artifact://host-1/self-deploy/offset/{len(prefix)}#sha256={digest}'
    assert calls[0]['run_ref'] == f'self-deploy:host-1:self-deploy:{AT}:{digest}'
    assert calls[0]['signature'] == 'self_deploy:rolled_back'
    assert calls[0]['repo'] == REPO and calls[0]['sha'] == SHA
    assert calls[0]['task_id'] == TASK and calls[0]['revision'] == 3
    assert calls[0]['observed_at'] == AT
    assert SECRET not in repr(calls) + repr(result)
    assert all(params == (TASK, 3, SHA) for _, params in connection.queries)


def test_deploy_replay_identity_is_stable(evidence, tmp_path):
    connection, _ = evidence
    source, digest = source_file(tmp_path)
    first = collect_deploy(connection, source, digest)
    second = collect_deploy(connection, source, digest)
    assert first.outcome == second.outcome == 'admitted'
    assert first.key == second.key


@pytest.mark.parametrize('outcome', ['deployed', 'noop', 'refused'])
def test_deploy_success_and_refusal_are_not_failures(evidence, tmp_path, outcome):
    connection, calls = evidence
    source, digest = source_file(tmp_path, deploy_row(outcome=outcome))
    result = collect_deploy(connection, source, digest)
    assert (result.outcome, result.reason) == ('ignored', 'deployment_non_failure')
    assert not calls


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory', 'missing'])
def test_deploy_requires_regular_nonlinked_source(evidence, tmp_path, kind):
    connection, calls = evidence
    source, digest = source_file(tmp_path)
    source.path.unlink()
    if kind == 'symlink':
        target = tmp_path / 'other'
        target.write_bytes(deploy_row())
        source.path.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(source.path)
    elif kind == 'directory':
        source.path.mkdir()
    result = collect_deploy(connection, source, digest)
    assert result.reason == 'deployment_observation_unavailable'
    assert not calls


@pytest.mark.parametrize('raw', [
    deploy_row().rstrip(b'\n'), b'not-json\n', b'[]\n',
    b'{"at":"one","at":"two"}\n',
    deploy_row().replace(b'{', b'{"outcome":"failed",', 1),
    deploy_row(detail=None), deploy_row(at='2026-11-04T12:00:00'),
    deploy_row(extra='unexpected'), b' ' * 65536 + b'\n',
])
def test_deploy_partial_duplicate_malformed_oversized_records_refused(evidence, tmp_path, raw):
    connection, calls = evidence
    source, digest = source_file(tmp_path, raw)
    result = collect_deploy(connection, source, digest)
    assert result.outcome == 'refused'
    assert not calls


@pytest.mark.parametrize('overrides', [
    {'target_sha': OLD_SHA}, {'previous_sha': 'short'},
    {'outcome': 'unknown'}, {'outcome': []},
])
def test_deploy_mismatched_sha_or_malformed_outcome_refused(evidence, tmp_path, overrides):
    connection, calls = evidence
    source, digest = source_file(tmp_path, deploy_row(**overrides))
    result = collect_deploy(connection, source, digest)
    assert result.outcome == 'refused'
    assert not calls


@pytest.mark.parametrize('overrides', [
    {'offset': 1}, {'offset': -1}, {'offset': True}, {'offset': 100000},
    {'sha256': '0' * 64}, {'sha256': 'short'},
])
def test_deploy_invalid_offset_or_hash_cannot_record(evidence, tmp_path, overrides):
    connection, calls = evidence
    source, digest = source_file(tmp_path)
    result = collect_deploy(connection, source, digest, **overrides)
    assert result.reason == 'deployment_observation_unavailable'
    assert not calls


def test_deploy_foreign_repository_has_no_file_read(evidence, tmp_path, monkeypatch):
    connection, calls = evidence
    source, digest = source_file(tmp_path)
    source = replace(source, repo='foreign/repo')

    def forbidden(*args):
        raise AssertionError('foreign source was opened')

    monkeypatch.setattr(collector, '_read_record', forbidden)
    result = collect_deploy(connection, source, digest)
    assert result.reason == 'unbound_deployment_source'
    assert not calls


@pytest.mark.parametrize('overrides', [
    {'task_id': 'foreign'}, {'revision': False}, {'expected_sha': SHA.upper()},
])
def test_invalid_deployment_request_has_no_scope_or_file_io(evidence, tmp_path, monkeypatch, overrides):
    connection, calls = evidence
    source, digest = source_file(tmp_path)

    def forbidden(*args):
        raise AssertionError('invalid request read a file')

    monkeypatch.setattr(collector, '_read_record', forbidden)
    result = collect_deploy(connection, source, digest, **overrides)
    assert result.reason == 'invalid_request'
    assert not calls and not connection.queries


@pytest.mark.parametrize('overrides', [
    {'host_id': '../host'}, {'source_id': 'source/secret'}, {'host_id': None},
])
def test_deploy_source_identifiers_are_not_arbitrary_references(evidence, tmp_path, overrides):
    connection, calls = evidence
    source, digest = source_file(tmp_path)
    result = collect_deploy(connection, replace(source, **overrides), digest)
    assert result.reason == 'deployment_observation_unavailable'
    assert not calls


@pytest.mark.parametrize("quota", [None, SimpleNamespace(identity="ambient", allow_ambient_fallback=False, degraded=False),
    SimpleNamespace(identity="fleet-app", allow_ambient_fallback=True, degraded=False),
    SimpleNamespace(identity="fleet-app", allow_ambient_fallback=False, degraded=True)])
def test_ci_requires_existing_fleet_tick_without_fallback(evidence, monkeypatch, quota):
    connection, calls = evidence
    monkeypatch.setattr(collector.gh_access, "current_quota", lambda: quota)
    result, requests = observe_ci(monkeypatch, connection)
    assert result == collector.CollectionResult("refused", "fleet_tick_required")
    assert requests == [] and calls == []


def test_github_case_variants_keep_same_attempt_identity(evidence, monkeypatch):
    connection, calls = evidence
    first, _ = observe_ci(monkeypatch, connection)
    monkeypatch.setattr(collector.review_merge, '_origin_owner_repo', lambda path: ('VOYN88', 'AI-Command-Center'))
    second, requests = observe_ci(monkeypatch, connection, ci_run(repository={'full_name': 'Voyn88/AI-Command-Center'}))
    assert first.outcome == second.outcome == 'admitted'
    assert first.key == second.key
    assert requests[0][1] == 'repos/voyn88/ai-command-center/actions/runs/42/attempts/2'
    assert calls[0]['run_ref'] == calls[1]['run_ref']
