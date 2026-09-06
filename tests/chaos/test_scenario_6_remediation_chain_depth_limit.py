"""Chaos suite scenario 6/8: the remediation loop is bounded, not eternal.

Nothing bounded this chain in production before the fix: each rejected
remediation spawned another task, branch, pull request and full CI run, and
the live backlog reached 158 `-REM` tasks with chains nine links deep. Past
`MAX_REMEDIATION_DEPTH` consecutive rejections, the evidence is about the
task or the reviewer, not the implementation -- the chain stops through the
state machine itself (REJECTED, not a bespoke "give up" status) and the
task carries an explanation instead of spawning a further attempt.
"""
# ruff: noqa: RUF100

from __future__ import annotations

from command_center.orchestrator import review_merge
from tests.chaos.conftest import _chain


def test_a_rejection_below_the_limit_still_spawns_a_remediation(rig):  # noqa: F811
    app_factory, store, _worker = rig
    pr_url = "https://github.com/x/y/pull/107"
    head = "9" * 40
    ids = _chain(
        store, app_factory, "VOYN-W0-CHAOS6A",
        review_merge.MAX_REMEDIATION_DEPTH - 1, pr_url, head,
    )
    spawned = review_merge._remediate_rejection(
        app_factory, ids[-1], pr_url, head, "Still wrong.\nVERDICT: REJECT\n"
    )
    assert spawned == ids[-1] + "-REM"


def test_the_chain_stops_at_the_depth_limit(rig):  # noqa: F811
    app_factory, store, _worker = rig
    pr_url = "https://github.com/x/y/pull/108"
    head = "a" * 40
    ids = _chain(
        store, app_factory, "VOYN-W0-CHAOS6B",
        review_merge.MAX_REMEDIATION_DEPTH, pr_url, head,
    )
    last = ids[-1]

    assert review_merge._remediate_rejection(
        app_factory, last, pr_url, head, "Rejected again.\nVERDICT: REJECT\n"
    ) is None

    with app_factory() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM backlog_task WHERE task_id = %s", (last + "-REM",))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT status, body FROM backlog_task WHERE task_id = %s", (last,))
        status, body = cur.fetchone()
        assert status == "REJECTED"
        assert "Remediation chain stopped" in body
