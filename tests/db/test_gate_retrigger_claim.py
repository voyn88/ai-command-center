"""The CI retry cap is durable across tasks and connection lifetimes."""
from contextlib import contextmanager

from command_center.db import roles
from command_center.orchestrator.review_merge import _claim_gate_retrigger
from tests.db.test_backlog_store import _provision


def test_gate_retrigger_is_once_per_pr_head_as_app(admin_conn, psycopg, test_dsn, role_passwords, connect_as):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    @contextmanager
    def factory():
        with connect_as(roles.APP_ROLE) as conn:
            yield conn
    with factory() as conn:
        for task in ('VOYN-GATE-ONE', 'VOYN-GATE-ALIAS'):
            conn.execute("SELECT * FROM backlog_upsert_task(%s,'0','P0','OPEN','task','test','','ai-command-center')", (task,))
    url='https://github.com/owner/repo/pull/12'
    head='a'*40
    # An unknown task cannot consume the retry. Initial window-label events
    # are irrelevant; only this durable, exact-head action is deduplicated.
    assert not _claim_gate_retrigger(factory,'VOYN-GATE-UNKNOWN',url,head)
    assert _claim_gate_retrigger(factory,'VOYN-GATE-ONE',url,head)
    assert not _claim_gate_retrigger(factory,'VOYN-GATE-ALIAS',url,head)
    assert _claim_gate_retrigger(factory,'VOYN-GATE-ALIAS',url,'b'*40)
    assert admin_conn.execute("SELECT count(*) FROM backlog_event WHERE reason='merge_gate_retrigger' AND outcome='granted'").fetchone()[0] == 2
