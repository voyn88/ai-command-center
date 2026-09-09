"""Pool-routing fitness scanner (VOYN-W0-AICC-SRV-09-READ-POOL).

`command_center/db/pool.py` exists because PostgreSQL forks a backend process
per connection: at the rates the dispatcher and the worker fleet generate,
connect-per-query spends more time forking backends than running queries. Every
PostgreSQL read this service performs today already goes through that pool —
see `docs/operations/SRV09_READ_POOL_PREMISE_CHECK.md` for the survey — but
until this module existed nothing *held* it there. The invariant was a
convention, kept by everyone who happened to copy an existing store.

That is the gap this scanner closes. It does not move any read onto the pool
(there was none left to move); it makes the property that is already true a
property that stays true, so the next store cannot reintroduce connect-per-query
by writing the one obvious line.

Three mechanical rules, consumed by `test_pool_routing_fitness.py`:

1. **No unpooled connection.** No file under `command_center/` may call a
   PostgreSQL driver's `connect()`. The one exception is declared in
   :data:`UNPOOLED_CONNECT_ALLOWED` with its reason, and the test pins that map
   so it cannot quietly grow.
2. **No second pool.** Only `command_center/db/pool.py` may build a pool. A
   pool built anywhere else is a second, unmanaged connection budget against
   the same server — the failure mode the singleton exists to prevent, and one
   that `pool_stats()` would not even report.
3. **The fallback stays the pool.** Every injectable store (the
   `connection_factory=None` constructor shape the whole `db/` package shares)
   must still resolve to `pool.connection()` when no factory is injected. The
   injection point is for tests; production reaches it through the default, and
   a store that loses the default loses the pool without failing anything.

Scope is `command_center/` and deliberately not `tests/`: the suites open
connections *as each role* to prove the grants, which is the one thing a pooled
connection cannot do. `sqlite3` is out of scope too — this rule is about the
PostgreSQL backend-per-connection cost, and SQLite is the authority store with
no pool to bypass.

Structural, over the AST, and keyed on *binding* rather than spelling: a call is
flagged because the name it is called on was bound to a driver in that same
file, not because the text `connect` appears. That is what keeps the desktop's
several hundred Qt `signal.connect(...)` calls and `runtime/db/core.py`'s
`db.connect(db_path)` out of the results while still catching
`import psycopg as pg; pg.connect(dsn)`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.aios_boundary import REPO_ROOT, iter_python_files

__all__ = [
    "DRIVER_MODULES",
    "POOL_MODULE",
    "UNPOOLED_CONNECT_ALLOWED",
    "find_second_pools",
    "find_unpooled_connections",
    "injectable_stores",
    "iter_scanned_files",
    "resolves_the_pool_fallback",
]

#: Python distributions that speak the PostgreSQL wire protocol. `sqlite3` is
#: absent on purpose: it has no server to fork a backend on.
DRIVER_MODULES = frozenset({"psycopg", "psycopg2", "psycopg_pool"})

#: Pool constructors exported by `psycopg_pool`.
POOL_CONSTRUCTORS = frozenset({"ConnectionPool", "AsyncConnectionPool"})

#: The one module allowed to build a pool.
POOL_MODULE = "command_center/db/pool.py"

#: The `aios-db` seam. Its `open_pool` is the raw, un-singletoned constructor
#: `pool.py` wraps; reaching it from anywhere else is rule 2's violation.
ADAPTER_MODULE = "command_center.db.adapter"

#: The package this gate polices.
SCANNED_PREFIX = "command_center/"

#: Files allowed to open a connection outside the pool, and why. A map rather
#: than a set so the reason is reviewed alongside the exemption, and pinned by
#: `test_the_only_unpooled_connection_is_the_credential_probe` so that adding an
#: entry is a deliberate edit to a test rather than a silent widening.
UNPOOLED_CONNECT_ALLOWED: dict[str, str] = {
    "command_center/ops/credential_rotation.py": (
        "Probes a *candidate* credential before it is installed. The pool is "
        "built from the credential currently in force, so by construction this "
        "check cannot run on it: routing it through the pool would test the old "
        "password and report the new one healthy. One connection per rotation, "
        "not per query, so the cost this gate exists to prevent does not apply."
    ),
}


def iter_scanned_files() -> list[Path]:
    """Every non-test `*.py` under `command_center/`."""
    return [
        path
        for path in iter_python_files()
        if _rel(path).startswith(SCANNED_PREFIX) and not _is_test_path(_rel(path))
    ]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_test_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return "tests" in parts or parts[-1].startswith("test_")


def _top(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _literal_import_target(node: ast.AST) -> str | None:
    """The module name in `importlib.import_module("x")` / `__import__("x")`.

    Only literal arguments resolve. A computed module name is beyond a static
    scanner and is recorded as a limit rather than papered over: the same
    acknowledged gap `aios_boundary` documents for its own driver detection.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_import_module = (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    )
    is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
    if not (is_import_module or is_dunder_import):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


class _Bindings:
    """What each name in one module is bound to, for the three questions asked.

    Built per file rather than per call site because the whole point is that a
    call is judged by what its receiver *is*, not by what it is spelled. A file
    that never imports a driver cannot violate rule 1 no matter how many
    `.connect(...)` calls it makes — which is exactly the desktop's situation.
    """

    def __init__(self) -> None:
        self.driver_modules: set[str] = set()
        self.driver_connects: set[str] = set()
        self.pool_constructors: set[str] = set()
        self.adapter_modules: set[str] = set()
        self.adapter_open_pools: set[str] = set()
        self.pool_modules: set[str] = set()


