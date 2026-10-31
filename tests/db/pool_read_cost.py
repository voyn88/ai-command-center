"""What the pool is worth on the PostgreSQL read path (VOYN-W0-AICC-SRV-09-READ-POOL).

`SRV09_READ_POOL_PREMISE_CHECK.md` closes the wiring half of that item: every
PostgreSQL read in this tree already takes its connection from
`command_center.db.pool`, and `tests/architecture/pool_routing.py` turns that
from a convention into a gate. What neither the survey nor the gate could say
is *how much it matters*. The item states a precondition — without the pool the
read cost is ~19.8x, and a threshold fails on "all four measured queries"
independent of correctness, so a correct read cutover would still be unusable —
and those numbers came from outside this checkout. A gate whose cost is
asserted somewhere else is a gate that gets argued away the first time it is
inconvenient.

This module measures it here, against a real PostgreSQL, through the code the
service actually runs.

**The seam is the store's own.** Every read repository in `command_center/db/`
takes an optional `connection_factory` and falls back to `pool.connection()`
when none is given (rule 3 of the routing gate). So the two policies compared
below differ in exactly one thing — where the connection comes from — while the
SQL, the parameter binding, the row decoding and the role are identical:

* ``pooled``   — `pool.connection()`, i.e. what production does.
* ``unpooled`` — a fresh `psycopg.connect()` per call, closed at block exit,
  i.e. precisely what rule 1 of the routing gate forbids. This is not a straw
  man: it is the shape a store acquires by being written from a driver example,
  which is the failure mode that gate was built for.

`tests/` is the one place in the repository allowed to open an unpooled
connection — the suites connect *as each role* to prove the grants, which is
why the gate excludes them. That exclusion is what lets the cost of an unpooled
connection be measured in the same repository that forbids one.

**Four reads, chosen as shapes rather than as favourites**, so that "all four
measured queries" means something checkable here:

1. `BacklogStore.list_tasks` — a filtered page plus its total: two statements
   on one checkout, the shape every list endpoint has.
2. `BacklogStore.get_task` — a single indexed row: the cheapest real read in
   the tree, and therefore the one where connection cost dominates hardest.
3. `WorkQueueReadStore.list_items` — an ordered, limited read over a view
   (`work_item_public`), the control plane's ordinary question.
4. `PostgresQueueMirror.list_entries` — an ordered full-table read, the
   heaviest of the four and so the *least* favourable to the pool.

**Reading the numbers.** Timings are medians over `MEASURED_CALLS` warmed
calls. They are a lower bound on the ratio, deliberately, for two reasons: the
measurement runs over loopback TCP, and TLS is off unless the supplied DSN asks
for it, so the unpooled side pays neither a network round trip nor a handshake
that a deployed replica pays on every single call. What the unpooled side does
pay, beyond TCP and SCRAM, is the loss of psycopg's per-connection prepared-
statement cache — that is a real cost of not pooling, not an artefact, but it
is why the ratio is not purely a connection-setup measurement.
"""

from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

__all__ = [
    "MEASURED_CALLS",
    "READ_CALLS",
    "ReadCost",
    "SEED_ROWS",
    "WARMUP_CALLS",
    "app_config",
    "format_report",
    "measure",
    "seed",
]

#: Rows seeded into each table the four reads touch. Large enough that a page
#: read is a page read rather than an empty scan; small enough that seeding is
#: not itself the slow part of the test.
SEED_ROWS = 100

#: Calls made and discarded before timing starts, per read and per policy. The
#: pool needs its `min_size` connections established, and psycopg prepares a
#: statement only after it has seen it a few times — measuring before either
#: has settled would compare two warm-up curves rather than two steady states.
WARMUP_CALLS = 15

#: Timed calls per read and per policy. The reported figure is the median, not
#: the mean: a single scheduler preemption on a shared CI box moves a mean and
#: does not move a median.
MEASURED_CALLS = 120

#: Task ids are constrained by `backlog_task_id_shape` to `^VOYN-[A-Za-z0-9]…`;
#: waves by `backlog_task_wave_shape` to a number or one of a closed set of
#: lane names. Seed rows are shaped to satisfy the real constraints rather than
#: being inserted past them, so the reads below run over rows the production
#: writer could have produced.
_TASK_ID = "VOYN-SRV09-BENCH-{:04d}"


@dataclass(frozen=True, slots=True)
class ReadCost:
    """One read, measured under both connection policies."""

    read: str
    unpooled_ms: float
    pooled_ms: float

    @property
    def ratio(self) -> float:
        return self.unpooled_ms / self.pooled_ms

    @property
    def connection_overhead_ms(self) -> float:
        """Milliseconds the unpooled policy adds to this read."""
        return self.unpooled_ms - self.pooled_ms


def _backlog_list(factory: Any) -> Any:
    from command_center.db.backlog_store import BacklogStore

    tasks, total = BacklogStore(connection_factory=factory).list_tasks(limit=50)
    return [task["task_id"] for task in tasks], total


def _backlog_get(factory: Any) -> Any:
    from command_center.db.backlog_store import BacklogStore

    return BacklogStore(connection_factory=factory).get_task(_TASK_ID.format(0))


def _queue_items(factory: Any) -> Any:
    from command_center.db.work_queue_read import WorkQueueReadStore

    items = WorkQueueReadStore(connection_factory=factory).list_items(limit=50)
    return [item["idempotency_key"] for item in items]


def _queue_entries(factory: Any) -> Any:
    from command_center.db.queue_store import PostgresQueueMirror

    entries = PostgresQueueMirror(connection_factory=factory).list_entries()
    return [entry["task_id"] for entry in entries]


