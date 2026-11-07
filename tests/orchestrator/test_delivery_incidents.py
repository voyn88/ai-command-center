import json
from unittest.mock import MagicMock

import pytest

from command_center.orchestrator.delivery_incidents import record_incident
from command_center.orchestrator.delivery_team import TeamResultError, incident_key


def observation(**changes):
    value = dict(task_id="VOYN-TASK-1", revision=3, repo="org/repo", run_ref="workflow:123",
                 sha="a" * 40, signature="test_contract:failure", kind="ci_failure",
                 reference="https://github.com/org/repo/actions/runs/123",
                 observed_at="2026-11-04T11:00:00Z", detail="Required contract check failed")
    value.update(changes)
    return value


def connection(result=True):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (result,)
    return conn


def test_observation_is_parameterized_exact_and_does_not_commit_or_schedule():
    conn = connection()
    args = observation()
    assert record_incident(conn, **args)
    cur = conn.cursor.return_value.__enter__.return_value
    sql, params = cur.execute.call_args.args
    assert "delivery_team_record_incident" in sql
    assert params[:3] == (args["task_id"], 3, incident_key(args["repo"], args["run_ref"], args["sha"], args["signature"]))
    evidence = json.loads(params[-1])
    assert evidence == {key: args[key] for key in ("kind", "reference", "observed_at", "detail")}
    assert "verified" not in evidence
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"revision": 0}, {"sha": None}, {"sha": "main"},
    {"kind": "pending"}, {"reference": "verified by model"},
    {"reference": "https://user:secret@example.test/log"},
    {"observed_at": "2026-11-04T11:00:00"}, {"observed_at": "yesterday"},
    {"detail": ""}, {"signature": "x" * 1025}, {"task_id": "other"},
])
def test_invalid_or_unreferenced_observation_has_no_database_effect(changes):
    conn = connection()
    with pytest.raises(TeamResultError):
        record_incident(conn, **observation(**changes))
    conn.cursor.assert_not_called()


def test_refusal_and_database_failure_are_not_claimed_persisted():
    conn = connection(False)
    assert record_incident(conn, **observation()) is False
    cur = conn.cursor.return_value.__enter__.return_value
    cur.execute.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        record_incident(conn, **observation())


@pytest.mark.parametrize("row", [None, (1,), (True, "unexpected")])
def test_malformed_admission_is_not_success(row):
    conn = connection()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = row
    with pytest.raises(TeamResultError):
        record_incident(conn, **observation())
