"""The fail-closed monitors' finding lifecycle, proved against a real server.

VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #481). The stall
clock itself is measured in `test_infra_monitor_queue_snapshot.py`; this file
covers the other half of the acceptance -- "the monitor clears the finding
when it measures healthy" -- which the schema could not keep.

0021 records ONE finding per (source, failure), because the planner mints one
task per failure. It could only clear ALL of a source's findings at once, and
`infra_monitor.record_findings` therefore cleared on an all-green tick and on
no other. `control-01:queue` reports six independent failures through that one
source and runs with `--max-recent-dead 0`: a single dead-lettered item in the
trailing hour keeps `dead_letter_growth` red, so on a fleet that dead-letters
at all the all-green tick never comes -- and `queue_stalled`, healthy again
since the stall clock was fixed, stays `open` behind it with its task, forever.

0024 adds the set difference: clear this source's open findings EXCEPT the
ones still being measured. Why a real server rather than a stub cursor:

* The behaviour under test is the `failure <> ALL (...)` predicate and the
  identity it preserves (`finding_id`, `opened_at`, and the `task_id` the
  planner linked). A fake cursor would assert the test author's idea of what
  an array predicate does.
* It runs as `aicc_app` and as `aicc_worker`, the two identities that record
  findings (the control-host queue probe and the worker-host probe). The
  overload is useless to them without its GRANT, and only running as the
  roles proves the GRANT is there.
"""

from __future__ import annotations

import json

import pytest

from command_center.db import roles
from command_center.ops import infra_monitor

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

SOURCE = "control-01:queue"


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


@pytest.fixture
def findings(admin_conn, psycopg, test_dsn, role_passwords):
    """``(as_role, open_findings)`` over a migrated database.

    ``as_role`` opens a connection as one of the product roles -- never as the
    superuser, whose privileges would hide a missing GRANT. ``open_findings``
    reads the rows back with `aicc_app`'s SELECT.
    """
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)

    def as_role(role: str, *, autocommit: bool = True):
        return psycopg.connect(
            _as_role(test_dsn, role, role_passwords[role]), autocommit=autocommit
        )

    def open_findings(source: str = SOURCE) -> dict[str, tuple]:
        """``{failure: (finding_id, task_id, opened_at)}`` for the open rows."""
        with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT failure, finding_id, task_id, opened_at FROM monitor_finding "
                "WHERE source = %s AND state = 'open'",
                (source,),
            )
            return {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}

    return as_role, open_findings


def _record(cur, source: str, failure: str) -> int:
    cur.execute(
        "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
        (source, failure, json.dumps({"failure": failure})),
    )
    return cur.fetchone()[0]


def test_a_resolved_failure_clears_while_a_red_sibling_keeps_its_row(findings) -> None:
    """THE REGRESSION. Two failures on one source; the next tick still
    measures only one of them. The resolved one closes; the surviving one is
    the SAME row -- same `finding_id`, same `opened_at`, same linked task --
    because reopening it would hand the planner a task-less finding and file
    the same task again."""
    as_role, open_findings = findings
    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        stalled = _record(cur, SOURCE, "queue_stalled")
        dead = _record(cur, SOURCE, "dead_letter_growth")
        # The planner's one write to a finding: the task it filed for it.
        cur.execute("SELECT monitor_link_task(%s, %s)", (dead, "VOYN-MON-DEAD-LETTER"))

    before = open_findings()
    assert set(before) == {"queue_stalled", "dead_letter_growth"}

    # The tick that follows the stall fix: the queue measures healthy, the
    # dead-letter count does not.
    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        _record(cur, SOURCE, "dead_letter_growth")
        cur.execute(
            "SELECT monitor_clear_finding(%s, %s)", (SOURCE, ["dead_letter_growth"])
        )
        assert cur.fetchone()[0] == 1  # one row cleared, not both

    after = open_findings()
    assert set(after) == {"dead_letter_growth"}
    assert after["dead_letter_growth"] == before["dead_letter_growth"]
    assert after["dead_letter_growth"][1] == "VOYN-MON-DEAD-LETTER"

    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state, cleared_at FROM monitor_finding WHERE finding_id = %s",
            (stalled,),
        )
        state, cleared_at = cur.fetchone()
        assert state == "cleared" and cleared_at is not None


