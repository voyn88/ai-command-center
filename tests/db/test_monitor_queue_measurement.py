"""`control-01:queue`'s own measurement, taken from a real database.

The monitor is the authority this task is stated in: it opens a pool, runs ONE
query over `work_item` (`infra_monitor.read_queue_snapshot`), and
`evaluate()` turns the counts into `dead_letter_growth:<n>>0`. Everything that
pinned that translation built `QueueSnapshot` by hand
(tests/ops/test_infra_monitor.py, which has no database at all), so the query
itself — the only half that touches the schema the queue fix changed, and the
half that decides whether the finding clears — was pinned by nothing. A column
rename, a state renamed, a `dead_reason` the quota regex stops matching: each
would leave a monitor reporting a cheerful `recent_dead=0` about a queue that
was emptying itself into the dead letter, and every test in both suites green.

So this module drives the real queue functions and reads the real query. The
outage that produced monitor_finding #3396 goes in at one end; what
`control-01` would have measured comes out of the other
(VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH).

Skipped wholesale unless `AICC_TEST_PG_ADMIN_DSN` is set — see `conftest`.
"""

from __future__ import annotations

import json
import secrets

import pytest

from command_center.ops import infra_monitor

# The protocol's own call shapes, imported rather than re-typed: a monitor test
# that enqueued and claimed through its own private SQL would go on passing
# after the protocol moved, which is the drift it exists to catch.
from tests.db.test_queue_claim import QUEUE, _call, _claim, _enqueue, _token

#: A quota refusal exactly as `worker.handlers` reports one, and exactly the
#: text `orchestrator.routing` recorded 142 times on 2026-08-23. It reaches
#: `dead_reason` prefixed with `lease_wait_exhausted:`, which is where
#: `read_queue_snapshot`'s quota regex has to find it.
SESSION_LIMIT = (
    "executor infrastructure failure (provider/auth/quota): "
    "You've hit your session limit"
)

#: One lane, up. The monitor's worker half is not under test here; these tests
#: are about the queue half, so the worker half is given nothing to report.
ONE_ACTIVE_LANE = {"voyn-aicc-worker@1.service": "active"}


@pytest.fixture
def monitor_reads(pg_connection_factory, monkeypatch):
    """`read_queue_snapshot()` pointed at this test's migrated database.

    `read_queue_snapshot` imports `pool` and `load_config` inside its body and
    opens/closes the process-wide pool around one `pool.connection()` block.
    Replacing those three names is therefore the whole seam — the SQL, the
    cursor and the unpacking below it stay exactly the code that runs on
    control-01, which is the only reason this test is worth having.
    """
    from command_center.db import config as db_config
    from command_center.db import pool

    monkeypatch.setattr(pool, "open_pool", lambda *args, **kwargs: None)
    monkeypatch.setattr(pool, "close_pool", lambda *args, **kwargs: None)
    monkeypatch.setattr(pool, "connection", pg_connection_factory)
    # Never reached (the patched `open_pool` ignores it), but it is loaded
    # before the call and would raise `ConfigError` on a host with no DSN.
    monkeypatch.setattr(db_config, "load_config", lambda: None)
    with pg_connection_factory() as conn:
        yield conn


def _report(snapshot: infra_monitor.QueueSnapshot) -> infra_monitor.MonitorReport:
    """The monitor's verdict on a snapshot, with control-01's own thresholds."""
    return infra_monitor.evaluate(
        ONE_ACTIVE_LANE,
        snapshot,
        minimum_active_workers=1,
        max_stalled_seconds=900,
        prometheus_ready=True,
    )


def _failure_classes(report: infra_monitor.MonitorReport) -> set[str]:
    """The failure CODES, which is the identity a finding is opened under."""
    return {infra_monitor.finding_key(failure) for failure in report.failures}


def _refuse(conn, item_id: str, *, reason: str = SESSION_LIMIT) -> str:
    """One full delivery that the fleet refuses with no fault in the work.

    Claim, then `queue_fail_lease_wait` — the pair a worker performs when
    `HandlerOutcome.no_fault` comes back. Returns the verdict's reason so the
    caller can assert which branch the database took.

    An item that is no longer deliverable is reported, not raised on: it has
    left the ready pool, which for these tests is a measurement rather than a
    broken fixture, and a loop that stopped here would fail with a claim error
    instead of with the count the monitor took.
    """
    token, token_hash = _token()
    verdict = _claim(conn, token_hash)
    if not (verdict[0] and verdict[2] == item_id):
        return f"not_delivered: {verdict[1] or verdict[2]}"
    ok, got = _call(
        conn,
        "SELECT ok, reason FROM queue_fail_lease_wait(%s, %s, %s, %s)",
        (verdict[3], token, reason, 20),
    )
    assert ok, got
    return got


def _serve_a_sibling(conn) -> str:
    """One delivery that reached the work and completed.

    `_queue_fleet_is_serving` (0028) asks for exactly this before it will let
    an exhausted wait budget become a dead letter, and `read_queue_snapshot`
    asks the same question as `recent_succeeded`. `priority` puts the sibling
    first so the claim inside here cannot take the item under test.
    """
    sibling = _enqueue(
        conn, f"served-{secrets.token_hex(4)}", max_attempts=1, priority=50
    )
    token, token_hash = _token()
    verdict = _claim(conn, token_hash)
    assert verdict[0] and verdict[2] == sibling, verdict[1]
    assert _call(
        conn,
        "SELECT ok FROM queue_complete(%s, %s, %s::jsonb)",
        (verdict[3], token, json.dumps({"served": True})),
    ) == (True,)
    return sibling


