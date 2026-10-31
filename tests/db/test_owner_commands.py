from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from command_center.db import migrations, roles
from command_center.db.backlog_parser import ParsedTask
from command_center.db.backlog_store import BacklogStore
from command_center.webapi.owner_commands import Command, execute


@pytest.fixture
def owner_rig(admin_conn, role_passwords, connect_as):
    roles.apply_bootstrap(admin_conn)
    with connect_as(roles.MIGRATOR_ROLE) as migrator:
        migrations.upgrade(migrator)
        roles.apply_table_grants(migrator)

    @contextmanager
    def factory():
        with connect_as(roles.APP_ROLE) as conn:
            yield conn

    store = BacklogStore(factory)

    def seed(task_id='VOYN-W0-OWNER-TEST', status='DEFER_TO_USER'):
        assert store.upsert_task(ParsedTask(task_id, '0', 'P1', status, 'task', 'Owner task', 'Implement isolated test', 'ai-command-center', 1))[0]
        with factory() as conn:
            revision = conn.execute('SELECT revision FROM backlog_task WHERE task_id=%s', (task_id,)).fetchone()[0]
        return Command(task_id=task_id, expected_revision=revision, idempotency_key=uuid4(), device_id=uuid4(), action='decision', decision='resume', note='Proceed with the selected scope')

    def send(command, allowed=frozenset({'launch', 'decision'})):
        with factory() as conn, conn.transaction():
            return execute(conn, 'owner', command, allowed, 2)

    return admin_conn, seed, send


def test_owner_decision_replay_conflict_and_scoped_actor(owner_rig):
    conn, seed, send = owner_rig
    command = seed()
    result = send(command)
    assert result['ok'] and result['reason'] == 'OPEN'
    assert send(command) == result
    assert send(command.model_copy(update={'note': 'Changed answer'}))['reason'] == 'idempotency_conflict'
    assert conn.execute("SELECT count(*) FROM backlog_event WHERE event='owner_decision' AND outcome='granted'").fetchone()[0] == 1
    assert 'Proceed with the selected scope' in conn.execute('SELECT body FROM backlog_task WHERE task_id=%s', (command.task_id,)).fetchone()[0]
    receipt = conn.execute("SELECT detail FROM backlog_event WHERE event='provenance' AND reason='owner-command:v1'").fetchone()[0]
    assert receipt['owner'] == 'owner'
    assert receipt['request']['note'] == command.note


def test_concurrent_retries_have_one_effect(owner_rig):
    conn, seed, send = owner_rig
    command = seed()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(send, [command] * 4))
    assert all(r == results[0] for r in results)
    assert conn.execute("SELECT count(*) FROM backlog_event WHERE event='owner_decision' AND outcome='granted'").fetchone()[0] == 1


def test_launch_narrows_existing_planner_and_retries_once(owner_rig):
    conn, seed, send = owner_rig
    seed('VOYN-W0-OWNER-OTHER', 'OPEN')
    conn.execute("SELECT monitor_record_finding('test-owner','unrelated_failure','{}'::jsonb)")
    command = seed(status='OPEN').model_copy(update={'action': 'launch', 'decision': None, 'note': ''})
    result = send(command)
    assert result['ok'], result
    assert send(command) == result
    assert conn.execute('SELECT task_id FROM work_item').fetchall() == [(command.task_id,)]
    assert conn.execute("SELECT status FROM backlog_task WHERE task_id='VOYN-W0-OWNER-OTHER'").fetchone()[0] == 'OPEN'
    assert conn.execute('SELECT count(*) FROM backlog_task').fetchone()[0] == 2
    assert conn.execute('SELECT task_id FROM monitor_finding').fetchone()[0] is None


def test_active_planner_cannot_be_bypassed(owner_rig):
    conn, seed, send = owner_rig
    command = seed(status='OPEN').model_copy(update={'action': 'launch'})
    conn.execute("SELECT backlog_lease_acquire('planner:global','aicc-planner',300)")
    assert send(command)['reason'] == 'planner_busy'
    assert conn.execute('SELECT count(*) FROM work_item').fetchone()[0] == 0


def test_read_grant_and_stale_revision_cannot_mutate(owner_rig):
    conn, seed, send = owner_rig
    command = seed()
    assert send(command, frozenset({'read'}))['reason'] == 'forbidden'
    stale = command.model_copy(update={'idempotency_key': uuid4(), 'expected_revision': command.expected_revision + 1})
    assert send(stale)['reason'] == 'revision_conflict'
    assert conn.execute('SELECT status FROM backlog_task').fetchone()[0] == 'DEFER_TO_USER'


def test_decision_cannot_skip_review_or_forge_done(owner_rig):
    conn, seed, send = owner_rig
    command = seed(status='READY_TO_REVIEW')
    assert send(command)['reason'] == 'not_awaiting_decision'
    assert conn.execute('SELECT status FROM backlog_task').fetchone()[0] == 'READY_TO_REVIEW'


def test_failed_receipt_rolls_back_effect(owner_rig, monkeypatch):
    import command_center.webapi.owner_commands as module
    conn, seed, send = owner_rig
    command = seed()
    original = module._row

    def fail_receipt(connection, sql, params=()):
        if 'backlog_record_provenance' in sql:
            raise RuntimeError('receipt unavailable')
        return original(connection, sql, params)

    monkeypatch.setattr(module, '_row', fail_receipt)
    with pytest.raises(RuntimeError):
        send(command)
    assert conn.execute('SELECT status FROM backlog_task').fetchone()[0] == 'DEFER_TO_USER'
