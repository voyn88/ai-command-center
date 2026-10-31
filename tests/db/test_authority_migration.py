"""Authority discovery must coexist with current PR evidence validation."""
import pytest

from tests.db.test_backlog_planner import (  # noqa: F401
    _complete_latest,
    _dispatch,
    _task,
    _test_repo_routes,
    rig,
)

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


@pytest.mark.parametrize("pr_url,expected", [
    (None, "DEFER_TO_USER"),
    ("https://github.com/voyn88/ai-command-center/pull/1", "READY_TO_REVIEW"),
    ("not-a-pr", "DEFER_TO_USER"),
])
def test_discovered_authority_and_pr_validation(rig, pr_url, expected):  # noqa: F811
    factory, store, worker = rig
    task_id = "VOYN-W0-AUTH"
    assert store.upsert_task(_task(task_id, repo="repo-p1"))[0]
    assert _dispatch(factory, task_id)[0]
    _complete_latest(factory, worker, task_id, {
        "status": "completed", "required_authorities": ["root"], "pr_url": pr_url,
    })
    with factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
        assert len(cur.fetchall()) == 1
        cur.execute("SELECT status, required_authorities FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone() == (expected, ["root"])
        if expected == "DEFER_TO_USER":
            cur.execute("SELECT * FROM backlog_resume_deferred(%s)", (task_id,))
            verdict = cur.fetchone()
            assert not verdict[0]
            assert verdict[1] == "authority_park_needs_owner"
