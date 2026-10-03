"""What the pool is worth on the PostgreSQL read path, measured here.

`SRV09_READ_POOL_PREMISE_CHECK.md` closed the wiring half of
VOYN-W0-AICC-SRV-09-READ-POOL — every PostgreSQL read in this tree already
takes its connection from `command_center.db.pool`, and
`tests/architecture/pool_routing.py` keeps it that way — but left the item's
*precondition* unchecked: "without the pool the ratio is ~19.8x, and the
threshold fails on all four measured queries independent of correctness". Those
figures came from outside this checkout and appear nowhere in it. A gate whose
cost lives in someone else's spreadsheet is a gate that gets argued away the
first time it is inconvenient; this test moves the number into the repository
that enforces the rule.

`tests/db/pool_read_cost.py` holds the measurement itself and documents the
choices — which four reads, why medians, why the ratio is a lower bound. This
module is the part pytest runs: provision a real database, seed it, and time
the four reads under the two connection policies.

Deliberately `serial`: the reads are timed, and a measurement sharing its box
with `-n auto` workers measures the other workers. The serial partition runs
with `-p no:xdist` (see `.github/workflows/ci.yml`), which is the only place in
this suite where a timing comparison means anything.

**The pooled side passes no factory at all.** Every read store treats
`connection_factory=None` as "use `pool.connection()`" — rule 3 of the routing
gate — so the pooled measurement exercises the production default rather than a
test-supplied stand-in of it. The unpooled side connects with the *same*
conninfo the pool was built from, so the two policies differ in where the
connection comes from and in nothing else: same role, same session settings,
same autocommit, same SQL.

The assertions are floors, not the measurement. The measured figures belong in
the item and in the premise-check document; what fails here is the *claim* —
that pooling is worth having on a read path — and it fails only if the cost of
a connection has collapsed to near nothing, which on a deployed replica (TLS,
a real network) it cannot. See `MIN_RATIO` for why the floors are where they
are.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.db import roles
from tests.db import pool_read_cost

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

#: Ratio every one of the four reads must show. The item cites ~19.8x, and the
#: first run of this test landed in the same family (17.6x-38.7x; the figures
#: and the box they came from are in `SRV09_READ_POOL_PREMISE_CHECK.md`) — but
#: pinning a measured ratio would be pinning that box's connect cost, which
#: varies by an order of magnitude between a container on loopback and a
#: replica behind TLS. What is asserted is the shape of the claim — connecting
#: costs multiples of reading — with enough headroom that a slow CI box moves
#: the numbers without moving the verdict.
MIN_RATIO = 3.0

#: Milliseconds the unpooled policy must add per call. A TCP connect plus a
#: SCRAM exchange plus a backend fork cannot come in under this, and a run that
#: claims it did is measuring something other than a new connection — a pooler
#: in front of the server, or a `connect()` that quietly reused a session.
MIN_OVERHEAD_MS = 0.25

#: How far the per-call overhead may vary across the four reads before it is
#: tracking the query rather than the connection. Loose on purpose: the reads
#: ask for different amounts of data, and an unpooled connection also loses
#: psycopg's prepared-statement cache, so their overheads are near each other
#: rather than identical (1.15x apart when first measured).
MAX_OVERHEAD_SPREAD = 8.0

#: The four reads named in the item's "all four measured queries". Pinned here
#: so that widening or narrowing the measurement is a visible edit to a test
#: rather than a silently smaller claim.
EXPECTED_READS = (
    "backlog.list_tasks",
    "backlog.get_task",
    "work_queue.list_items",
    "queue_mirror.list_entries",
)


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    """Bootstrap as superuser, migrate as the migrator — the production order."""
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@pytest.fixture
def costs(admin_conn, psycopg, test_dsn, role_passwords):
    """The four reads, timed under both policies against a real server."""
    from command_center.db import pool

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    config = pool_read_cost.app_config(app_dsn)
    conninfo = config.conninfo()

    with psycopg.connect(conninfo, autocommit=True) as app_conn:
        pool_read_cost.seed(app_conn, admin_conn)

    @contextmanager
    def unpooled():
        with psycopg.connect(conninfo, autocommit=True) as conn:
            yield conn

    # Not "open if not already open": `open_pool()` is idempotent and returns
    # whatever pool a previous test in this process left behind, which would
    # point the pooled side at another database entirely.
    pool.close_pool()
    pool.open_pool(config)
    try:
        yield pool_read_cost.measure(unpooled, None)
    finally:
        pool.close_pool()


def test_every_measured_read_costs_multiples_more_without_the_pool(costs) -> None:
    """The item's precondition, checked against this tree instead of cited.

    All four reads, because "independent of correctness" is the part that
    matters: a read cutover that returns exactly the right rows is still
    unusable at this cost, so the ratio has to hold for the cheap reads and the
    expensive one alike.
    """
    report = pool_read_cost.format_report(costs)
    assert tuple(cost.read for cost in costs) == EXPECTED_READS

    too_cheap = [c for c in costs if c.ratio < MIN_RATIO]
    assert not too_cheap, (
        f"pooling stopped paying for {[c.read for c in too_cheap]} "
        f"(floor {MIN_RATIO}x)\n{report}"
    )

    free_connections = [c for c in costs if c.connection_overhead_ms < MIN_OVERHEAD_MS]
    assert not free_connections, (
        f"a new connection appears to cost nothing for "
        f"{[c.read for c in free_connections]} (floor {MIN_OVERHEAD_MS} ms/call); "
        f"the unpooled side is probably not connecting\n{report}"
    )

    print(f"\n{report}")


def test_the_penalty_is_per_connection_not_per_query(costs) -> None:
    """Why the ratio is largest on the cheapest read, stated as a property.

    The unpooled policy adds roughly the same wall-clock to every read, because
    what it adds is a connection — the same TCP handshake, SCRAM exchange and
    backend fork regardless of what the query then does. That is the fact that
    makes a single measured ratio transferable: it predicts that the cheaper the
    read, the worse the ratio, which is why a read cutover cannot be rescued by
    making the queries faster.

    Bounded loosely (`MAX_OVERHEAD_SPREAD`), because what is being asserted is
    that the overheads cluster while the ratios do not.
    """
    report = pool_read_cost.format_report(costs)
    overheads = {cost.read: cost.connection_overhead_ms for cost in costs}
    spread = max(overheads.values()) / min(overheads.values())
    assert spread <= MAX_OVERHEAD_SPREAD, (
        f"per-call overhead varies {spread:.1f}x across the four reads, so it is "
        f"tracking the query rather than the connection\n{report}"
    )

    cheapest = min(costs, key=lambda cost: cost.pooled_ms)
    heaviest = max(costs, key=lambda cost: cost.pooled_ms)
    assert cheapest.ratio > heaviest.ratio, (
        "the cheapest read did not show the worst ratio, which is what a "
        f"per-connection penalty implies\n{report}"
    )