def test_an_outage_that_refuses_every_delivery_measures_no_dead_letter_growth(
    monitor_reads,
):
    """The acceptance property, end to end: queue functions in, monitor out.

    Twenty-five no-fault refusals — five more than the wait budget — with
    nothing else getting through. Before 0028 the twenty-first dead-lettered
    the item, and `control-01:queue` measured the arrivals as
    `dead_letter_growth`; the fleet's dominant outage (a five-hour rolling
    subscription cap) outlasts the budget's 68.5-minute horizon by four times,
    so an outage emptied the backlog into the DLQ a monitor tick at a time.

    What the monitor must see instead is asserted in both directions: the
    dead-letter class is absent, AND the outage is still visible — as
    `throughput_stalled`, the class that names a fleet which has stopped
    serving. A stall keeps the work and names the fleet; a dead letter
    destroys the work and blames the item. Silence would be the third,
    wrong answer, so it is excluded here rather than left to inference.
    """
    item_id = _enqueue(monitor_reads, "outage", max_attempts=2)

    # Every delivery first, then the measurement: the property under test is
    # what `control-01` reads afterwards, so that is what a regression here
    # must fail on. Asserting each refusal inside the loop would fail on
    # delivery 21 with a verdict string, one layer short of the monitor.
    verdicts = {_refuse(monitor_reads, item_id) for _ in range(25)}

    snapshot = infra_monitor.read_queue_snapshot()
    assert snapshot.recent_dead == 0, f"an outage is not a verdict on the work: {verdicts}"
    assert snapshot.recent_quota_dead == 0
    assert (snapshot.ready, snapshot.claimed, snapshot.dead) == (1, 0, 0)
    assert verdicts == {"lease_wait_requeued"}

    report = _report(snapshot)
    assert "dead_letter_growth" not in _failure_classes(report)
    assert "throughput_stalled" in _failure_classes(report), (
        "the outage must still surface -- as the fleet's failure, not the item's"
    )


def test_an_item_the_fleet_singled_out_still_reaches_the_monitor(monitor_reads):
    """The measurement is live, not vacuously zero.

    The same refusal text, the same budget, one served sibling later: now the
    item was singled out, the dead letter is the right answer, and the monitor
    has to say so. This is what keeps the test above from passing for the
    uninteresting reason that nothing can ever reach `recent_dead`.

    It also pins both classifications against a `dead_reason` the DATABASE
    composed rather than one written here. `queue_fail_lease_wait` prefixes
    the handler's text with `lease_wait_exhausted: `, and
    `read_queue_snapshot`'s quota regex has to go on matching through that
    prefix -- otherwise a capacity outage that outlasted everything reaches
    the operator as a bare queue defect instead of as
    `executor_quota_exhausted`, the class routing and budgets are supposed to
    get a task from. (Which alternative of that regex fires is not pinned:
    this reason matches on `quota` as well as on `session limit`.)
    """
    item_id = _enqueue(monitor_reads, "singled-out", max_attempts=2)
    for delivery in range(1, 21):
        assert _refuse(monitor_reads, item_id) == "lease_wait_requeued", delivery

    _serve_a_sibling(monitor_reads)
    assert _refuse(monitor_reads, item_id) == "lease_wait_dead_lettered"

    snapshot = infra_monitor.read_queue_snapshot()
    assert snapshot.recent_dead == 1
    assert snapshot.recent_quota_dead == 1, (
        "the refusal text travels into `dead_reason`, and the monitor buckets "
        "the DLQ row by it"
    )
    assert snapshot.recent_succeeded == 1

    report = _report(snapshot)
    assert not report.ok
    assert _failure_classes(report) >= {
        "dead_letter_growth",
        "executor_quota_exhausted",
    }
    assert "dead_letter_growth:1>0" in report.failures


def test_the_snapshot_counts_only_the_trailing_hour(monitor_reads):
    """Why the finding can clear at all.

    `recent_dead` is a WINDOW (`updated_at > now() - interval '1 hour'`), not
    the DLQ's depth: control-01 is holding dead letters the old classification
    produced, and the monitor clears when dead-lettering STOPS, with no
    operator redrive in between. An implementation that counted `dead` rows
    outright would hold the finding open forever and no fix could ever close
    it -- so the distinction is asserted, using the same two columns the query
    reads.
    """
    item_id = _enqueue(monitor_reads, "already-dead", max_attempts=1)
    token, token_hash = _token()
    verdict = _claim(monitor_reads, token_hash)
    assert verdict[0] and verdict[2] == item_id, verdict[1]
    assert _call(
        monitor_reads,
        "SELECT ok, reason FROM queue_fail(%s, %s, %s, %s)",
        (verdict[3], token, "agent produced no commits", False),
    ) == (True, "dead_lettered")

    assert infra_monitor.read_queue_snapshot().recent_dead == 1

    # Age that row out of the window without touching anything else. The
    # queue's own functions cannot do this (nothing rewrites `updated_at`
    # backwards), and waiting an hour is not a test.
    with monitor_reads.cursor() as cur:
        cur.execute(
            "UPDATE work_item SET updated_at = now() - interval '2 hours' "
            "WHERE work_item_id = %s AND queue = %s",
            (item_id, QUEUE),
        )
        assert cur.rowcount == 1

    aged = infra_monitor.read_queue_snapshot()
    assert aged.dead == 1, "the dead letter is still there"
    assert aged.recent_dead == 0, "but it is no longer growth"
    assert _report(aged).ok, (
        "a queue that stopped dead-lettering is healthy, so the finding clears "
        "without an operator redriving what the old classification produced"
    )
