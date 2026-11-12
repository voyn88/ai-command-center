"""Numbered foundation/CTO migrations compose through actual role dispatch."""
import json

import pytest

from tests.db.test_cto_assignment import TASK, activate, assign, claim, start
from tests.db.test_delivery_team import payload

pytest_plugins = ("tests.db.test_cto_assignment",)
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


@pytest.mark.parametrize("authorized", [False, True])
def test_team_admission_preserves_stage_on_cto_refusal(cto_rig, authorized):
    app, _, _, _ = cto_rig
    app.execute("SELECT * FROM delivery_team_sync(100)")
    candidate = next(row[0] for row in app.execute(
        "SELECT * FROM delivery_team_candidates(100)"
    ).fetchall() if row[0]["task_id"] == TASK)
    body = payload(candidate)
    before = app.execute(
        "SELECT status,work_item_id,dispatch_count FROM delivery_team_stage "
        "WHERE task_id=%s AND stage=%s", (TASK, candidate["stage"])
    ).fetchone()
    if authorized:
        assign(cto_rig, body)
    activate(cto_rig)
    result = app.execute(
        "SELECT * FROM delivery_team_dispatch(%s,%s,%s,%s,%s::jsonb,2)",
        (TASK, candidate["revision"], candidate["stage"],
         candidate["input_digest"], json.dumps(body)),
    ).fetchone()
    after = app.execute(
        "SELECT status,work_item_id,dispatch_count FROM delivery_team_stage "
        "WHERE task_id=%s AND stage=%s", (TASK, candidate["stage"])
    ).fetchone()
    if authorized:
        assert result[0] and result[2]
        assert after == ("queued", result[2], before[2] + 1)
        work = claim(cto_rig)
        assert work[0] and work[2] == result[2]
        assert start(cto_rig, work[3])[0]
    else:
        assert not result[0] and result[2] is None
        assert after == before
        assert app.execute("SELECT count(*) FROM work_item").fetchone() == (0,)
        assert app.execute(
            "SELECT count(*) FROM cto_assignment_event WHERE action='enqueue' AND reason='assignment_required'"
        ).fetchone()[0] >= 1
