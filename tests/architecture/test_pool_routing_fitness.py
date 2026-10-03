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
    """No non-test file in the repository opens its own PostgreSQL connection.

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
        if not routing.resolves_the_pool_fallback(tree, rel):
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


def test_rule_one_catches_the_drivers_own_class_api() -> None:
    """`psycopg.Connection.connect(...)` is the form the driver's docs lead with.

    The first draft matched `<name>.connect(...)` where `<name>` was itself a
    bound driver — one attribute deep — so the module-level function was caught
    and psycopg 3's explicit-class API, two deep, was not. A gate that catches
    only the spelling nobody copies is worse than no gate, because it reports
    green about a rule it is not enforcing.
    """
    for source in (
        "import psycopg\ndef go(dsn):\n    return psycopg.Connection.connect(dsn)\n",
        (
            "import psycopg\nasync def go(dsn):\n"
            "    return await psycopg.AsyncConnection.connect(dsn)\n"
        ),
        (
            "from psycopg import Connection\n"
            "def go(dsn):\n    return Connection.connect(dsn)\n"
        ),
        (
            "from psycopg import AsyncConnection as C\n"
            "async def go(dsn):\n    return await C.connect(dsn)\n"
        ),
    ):
        assert routing.find_unpooled_connections(
            ast.parse(source), "command_center/db/new_store.py"
        ), source


def test_a_driver_handed_over_uncalled_is_still_a_bypass() -> None:
    """`partial(psycopg.connect, dsn)` connects; it just does it somewhere else.

    Rules that only inspect `ast.Call` watch the reference go past. Naming the
    driver is the decision, and that is what the scanner reports on — so the
    injection seam every store in `db/` offers cannot be used to smuggle the
    driver in as a `connection_factory`.
    """
    handed_over = ast.parse(
        "import psycopg\n"
        "from functools import partial\n"
        "def build(dsn):\n"
        "    return partial(psycopg.connect, dsn)\n"
    )
    found = routing.find_unpooled_connections(handed_over, "command_center/db/x.py")
    assert found and "hands out" in found[0][1]

    injected = ast.parse(
        "import psycopg\n"
        "from command_center.db.work_queue_read import WorkQueueReadStore\n"
        "def store(dsn):\n"
        "    return WorkQueueReadStore(\n"
        "        connection_factory=lambda: psycopg.connect(dsn)\n"
        "    )\n"
    )
    assert routing.find_unpooled_connections(injected, "command_center/api/x.py")


def test_rule_two_covers_both_drivers_pooling_apis() -> None:
    """psycopg2's pool module is a second pool as much as psycopg_pool's is.

    `psycopg2.pool.ThreadedConnectionPool` and its siblings are the whole of
    that driver's pooling API. The first draft knew only `psycopg_pool`'s two
    class names, so a pool built the psycopg2 way — the form most search results
    still show — passed clean while the psycopg 3 form was caught.
    """
    for source in (
        (
            "import psycopg2.pool\n"
            "def build(dsn):\n"
            "    return psycopg2.pool.ThreadedConnectionPool(1, 5, dsn)\n"
        ),
        (
            "from psycopg2.pool import SimpleConnectionPool\n"
            "def build(dsn):\n"
            "    return SimpleConnectionPool(1, 5, dsn)\n"
        ),
        (
            "from psycopg2.pool import PersistentConnectionPool as P\n"
            "def build(dsn):\n"
            "    return P(1, 5, dsn)\n"
        ),
        (
            "import psycopg_pool\n"
            "def build(dsn):\n"
            "    return psycopg_pool.NullConnectionPool(dsn)\n"
        ),
    ):
        assert routing.find_second_pools(
            ast.parse(source), "command_center/worker/runner.py"
        ), source


def test_rule_two_reads_the_dotted_module_spelling() -> None:
    """`import a.b.c` then `a.b.c.f()` binds `a` and says the rest inline.

    Both raw openers are reachable that way, and neither was seen when the
    scanner only resolved a single `Name.attr`.
    """
    dotted_adapter = ast.parse(
        "import command_center.db.adapter\n"
        "def build(dsn):\n"
        "    return command_center.db.adapter.open_pool(dsn)\n"
    )
    assert routing.find_second_pools(dotted_adapter, "command_center/worker/runner.py")

    # `aios_db` is the AIOS boundary gate's business first — only `db/adapter.py`
    # may import it — but rule 2 does not want to depend on a second gate staying
    # switched on to know what a pool is.
    direct = ast.parse(
        "from aios_db import open_pool\ndef build(dsn):\n    return open_pool(dsn)\n"
    )
    assert routing.find_second_pools(direct, "command_center/worker/runner.py")


def test_the_aios_seam_may_forward_the_raw_opener_but_not_build_a_pool() -> None:
    """`db/adapter.py` exists to be the one place `aios_db.open_pool` is named.

    Exempting the seam for exactly that name — and for nothing else — is what
    keeps rule 2 from failing correct code if the re-export ever becomes a
    wrapper, without turning the adapter into a second place a driver pool can
    be constructed.
    """
    forwarding = ast.parse(
        "from aios_db import open_pool\n"
        "def open(dsn, **kwargs):\n"
        "    return open_pool(dsn, **kwargs)\n"
    )
    assert routing.find_second_pools(forwarding, routing.ADAPTER_MODULE) == []

    driver_pool = ast.parse(
        "from psycopg_pool import ConnectionPool\n"
        "def build(dsn):\n"
        "    return ConnectionPool(dsn)\n"
    )
    assert routing.find_second_pools(driver_pool, routing.ADAPTER_MODULE)

    # The real file, so the exemption cannot be pointing at a renamed module.
    path = routing.REPO_ROOT / routing.ADAPTER_MODULE
    assert "aios_db" in path.read_text(encoding="utf-8")
    assert routing.find_second_pools(_parse(path), routing.ADAPTER_MODULE) == []


def test_rule_three_accepts_every_way_of_reaching_the_pool() -> None:
    """Too narrow, this rule fails correct code — the way a gate gets deleted.

    The first draft recognised `from command_center.db import pool` and nothing
    else, so three ordinary spellings of the same import read as a store that
    had *lost* its fallback. The remedy a developer applies to a gate like that
    is to rewrite a correct import until the gate stops complaining, which
    teaches that the gate is about spelling.
    """
    rel = "command_center/db/x.py"
    body = "def _connection(self):\n        return {}\n"
    for header, call in (
        ("from command_center.db import pool", "pool.connection()"),
        ("import command_center.db.pool as pool", "pool.connection()"),
        ("from command_center.db.pool import connection", "connection()"),
        ("import command_center.db.pool", "command_center.db.pool.connection()"),
        # Relative, which only resolves because the scanner is told which file
        # it is reading — the reason `resolves_the_pool_fallback` takes a path.
        ("from . import pool", "pool.connection()"),
    ):
        source = f"{header}\nclass Store:\n    " + body.format(call)
        assert routing.resolves_the_pool_fallback(ast.parse(source), rel), source

    # And the case the rule exists for still reads as unrouted.
    dropped = ast.parse(
        "class Store:\n"
        "    def __init__(self, connection_factory=None):\n"
        "        self._factory = connection_factory\n"
        "    def _connection(self):\n"
        "        return self._factory()\n"
    )
    assert routing.resolves_the_pool_fallback(dropped, rel) is False


def test_the_scan_is_the_repository_and_not_one_package() -> None:
    """A rule keyed to one directory is evaded by choosing another directory.

    The backend cost is paid by the PostgreSQL server, which does not know which
    package the connecting process was started from. `tests/` is the one
    deliberate exclusion — the suites connect *as each role* to prove the
    grants, which is the single thing a pooled connection cannot do.
    """
    scanned = {
        path.relative_to(routing.REPO_ROOT).as_posix()
        for path in routing.iter_scanned_files()
    }
    assert "command_center/db/pool.py" in scanned
    assert not [rel for rel in scanned if rel.startswith("tests/")]

    # Packages outside `command_center/` that ship code and could hold a store.
    for prefix in ("scripts/", "ops/"):
        assert [rel for rel in scanned if rel.startswith(prefix)], prefix
