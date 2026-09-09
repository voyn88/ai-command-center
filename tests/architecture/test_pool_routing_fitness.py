"""Architecture fitness gates for the PostgreSQL pool (VOYN-W0-AICC-SRV-09-READ-POOL).

The backlog item asked to "wire `command_center/db/pool.py` onto the PG read
path", citing a ~19.8x connect-per-query cost ratio. Investigation
(`docs/operations/SRV09_READ_POOL_PREMISE_CHECK.md`) found the wiring already
done everywhere a PostgreSQL read exists, and the one place the item named —
`command_center/runtime/` — reading SQLite by design, under two guard tests that
fail if a PostgreSQL read appears there.

So there was no connection left to move. What there was, was an unenforced
invariant: every PostgreSQL connection in this service comes from the process
pool, and nothing checked it. These gates check it. The cost the item was
worried about is real; the way it gets reintroduced is not a store that forgot
to use the pool, it is the *next* store, written by copying a driver example.

The scanner and its signatures live in `tests/architecture/pool_routing.py`.
"""

from __future__ import annotations

import ast

from tests.architecture import pool_routing as routing


def _parse(path) -> ast.AST:
    rel = path.relative_to(routing.REPO_ROOT).as_posix()
    return ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def test_every_postgres_connection_is_taken_from_the_process_pool() -> None:
    """No file under `command_center/` opens its own PostgreSQL connection.

    This is the gate the backlog item's cost argument actually needs. A pool
    that most callers use is not a pool: one module calling `psycopg.connect()`
    per request restores the fork-per-query cost for its own traffic, and does
    it invisibly, because `pool_stats()` only counts what went through the pool.
    """
    violations: list[str] = []
    for path in routing.iter_scanned_files():
        rel = path.relative_to(routing.REPO_ROOT).as_posix()
        for lineno, description in routing.find_unpooled_connections(_parse(path), rel):
            violations.append(f"{rel}:{lineno}: {description}")
    assert not violations, (
        "PostgreSQL connections must be taken from the process pool "
        "(`command_center.db.pool.connection()`), or injected as a "
        "`connection_factory`. PostgreSQL forks a backend per connection; "
        "connect-per-query is the cost VOYN-W0-AICC-SRV-09-READ-POOL exists to "
        "keep out. If a bypass is genuinely unavoidable, add it to "
        "`pool_routing.UNPOOLED_CONNECT_ALLOWED` with its reason.\n"
        + "\n".join(violations)
    )


def test_no_second_pool_is_built_outside_the_pool_module() -> None:
    """`pool.py`'s singleton is the whole connection budget for the process.

    A second pool is worse than a bare connection: it holds `min_size` backends
    open forever, `close_pool()` does not close it, and a credential rotation
    that `replace_pool()` completes leaves it authenticating with the revoked
    password until something notices.
    """
    violations: list[str] = []
    for path in routing.iter_scanned_files():
        rel = path.relative_to(routing.REPO_ROOT).as_posix()
        for lineno, description in routing.find_second_pools(_parse(path), rel):
            violations.append(f"{rel}:{lineno}: {description}")
    assert not violations, (
        f"Only {routing.POOL_MODULE} may build a pool; everything else opens it "
        "via `pool.open_pool()` at startup and reaches it via "
        "`pool.connection()`.\n" + "\n".join(violations)
    )


def test_every_injectable_store_keeps_the_pool_as_its_fallback() -> None:
    """The `connection_factory` seam is for tests; production is the default.

    Every store in `command_center/db` takes an optional factory so a test can
    supply a connection without a process-wide pool. That parameter is also the
    quiet way to lose the pool: a store whose default stops resolving
    `pool.connection()` still passes every test that injects a factory — which
    is all of them — and fails only in production, as a connection error rather
    than as a routing mistake.
    """
    stores: dict[str, list[str]] = {}
    unrouted: list[str] = []
    for path in routing.iter_scanned_files():
        rel = path.relative_to(routing.REPO_ROOT).as_posix()
        tree = _parse(path)
        declared = routing.injectable_stores(tree)
        if not declared:
            continue
        stores[rel] = declared
        if not routing.resolves_the_pool_fallback(tree):
            unrouted.append(f"{rel}: {', '.join(declared)}")

    assert stores, (
        "no injectable store found at all — the scanner stopped recognising the "
        "`connection_factory=None` constructor shape, and this gate would pass "
        "vacuously."
    )
    assert not unrouted, (
        "an injectable store must fall back to `pool.connection()` when no "
        "factory is injected:\n" + "\n".join(unrouted)
    )