def test_an_empty_argument_clears_the_source_like_the_one_argument_form(
    findings,
) -> None:
    """A green tick passes no failures, which must still mean "clear it all" --
    the behaviour `monitor_clear_finding(text)` has and that the monitor's
    healthy path depends on. NULL is the same statement: an argument the
    driver did not fill in must not silently spare every open row."""
    as_role, open_findings = findings
    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        _record(cur, SOURCE, "queue_stalled")
        _record(cur, SOURCE, "dead_letter_growth")
        cur.execute("SELECT monitor_clear_finding(%s, %s)", (SOURCE, None))
        assert cur.fetchone()[0] == 2
    assert open_findings() == {}

    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        _record(cur, SOURCE, "queue_stalled")
        cur.execute("SELECT monitor_clear_finding(%s, %s)", (SOURCE, []))
        assert cur.fetchone()[0] == 1
    assert open_findings() == {}


def test_the_clear_is_scoped_to_its_own_source(findings) -> None:
    """`p_source` is the whole authority: one probe reconciling its findings
    must not close another host's, whose measurement it never took."""
    as_role, open_findings = findings
    with as_role(roles.APP_ROLE) as conn, conn.cursor() as cur:
        _record(cur, SOURCE, "queue_stalled")
        _record(cur, "worker-01:infra", "active_workers")
        cur.execute("SELECT monitor_clear_finding(%s, %s)", (SOURCE, []))
        assert cur.fetchone()[0] == 1

    assert open_findings() == {}
    assert set(open_findings("worker-01:infra")) == {"active_workers"}


def test_the_worker_host_probe_may_clear_its_own_findings_too(findings) -> None:
    """The worker-host probe records under `aicc_worker` (0021), so the
    overload is granted to it as well -- and to nothing else: `aicc_worker`
    still may not read `monitor_finding`, which is the control plane's."""
    as_role, _open_findings = findings
    with as_role(roles.WORKER_ROLE) as conn, conn.cursor() as cur:
        _record(cur, "worker-01:infra", "active_workers")
        cur.execute(
            "SELECT monitor_clear_finding(%s, %s)", ("worker-01:infra", ["prometheus"])
        )
        assert cur.fetchone()[0] == 1
    assert "monitor_clear_finding(text, text[])" in roles.FUNCTION_PRIVILEGES[
        roles.WORKER_ROLE
    ]
    assert roles.PRIVILEGES[roles.WORKER_ROLE].get(
        "monitor_finding", frozenset()
    ) == frozenset()


def test_record_findings_reconciles_a_red_tick_end_to_end(
    findings, monkeypatch
) -> None:
    """The production function against the production functions.

    `infra_monitor.record_findings` is what the deployed probe calls every two
    minutes; pointing its pool at this database proves the whole path -- the
    keys it records, the keys it spares, and the commit -- rather than the two
    statements in isolation.
    """
    from contextlib import contextmanager

    from command_center.db import pool

    as_role, open_findings = findings

    @contextmanager
    def connection():
        with as_role(roles.APP_ROLE, autocommit=False) as conn:
            yield conn

    monkeypatch.setattr(pool, "open_pool", lambda config=None: None)
    monkeypatch.setattr(pool, "close_pool", lambda: None)
    monkeypatch.setattr(pool, "connection", connection)
    monkeypatch.setattr("command_center.db.config.load_config", lambda: None)

    detail = {"ok": False, "queue": {"ready": 3}}
    infra_monitor.record_findings(
        SOURCE, ("queue_stalled", "dead_letter_growth:53>0"), detail
    )
    assert set(open_findings()) == {"queue_stalled", "dead_letter_growth"}
    opened = open_findings()

    # The stall clock now measures healthy; the dead letters are unchanged.
    infra_monitor.record_findings(SOURCE, ("dead_letter_growth:54>0",), detail)
    surviving = open_findings()
    assert set(surviving) == {"dead_letter_growth"}
    # Same row, refreshed rather than reopened -- the finding is keyed by the
    # failure CODE, so a changed measurement is not a new finding.
    assert surviving["dead_letter_growth"] == opened["dead_letter_growth"]

    infra_monitor.record_findings(SOURCE, (), detail)
    assert open_findings() == {}
