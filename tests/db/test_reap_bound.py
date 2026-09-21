"""The reaper's batch bound, checked without a database.

`test_work_queue_admin.py` proves `WorkQueueAdmin.reap` against a real server
as `aicc_app`: that it loops until a batch comes back short, that a bounded
call commits its batch instead of holding it hostage to the rest, that only a
missing `queue_reap(integer)` may be answered with the unbounded arity. Those
are proofs about the LOOP, and every one of them sizes its backlog as
`WorkQueueAdmin.REAP_BATCH + 3` -- they read the constant and follow it
wherever it goes.

So the one thing none of them can see is THE CONSTANT CEASING TO BE A BOUND.
`REAP_BATCH = 10**9` was live on this branch and contradicted by nothing the
suite could express: the batching tests do not fail at that value, they stop
being runnable -- a backlog of a billion claims is not a red test, it is a
gate that hangs -- while production reverts to exactly the all-or-nothing reap
0028 removed. 0028's fix is not the loop; it is the loop AND a bound small
enough to make the loop mean something, and only half of it was pinned.

MEASURED against a real PostgreSQL 16 server, 1000 lapsed leases, the tick
cancelled 100ms in -- `aicc-queue-reaper.service` is a `Type=oneshot` whose
`TimeoutStartSec=60s` kill arrives exactly this way, as does the restart of
the tunnel the credential rotation cycles:

    REAP_BATCH = 100    tick killed after 104.8 ms -> recovered 600 of 1000
    REAP_BATCH = 10**9  tick killed after 105.5 ms -> recovered   0 of 1000

Same server, same work, same interruption: the bounded tick banked six
batches, the unbounded one rolled back everything it had done. A reaper that
recovers 0 is not a slow reaper, it is `lapsed_claim_age_seconds` climbing
without limit -- the one starvation class `infra_monitor.evaluate` neither
excuses by spare capacity nor bounds by the fleet clock, and the one with no
exit reachable by fleet action: restarting a lane does not reap, and
`queue_redrive` only reaches items that are already `dead`. That is
`queue_stalled` on `control-01:queue`.

These tests need no server and so run on every machine and in every gate,
which is the point -- the regression they pin reached HEAD through a suite
whose only witnesses to it were tests that cannot be collected without one.
Sibling in spirit to `test_roles_render.py` beside `test_role_privileges.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from command_center.db.work_queue_admin import WorkQueueAdmin

REPO_ROOT = Path(__file__).resolve().parents[2]
REAPER_UNIT = REPO_ROOT / "deploy/systemd/aicc-queue-reaper.service"

#: Seconds of server time one expiration costs, rounded UP from measurement.
#: 200 expirations and their audit rows committed in 41.2 ms on PostgreSQL 16
#: -- 0.206 ms each -- over a local socket against a small table. Production's
#: reaper crosses `voyn-aicc-pgtunnel.service` to a loaded server, so the
#: figure used here is ~5x that, and the ceiling it yields is correspondingly
#: forgiving: this test is a guard against a bound that has stopped bounding,
#: not a performance budget.
EXPIRY_COST_SECONDS = 0.001

#: The share of the tick's kill budget one batch may spend. A batch is one
#: transaction, so this is precisely what an interruption costs: at a tenth,
#: a killed tick loses at most a tenth of a minute's recovery and the next
#: tick (`OnUnitActiveSec=1min`) starts from where the last batch committed.
BATCH_SHARE_OF_TICK = 0.1


def _timeout_start_seconds() -> float:
    """`TimeoutStartSec` from the reaper unit -- the hard kill a batch must
    fit inside. Read from the unit rather than restated here so the ceiling
    follows the unit if the tick's budget is ever retuned."""
    match = re.search(
        r"^TimeoutStartSec=(\d+)s\s*$", REAPER_UNIT.read_text(), re.MULTILINE
    )
    assert match is not None, f"no TimeoutStartSec in {REAPER_UNIT}"
    return float(match.group(1))


def test_the_reap_batch_is_a_bound_a_killed_tick_can_afford() -> None:
    """One batch must be a small fraction of the tick, because one batch is
    what an interruption throws away.

    This is the half of 0028 the loop cannot defend. `reap()` commits batch
    after batch, so the cost of the `TimeoutStartSec=60s` kill is the batch in
    flight -- but only while a batch is a fraction of the tick. Raise the
    bound past the tick and the loop makes exactly one call, that call is the
    whole backlog, and the kill takes all of it: measured, 0 of 1000 recovered
    where the bounded tick banked 600.
    """
    ceiling = int(BATCH_SHARE_OF_TICK * _timeout_start_seconds() / EXPIRY_COST_SECONDS)
    assert WorkQueueAdmin.REAP_BATCH <= ceiling, (
        f"REAP_BATCH={WorkQueueAdmin.REAP_BATCH} is not a bound a killed tick "
        f"can afford: at {EXPIRY_COST_SECONDS * 1000:g} ms per expiration one "
        f"batch is {WorkQueueAdmin.REAP_BATCH * EXPIRY_COST_SECONDS:g}s of a "
        f"{_timeout_start_seconds():g}s tick, and an interrupted reap commits "
        f"none of it -- which is the unbounded reap 0028 removed"
    )


def test_the_reap_batch_is_a_bound_the_loop_can_terminate_on() -> None:
    """And it must be positive, or the reaper never returns at all.

    `queue_reap` clamps its own argument -- `greatest(p_max_items, 1)`, 0028's
    line 118 -- while `reap()`'s termination test compares the count against
    the bound it ASKED for. At `REAP_BATCH <= 0` those two disagree for ever:
    the server reaps one item and answers 1, `1 < 0` is false, and the loop
    goes round; the queue empties, the server answers 0, `0 < 0` is false, and
    the loop goes round again. Reproduced in-process -- still looping after 51
    calls against an empty queue.

    Same symptom as the ceiling and a worse shape: the tick spins until
    systemd kills it, recovers nothing, and does it again every minute.
    """
    assert WorkQueueAdmin.REAP_BATCH >= 1, (
        f"REAP_BATCH={WorkQueueAdmin.REAP_BATCH} never satisfies "
        "`reaped < REAP_BATCH` -- the server clamps the bound up to 1 and "
        "`reap()` loops for ever, including against an empty queue"
    )