def test_the_only_unpooled_connection_is_the_credential_probe() -> None:
    """The exemption list is pinned, so widening it is a reviewed edit.

    An allow-list that a future change can append to silently is not a gate. The
    rotation probe qualifies because it must authenticate as a credential the
    pool is not built from — a reason that does not generalise to a second entry
    without someone arguing it here.
    """
    assert set(routing.UNPOOLED_CONNECT_ALLOWED) == {
        "command_center/ops/credential_rotation.py"
    }
    reason = routing.UNPOOLED_CONNECT_ALLOWED["command_center/ops/credential_rotation.py"]
    assert "candidate" in reason and "per rotation" in reason

    # The exemption is a file the scanner really does reach, not a stale path
    # left behind by a rename: a typo'd key would exempt nothing and this suite
    # would still be green.
    scanned = {
        path.relative_to(routing.REPO_ROOT).as_posix()
        for path in routing.iter_scanned_files()
    }
    assert set(routing.UNPOOLED_CONNECT_ALLOWED) <= scanned

    # ...and it is exempt because it is listed, not because it stopped
    # connecting: drop the exemption and the file must be a violation again.
    path = routing.REPO_ROOT / "command_center/ops/credential_rotation.py"
    assert routing.find_unpooled_connections(_parse(path), "command_center/other.py")


def test_scanner_semantics_are_stable() -> None:
    """The gate itself must not rot: drivers caught, ordinary `.connect` ignored."""
    plain = ast.parse("import psycopg\ndef go(dsn):\n    return psycopg.connect(dsn)\n")
    assert routing.find_unpooled_connections(plain, "command_center/x.py")

    # Aliasing is not a hiding place.
    aliased = ast.parse("import psycopg as pg\ndef go(dsn):\n    return pg.connect(dsn)\n")
    assert routing.find_unpooled_connections(aliased, "command_center/x.py")

    # Nor is importing the function directly, with or without a rename.
    direct = ast.parse("from psycopg import connect\ndef go(dsn):\n    return connect(dsn)\n")
    assert routing.find_unpooled_connections(direct, "command_center/x.py")
    renamed = ast.parse(
        "from psycopg import connect as _open\ndef go(dsn):\n    return _open(dsn)\n"
    )
    assert routing.find_unpooled_connections(renamed, "command_center/x.py")

    # Nor `importlib`, which is how the stores already import the pool lazily
    # and therefore the most available disguise in this codebase.
    dynamic = ast.parse(
        "import importlib\n"
        "driver = importlib.import_module('psycopg')\n"
        "def go(dsn):\n"
        "    return driver.connect(dsn)\n"
    )
    assert routing.find_unpooled_connections(dynamic, "command_center/x.py")

    # A lazily imported driver inside the function body is the shape every
    # store in `db/` already uses for the pool, so it must be caught too.
    lazy = ast.parse(
        "def go(dsn):\n    import psycopg2\n    return psycopg2.connect(dsn)\n"
    )
    assert routing.find_unpooled_connections(lazy, "command_center/x.py")


def test_the_scanner_does_not_fire_on_the_legitimate_shapes() -> None:
    """Precision matters more than recall here, because a noisy gate gets muted.

    Three shapes this repository is full of must stay clean, or the gate would
    have to be suppressed file by file until it meant nothing.
    """
    # SQLite is the authority store. It has no server to fork a backend on, and
    # `runtime/db/core.py` opens it per call by design.
    sqlite = ast.parse("import sqlite3\ndef go(path):\n    return sqlite3.connect(path)\n")
    assert routing.find_unpooled_connections(sqlite, "command_center/runtime/db/core.py") == []

    # Qt signals. Several hundred of these live under `command_center/desktop`,
    # and a rule keyed on the spelling `\.connect\(` would flag every one.
    qt = ast.parse(
        "class Window:\n"
        "    def wire(self):\n"
        "        self.button.clicked.connect(self.refresh)\n"
        "        self.sidebar.section_selected.connect(self._on_section)\n"
    )
    assert routing.find_unpooled_connections(qt, "command_center/desktop/main_window.py") == []

    # The correct store shape: lazily import the pool, fall back to it.
    store = ast.parse(
        "class Store:\n"
        "    def __init__(self, connection_factory=None):\n"
        "        self._factory = connection_factory\n"
        "    def _connection(self):\n"
        "        if self._factory is not None:\n"
        "            return self._factory()\n"
        "        from command_center.db import pool\n"
        "        return pool.connection()\n"
    )
    assert routing.find_unpooled_connections(store, "command_center/db/new_store.py") == []
    assert routing.find_second_pools(store, "command_center/db/new_store.py") == []
    assert routing.injectable_stores(store) == ["Store"]
    assert routing.resolves_the_pool_fallback(store) is True

    # The same store having dropped its fallback: still no violation of rules
    # 1 or 2 — which is exactly why rule 3 has to exist separately.
    dropped = ast.parse(
        "class Store:\n"
        "    def __init__(self, connection_factory=None):\n"
        "        self._factory = connection_factory\n"
        "    def _connection(self):\n"
        "        return self._factory()\n"
    )
    assert routing.find_unpooled_connections(dropped, "command_center/db/new_store.py") == []
    assert routing.injectable_stores(dropped) == ["Store"]
    assert routing.resolves_the_pool_fallback(dropped) is False