def _bindings(tree: ast.AST) -> _Bindings:
    found = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or _top(alias.name)
                if _top(alias.name) in DRIVER_MODULES:
                    found.driver_modules.add(bound)
                if alias.name == ADAPTER_MODULE and alias.asname:
                    found.adapter_modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _top(module) in DRIVER_MODULES:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name == "connect":
                        found.driver_connects.add(bound)
                    elif alias.name in POOL_CONSTRUCTORS:
                        found.pool_constructors.add(bound)
            if module == ADAPTER_MODULE:
                for alias in node.names:
                    if alias.name == "open_pool":
                        found.adapter_open_pools.add(alias.asname or alias.name)
            # `from command_center.db import adapter` / `import pool`
            if module == "command_center.db":
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name == "adapter":
                        found.adapter_modules.add(bound)
                    elif alias.name == "pool":
                        found.pool_modules.add(bound)
        elif isinstance(node, ast.Assign):
            target_module = _literal_import_target(node.value)
            if target_module and _top(target_module) in DRIVER_MODULES:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.driver_modules.add(target.id)
    return found


def find_unpooled_connections(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """Rule 1: driver `connect()` calls, other than the declared exemption."""
    if rel_path in UNPOOLED_CONNECT_ALLOWED:
        return []
    bound = _bindings(tree)
    if not (bound.driver_modules or bound.driver_connects):
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id in bound.driver_modules
        ):
            found.append(
                (node.lineno, f"{func.value.id}.connect(...) opens a connection outside the pool")
            )
        elif isinstance(func, ast.Name) and func.id in bound.driver_connects:
            found.append(
                (node.lineno, f"{func.id}(...) opens a connection outside the pool")
            )
    return sorted(found)


def find_second_pools(tree: ast.AST, rel_path: str) -> list[tuple[int, str]]:
    """Rule 2: a pool built outside `pool.py`.

    `pool.open_pool(...)` is *not* a violation and must not be: it is the
    singleton opener, and the entry points (`api/app.py`, `webapi/app.py`,
    `worker/__main__.py`, `db/cli.py`) are supposed to call it at startup. What
    is flagged is reaching past it to the raw constructor — `adapter.open_pool`
    or `psycopg_pool.ConnectionPool` — which yields a pool no `close_pool()`
    closes and no `replace_pool()` rotates.
    """
    if rel_path == POOL_MODULE:
        return []
    bound = _bindings(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "open_pool"
            and isinstance(func.value, ast.Name)
            and func.value.id in bound.adapter_modules
        ):
            found.append(
                (node.lineno, f"{func.value.id}.open_pool(...) builds a second pool")
            )
        elif isinstance(func, ast.Name) and func.id in bound.adapter_open_pools:
            found.append((node.lineno, f"{func.id}(...) builds a second pool"))
        elif isinstance(func, ast.Name) and func.id in bound.pool_constructors:
            found.append((node.lineno, f"{func.id}(...) builds a second pool"))
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in POOL_CONSTRUCTORS
            and isinstance(func.value, ast.Name)
            and func.value.id in bound.driver_modules
        ):
            found.append(
                (node.lineno, f"{func.value.id}.{func.attr}(...) builds a second pool")
            )
    return sorted(found)


def _defaulted_parameters(args: ast.arguments) -> set[str]:
    """Parameter names that carry a default, positional and keyword-only alike.

    `args.defaults` aligns to the *tail* of the positional parameters; a
    keyword-only default is `None` in `kw_defaults` when absent, which is not
    the same as a default of `None`.
    """
    named: set[str] = set()
    positional = args.posonlyargs + args.args
    if args.defaults:
        for arg in positional[-len(args.defaults) :]:
            named.add(arg.arg)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            named.add(arg.arg)
    return named


def injectable_stores(tree: ast.AST) -> list[str]:
    """Classes taking the package's `connection_factory=None` constructor shape.

    Detected by the parameter, not by a name or a base class: the seven stores
    that have it share no ancestor (`PostgresTableMirror` is one of them, the
    admin/read surfaces are plain classes), and a rule keyed on the base would
    miss exactly the hand-written ones.

    The parameter must have a *default*, and that qualifier is the whole rule
    rather than an incidental detail. What rule 3 protects is the behaviour a
    caller gets when it says nothing — so there has to be a "says nothing" to
    have. `orchestrator/planner.py`'s `Planner(connection_factory)` takes the
    factory as a required argument and is the case that proved this: it holds no
    opinion about where connections come from, its caller (`db/cli.py`) hands it
    one taken from `pool.connection()`, and demanding a fallback it can never
    reach would have meant adding a second, unused way to reach the pool purely
    to satisfy a gate. A required factory is covered by rules 1 and 2 at the
    caller, which is where the decision actually is.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "__init__":
                continue
            if "connection_factory" in _defaulted_parameters(item.args):
                found.append(node.name)
    return sorted(found)


def resolves_the_pool_fallback(tree: ast.AST) -> bool:
    """Rule 3: the module reaches `pool.connection()` somewhere.

    Asserted at module rather than method granularity on purpose. The stores
    resolve the pool inside `_connection`, but that name is a convention, and
    pinning it would make the gate a rule about a method name — the mistake
    `mirror_discovery` documents for its own first two attempts. What matters is
    that the module still knows how to reach the pool at all; a store that drops
    the fallback has no other way to spell it.
    """
    bound = _bindings(tree)
    if not bound.pool_modules:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "connection"
            and isinstance(func.value, ast.Name)
            and func.value.id in bound.pool_modules
        ):
            return True
    return False