#: Ordered so the report reads cheapest-shape-last; see the module docstring
#: for why these four and not others.
READ_CALLS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("backlog.list_tasks", _backlog_list),
    ("backlog.get_task", _backlog_get),
    ("work_queue.list_items", _queue_items),
    ("queue_mirror.list_entries", _queue_entries),
)


def app_config(app_dsn: str):
    """A `PostgresConfig` for `pool.open_pool()` built from an app-role DSN.

    Pool sizing is pinned here rather than left to `load_config()` defaults so
    the measurement does not silently change meaning when a deployment default
    is retuned. `min_size` is above one because a pool that has to open a
    connection on the first checkout would charge the pooled side exactly the
    cost being measured.
    """
    from psycopg.conninfo import conninfo_to_dict

    from command_center.db.config import PostgresConfig

    params = conninfo_to_dict(app_dsn)
    return PostgresConfig(
        host=str(params.get("host", "127.0.0.1")),
        port=int(params.get("port", 5432)),
        dbname=str(params["dbname"]),
        user=str(params["user"]),
        password=str(params.get("password", "")),
        sslmode=str(params.get("sslmode", "prefer")),
        sslrootcert=None,
        connect_timeout=5,
        application_name="srv09-read-cost",
        pool_min_size=2,
        pool_max_size=8,
        pool_timeout_seconds=5.0,
        statement_timeout_ms=30_000,
    )


def seed(app_connection, superuser_connection) -> None:
    """Populate the tables the four reads touch.

    Two connections because the grant matrix is real: `queue_enqueue()` is an
    `aicc_app` privilege, while `queue_entry` carries `_APP_DML` — SELECT,
    INSERT, UPDATE and deliberately no DELETE — so its rows are inserted
    directly rather than through `PostgresQueueMirror.replace_entries()`, whose
    whole-list replacement opens with a DELETE. Seeding through the role that
    actually owns each write keeps the seeded rows honest; the measurement
    itself then reads everything as `aicc_app`, which is the role a served
    request runs under.
    """
    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore
    from command_center.db.work_queue_store import WorkQueueStore

    @contextmanager
    def app_factory() -> Iterator[Any]:
        yield app_connection

    backlog = BacklogStore(connection_factory=app_factory)
    queue = WorkQueueStore(connection_factory=app_factory)
    for index in range(SEED_ROWS):
        task_id = _TASK_ID.format(index)
        accepted, reason, _ = backlog.upsert_task(
            ParsedTask(
                task_id=task_id,
                wave="0",
                priority="P1",
                status="OPEN",
                kind="task",
                title=f"read-cost bench row {index}",
                body="body " * 40,
                repo="aicc",
                line_no=index,
            )
        )
        if not accepted:  # pragma: no cover — a seed refusal is a broken bench
            raise AssertionError(f"seeding backlog_task refused: {reason}")
        queue.enqueue(
            "default",
            idempotency_key=f"srv09-bench-{index}",
            payload={"index": index},
            task_id=task_id,
        )

    with superuser_connection.cursor() as cur:
        cur.executemany(
            "INSERT INTO queue_entry (id, task_id, project, state, reason, run_id,"
            " added_at, evaluated_at, launched_at, position)"
            " VALUES (%s, %s, %s, %s, %s, %s, now(), NULL, NULL, %s)",
            [
                (index, _TASK_ID.format(index), "aicc", "queued", "", None, index)
                for index in range(SEED_ROWS)
            ],
        )


def _median_ms(call: Callable[[Any], Any], factory: Any) -> tuple[float, Any]:
    for _ in range(WARMUP_CALLS):
        result = call(factory)
    samples = []
    for _ in range(MEASURED_CALLS):
        started = time.perf_counter()
        result = call(factory)
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), result


def measure(unpooled_factory: Any, pooled_factory: Any) -> list[ReadCost]:
    """Time every read under both policies, newest measurement per read.

    Each read's two policies are compared for equality as well as for time. A
    read that raised, or that quietly returned nothing because the seed did not
    land, would otherwise look extremely fast under whichever policy broke it —
    the one way a benchmark can report a large ratio while measuring nothing.
    """
    costs = []
    for name, call in READ_CALLS:
        unpooled_ms, unpooled_result = _median_ms(call, unpooled_factory)
        pooled_ms, pooled_result = _median_ms(call, pooled_factory)
        if unpooled_result != pooled_result:  # pragma: no cover — see docstring
            raise AssertionError(
                f"{name}: the two connection policies did not read the same rows"
            )
        if not unpooled_result:  # pragma: no cover — see docstring
            raise AssertionError(
                f"{name}: read returned nothing; the seed did not land"
            )
        costs.append(ReadCost(read=name, unpooled_ms=unpooled_ms, pooled_ms=pooled_ms))
    return costs


def format_report(costs: list[ReadCost]) -> str:
    """A table an operator can paste into the item, units included."""
    width = max(len(cost.read) for cost in costs)
    lines = [
        f"{'read':<{width}}  {'unpooled ms':>11}  {'pooled ms':>9}  "
        f"{'+ms/call':>8}  {'ratio':>6}",
        "-" * (width + 42),
    ]
    for cost in costs:
        lines.append(
            f"{cost.read:<{width}}  {cost.unpooled_ms:11.3f}  {cost.pooled_ms:9.3f}  "
            f"{cost.connection_overhead_ms:8.3f}  {cost.ratio:5.1f}x"
        )
    lines.append(
        f"medians of {MEASURED_CALLS} calls after {WARMUP_CALLS} warm-up calls, "
        f"{SEED_ROWS} seeded rows per table"
    )
    return "\n".join(lines)