def test_rule_three_asks_only_about_a_default_the_caller_can_omit() -> None:
    """A required `connection_factory` is not an unrouted store.

    `orchestrator/planner.py` is the live case: `Planner(connection_factory)`
    takes the factory as a required argument, so there is no "caller said
    nothing" branch for the pool to be the answer to. Its caller in `db/cli.py`
    passes a connection taken from `pool.connection()`. The first draft of this
    gate flagged it, and the only ways to satisfy that would have been to give
    `Planner` a pool fallback nothing calls, or to exempt the file by name —
    both worse than narrowing the rule to what it actually protects.
    """
    required = ast.parse(
        "class Planner:\n"
        "    def __init__(self, connection_factory):\n"
        "        self._factory = connection_factory\n"
    )
    assert routing.injectable_stores(required) == []

    # `= None` is what makes it a store's own decision, and then the pool must
    # be that decision.
    optional = ast.parse(
        "class Planner:\n"
        "    def __init__(self, connection_factory=None):\n"
        "        self._factory = connection_factory\n"
    )
    assert routing.injectable_stores(optional) == ["Planner"]

    # Keyword-only, which is the shape a future store is as likely to use.
    kwonly = ast.parse(
        "class Store:\n"
        "    def __init__(self, *, connection_factory=None):\n"
        "        self._factory = connection_factory\n"
    )
    assert routing.injectable_stores(kwonly) == ["Store"]
    kwonly_required = ast.parse(
        "class Store:\n"
        "    def __init__(self, *, connection_factory):\n"
        "        self._factory = connection_factory\n"
    )
    assert routing.injectable_stores(kwonly_required) == []


def test_the_real_planner_is_the_required_factory_case() -> None:
    """Pinned against the file, not just a synthetic: a rename or a signature
    change that gives `Planner` a default must bring it under rule 3 rather than
    silently keep the narrowing that was justified by its current shape."""
    path = routing.REPO_ROOT / "command_center/orchestrator/planner.py"
    tree = _parse(path)
    assert routing.injectable_stores(tree) == []
    assert "connection_factory" in path.read_text(encoding="utf-8")


def test_the_second_pool_rule_distinguishes_the_opener_from_the_constructor() -> None:
    """`pool.open_pool()` at startup is the point; `adapter.open_pool()` is not.

    Getting this backwards would flag the four entry points that are behaving
    correctly and miss the one construction that matters, so both directions are
    pinned rather than assumed.
    """
    startup = ast.parse(
        "from command_center.db import pool\ndef main():\n    pool.open_pool()\n"
    )
    assert routing.find_second_pools(startup, "command_center/api/app.py") == []

    via_adapter = ast.parse(
        "from command_center.db import adapter\n"
        "def build(dsn):\n"
        "    return adapter.open_pool(dsn, min_size=1)\n"
    )
    assert routing.find_second_pools(via_adapter, "command_center/worker/runner.py")

    imported = ast.parse(
        "from command_center.db.adapter import open_pool\n"
        "def build(dsn):\n"
        "    return open_pool(dsn)\n"
    )
    assert routing.find_second_pools(imported, "command_center/worker/runner.py")

    driver_pool = ast.parse(
        "from psycopg_pool import ConnectionPool\n"
        "def build(dsn):\n"
        "    return ConnectionPool(dsn, min_size=4)\n"
    )
    assert routing.find_second_pools(driver_pool, "command_center/worker/runner.py")

    # `pool.py` itself is the one module allowed to do it.
    assert routing.find_second_pools(via_adapter, routing.POOL_MODULE) == []
    assert routing.find_second_pools(driver_pool, routing.POOL_MODULE) == []
