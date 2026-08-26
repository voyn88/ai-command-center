"""The structured backlog store, proved against real PostgreSQL as aicc_app.

The 0005 functions own the semantics; these tests prove them under the real
grants — the role the importer, the CLI and (BO-S2) the planner authenticate
as. A superuser connection would prove nothing about the write path actually
available to the control plane.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from command_center.db import roles
from command_center.db.backlog_parser import ParsedTask
from command_center.db.backlog_store import BacklogStore

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "backlog_sample.md"


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@pytest.fixture
def store(admin_conn, psycopg, test_dsn, role_passwords):
    from contextlib import contextmanager

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    @contextmanager
    def factory():
        with psycopg.connect(app_dsn, autocommit=True) as conn:
            yield conn

    yield BacklogStore(factory)


def _task(task_id: str, **overrides) -> ParsedTask:
    values = dict(
        task_id=task_id,
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title=task_id.lower(),
        body="",
        repo=None,
        line_no=1,
    )
    values.update(overrides)
    return ParsedTask(**values)


# -- the status machine -------------------------------------------------------


FORBIDDEN = [
    ("OPEN", "DONE"),
    ("OPEN", "READY_TO_REVIEW"),
    ("OPEN", "OPEN"),
    ("IN_PROGRESS", "DONE"),
    ("IN_PROGRESS", "OPEN"),
    ("READY_TO_REVIEW", "OPEN"),
    ("READY_TO_REVIEW", "IN_PROGRESS"),
    ("DONE", "OPEN"),
    ("DONE", "IN_PROGRESS"),
    ("UNTRIAGED", "IN_PROGRESS"),
    ("DEFER_TO_USER", "DONE"),
]


@pytest.mark.parametrize("start,target", FORBIDDEN)
def test_every_forbidden_transition_is_refused(store, start, target) -> None:
    task_id = f"VOYN-W0-TR-{start[:2]}-{target[:2]}"
    assert store.upsert_task(_task(task_id, status=start))[0]
    ok, reason, _ = store.transition(task_id, target, expected_revision=1)
    assert ok is False
    assert reason.startswith(("illegal_transition", "revision_conflict")) is True
    assert reason.startswith("illegal_transition"), reason
    assert store.get_task(task_id)["status"] == start, "a refusal changes nothing"


def test_the_executable_chain_walks_one_step_at_a_time(store) -> None:
    assert store.upsert_task(_task("VOYN-W0-CHAIN"))[0]
    ok, _, rev = store.transition("VOYN-W0-CHAIN", "IN_PROGRESS", 1)
    assert ok and rev == 2
    ok, _, rev = store.transition("VOYN-W0-CHAIN", "READY_TO_REVIEW", 2)
    assert ok and rev == 3
    # DONE is machine-gated on evidence: a pr and a merged sha, not a claim.
    ok, reason, _ = store.transition("VOYN-W0-CHAIN", "DONE", 3)
    assert not ok and reason.startswith("missing_evidence")
    assert store.record_evidence("VOYN-W0-CHAIN", "pr", "#999")[0]
    ok, reason, _ = store.transition("VOYN-W0-CHAIN", "DONE", 3)
    assert not ok, "a pr alone is not a merge"
    assert store.record_evidence("VOYN-W0-CHAIN", "sha", "deadbeef")[0]
    ok, _, rev = store.transition("VOYN-W0-CHAIN", "DONE", 3)
    assert ok and rev == 4
    assert store.get_task("VOYN-W0-CHAIN")["status"] == "DONE"


def test_a_gate_is_a_control_record_not_an_executable_task(store) -> None:
    assert store.upsert_task(_task("VOYN-W0-G9", kind="gate", title="gate"))[0]
    ok, reason, _ = store.transition("VOYN-W0-G9", "IN_PROGRESS", 1)
    assert not ok and reason == "gate_is_control_record"


def test_optimistic_lock_refuses_the_loser_of_a_race(store) -> None:
    """Two writers read revision 1; the first's transition lands, the second's
    write against the now-stale revision is refused — not silently merged."""
    assert store.upsert_task(_task("VOYN-W0-RACE"))[0]
    first = store.transition("VOYN-W0-RACE", "IN_PROGRESS", 1)
    assert first[0] and first[2] == 2
    second = store.transition("VOYN-W0-RACE", "IN_PROGRESS", 1)  # stale read
    assert second[0] is False and second[1] == "revision_conflict"
    assert second[2] == 2, "the refusal reports the current revision for re-read"


# -- dependencies -------------------------------------------------------------


def test_a_dependency_cycle_is_refused_with_its_path(store) -> None:
    for name in ("VOYN-W0-DA", "VOYN-W0-DB", "VOYN-W0-DC"):
        assert store.upsert_task(_task(name))[0]
    assert store.add_dependency("VOYN-W0-DA", "VOYN-W0-DB")[0]
    assert store.add_dependency("VOYN-W0-DB", "VOYN-W0-DC")[0]
    ok, reason, path = store.add_dependency("VOYN-W0-DC", "VOYN-W0-DA")
    assert not ok and reason == "cycle"
    assert path == ["VOYN-W0-DC", "VOYN-W0-DA", "VOYN-W0-DB", "VOYN-W0-DC"]
    ok, reason, _ = store.add_dependency("VOYN-W0-DA", "VOYN-W0-DA")
    assert not ok and reason == "self_dependency"
    # Idempotent re-add of an existing edge is an ok, not a duplicate error.
    assert store.add_dependency("VOYN-W0-DA", "VOYN-W0-DB")[0]


# -- the writer lease ---------------------------------------------------------


def test_lease_protocol_one_writer_per_authority(store) -> None:
    ok, _ = store.lease_acquire("repo:test-repo", "writer-a", 60)
    assert ok
    ok, reason = store.lease_acquire("repo:test-repo", "writer-b", 60)
    assert not ok and reason == "held"
    assert store.lease_heartbeat("repo:test-repo", "writer-a", 60)[0]
    assert store.lease_heartbeat("repo:test-repo", "writer-b", 60) == (
        False,
        "not_owner",
    )
    assert store.lease_release("repo:test-repo", "writer-b") == (False, "not_owner")
    assert store.lease_release("repo:test-repo", "writer-a")[0]
    ok, _ = store.lease_acquire("repo:test-repo", "writer-b", 60)
    assert ok, "a released authority is free"


def test_lease_takeover_only_after_proven_expiry(store) -> None:
    assert store.lease_acquire("repo:exp", "napper", 1)[0]
    assert store.lease_acquire("repo:exp", "usurper", 60) == (False, "held")
    deadline = time.monotonic() + 10
    taken = (False, "")
    while time.monotonic() < deadline:
        taken = store.lease_acquire("repo:exp", "usurper", 60)
        if taken[0]:
            break
        time.sleep(0.2)
    assert taken[0], "the expired lease was never taken over"
    # The napper's heartbeat now meets the fence, not a revival.
    assert store.lease_heartbeat("repo:exp", "napper", 60) == (False, "not_owner")


# -- the importer -------------------------------------------------------------


def test_import_is_idempotent_and_loses_nothing(store) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    first = store.import_markdown(text)
    assert first.inserted > 0 and first.refused == []
    assert first.unparsed, "the fixture's malformed lines must be reported"

    second = store.import_markdown(text)
    assert second.changed == 0, "a second run over the same text changes nothing"
    assert second.unchanged == first.inserted + first.updated + first.unchanged

    # The store now answers the projection's questions.
    counts = store.counts_by_status()
    assert counts.get("OPEN", 0) >= 3
    assert store.get_task("VOYN-W0-S3")["status"] == "IN_PROGRESS"
    assert store.get_task("VOYN-W0-G1")["kind"] == "gate"


def test_export_round_trips_the_whole_store_through_the_projection(store) -> None:
    """BO-S4: `export_tasks` plus `render_backlog` plus a re-import must be a
    no-op — the property that makes the generated file trustworthy as a read
    projection rather than a lossy summary."""
    from command_center.db.backlog_parser import parse_backlog
    from command_center.db.backlog_projection import render_backlog

    text = FIXTURE.read_text(encoding="utf-8")
    first = store.import_markdown(text)
    assert first.inserted > 0

    exported = store.export_tasks()
    assert len(exported) == first.inserted
    assert all("body" in task for task in exported), "export must carry body"

    rendered = render_backlog(exported, generated_at="2026-08-26T00:00:00+00:00")
    reparsed = parse_backlog(rendered)
    assert reparsed.unparsed == [], "a generated file must always re-parse clean"
    assert {t.task_id for t in reparsed.tasks} == {t["task_id"] for t in exported}

    # Feeding the generated file back through the importer changes nothing:
    # the store, exported and re-imported, is a fixed point.
    reimported = store.import_markdown(rendered)
    assert reimported.changed == 0, "export -> import must be a no-op"


def test_import_reports_a_record_the_schema_refuses(store) -> None:
    """The parser and the CHECKs are two fences; a record that leaps the
    first must still be caught, reported and not half-written by the second."""
    bad = _task("VOYN-W0-BADPRI", priority="P99")  # parser would not produce it
    ok, reason, _ = store.upsert_task(bad)
    assert not ok and reason.startswith("constraint:")
    assert store.get_task("VOYN-W0-BADPRI") is None


def test_app_role_cannot_write_the_tables_directly(
    store, psycopg, admin_conn, test_dsn, role_passwords
) -> None:
    """The status machine is unbypassable because no SQL write path exists:
    aicc_app holds SELECT and EXECUTE, nothing else."""
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    assert store.upsert_task(_task("VOYN-W0-NODML"))[0]
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE backlog_task SET status = 'DONE' WHERE task_id = %s",
                    ("VOYN-W0-NODML",),
                )


# -- list_tasks / list_events / list_evidence (VOYN-W0-AICC-DESKTOP-BACKLOG-VIEW) --


def test_list_tasks_filters_paginates_and_counts_the_total(store) -> None:
    for i in range(3):
        assert store.upsert_task(_task(f"VOYN-W0-LT{i}"))[0]
    assert store.upsert_task(_task("VOYN-W0-LT-DONE", status="UNTRIAGED"))[0]

    page, total = store.list_tasks(status="OPEN", limit=2, offset=0)
    assert total == 3  # the full filtered count, not just this page's length
    assert len(page) == 2
    ids_page1 = {t["task_id"] for t in page}

    page2, total2 = store.list_tasks(status="OPEN", limit=2, offset=2)
    assert total2 == 3
    assert len(page2) == 1
    assert page2[0]["task_id"] not in ids_page1  # no overlap across pages

    unfiltered, total_all = store.list_tasks(limit=100)
    assert total_all >= 4
    assert any(t["status"] == "UNTRIAGED" for t in unfiltered)


def test_list_events_and_evidence_are_newest_and_ordered(store) -> None:
    assert store.upsert_task(_task("VOYN-W0-EV"))[0]
    task = store.get_task("VOYN-W0-EV")
    ok, reason, revision = store.transition("VOYN-W0-EV", "IN_PROGRESS", task["revision"])
    assert ok, reason

    events = store.list_events("VOYN-W0-EV")
    assert events[0]["event"] == "transition"  # newest first
    assert events[0]["detail"]["to"] == "IN_PROGRESS"
    assert any(e["reason"] == "inserted" for e in events)  # the upsert too

    assert store.record_evidence("VOYN-W0-EV", "pr", "https://github.com/o/r/pull/9")[0]
    assert store.record_evidence("VOYN-W0-EV", "sha", "cafef00d")[0]
    evidence = store.list_evidence("VOYN-W0-EV")
    assert [e["kind"] for e in evidence] == ["pr", "sha"]  # recorded_at order
    assert evidence[0]["value"] == "https://github.com/o/r/pull/9"


def test_list_events_for_unknown_task_is_an_empty_list_not_an_error(store) -> None:
    assert store.list_events("VOYN-W0-GHOST") == []
    assert store.list_evidence("VOYN-W0-GHOST") == []
