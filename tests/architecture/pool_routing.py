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

1. **No unpooled connection.** No scanned file may reach a PostgreSQL driver's
   `connect`. The one exception is declared in :data:`UNPOOLED_CONNECT_ALLOWED`
   with its reason, and the test pins that map so it cannot quietly grow.
2. **No second pool.** Only `command_center/db/pool.py` may build a pool. A
   pool built anywhere else is a second, unmanaged connection budget against
   the same server — the failure mode the singleton exists to prevent, and one
   that `pool_stats()` would not even report.
3. **The fallback stays the pool.** Every injectable store (the
   `connection_factory=None` constructor shape the whole `db/` package shares)
   must still resolve to `pool.connection()` when no factory is injected. The
   injection point is for tests; production reaches it through the default, and
   a store that loses the default loses the pool without failing anything.

Scope is every non-test module in the repository, not only `command_center/`.
Rules keyed to one package are avoided by moving the file: the cost is paid by
the *server*, which does not care which directory the connecting process was
started from, so an operational script under `scripts/` or a service module
under `native_gateway/` is in scope on the same argument. `tests/` is the
deliberate exclusion: the suites open connections *as each role* to prove the
grants, which is the one thing a pooled connection cannot do. `sqlite3` is out
of scope too — this rule is about the PostgreSQL backend-per-connection cost,
and SQLite is the authority store with no pool to bypass.

Structural, over the AST, and keyed on *binding* rather than spelling. Every
`name.attr.attr` chain is resolved back through the file's own imports to the
dotted path it actually denotes, and the rules match on that path. So
`signal.connect(...)` — the desktop's several hundred Qt calls — and
`runtime/db/core.py`'s `db.connect(db_path)` resolve to nothing and stay clean,
while `import psycopg as pg; pg.connect(...)`, `from psycopg import connect as
_open`, `psycopg.Connection.connect(...)` and the `importlib.import_module`
form all resolve onto `psycopg.connect` and are caught by one rule rather than
one clause per spelling.

Resolving the whole chain, rather than only `Name.attr`, is what the first
draft of this scanner got wrong, in both directions:

* `psycopg.Connection.connect(dsn)` and `psycopg.AsyncConnection.connect(dsn)`
  — psycopg 3's *documented* explicit-class API, and therefore a likelier
  copy-paste than the bare function — resolve through a two-deep chain and were
  missed by rule 1 entirely.
* `psycopg2.pool.ThreadedConnectionPool` and its siblings are the whole of
  psycopg2's pooling API, and every one of them was missed by rule 2: the
  constructor names were unknown to it, and the dotted form resolved to
  nothing. A second pool built the psycopg2 way passed the gate clean.
* `import command_center.db.adapter` followed by
  `command_center.db.adapter.open_pool(...)` was missed for the same reason,
  where the `from command_center.db import adapter` spelling was caught.
* Rule 3 recognised exactly one way to reach the pool
  (`from command_center.db import pool`), so `import command_center.db.pool as
  pool`, `from command_center.db.pool import connection` and `from . import
  pool` all read as *lost fallbacks* — a gate failing correct code, which is
  the failure mode that gets a gate deleted rather than fixed.

A reference counts, not only a call: `functools.partial(psycopg.connect, dsn)`
hands the driver to something that will call it later, and a rule that only
looked at `ast.Call` would watch it go past.

Acknowledged limit, stated rather than papered over (the same one
`aios_boundary` records for its own driver detection): a module reached through
a *non-literal* dynamic import, or through a name rebound at runtime, is beyond
a static scanner. Literal `importlib`/`__import__`, aliases, relative imports
and attribute chains are resolved.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.aios_boundary import REPO_ROOT, iter_python_files

__all__ = [
    "ADAPTER_MODULE",
    "DRIVER_MODULES",
    "POOL_CONNECTION",
    "POOL_CONSTRUCTORS",
    "POOL_MODULE",
    "RAW_POOL_OPENERS",
    "UNPOOLED_CONNECT_ALLOWED",
    "find_second_pools",
    "find_unpooled_connections",
    "injectable_stores",
    "iter_scanned_files",
    "resolve_references",
    "resolves_the_pool_fallback",
]

#: Python distributions that speak the PostgreSQL wire protocol. `sqlite3` is
#: absent on purpose: it has no server to fork a backend on.
DRIVER_MODULES = frozenset({"psycopg", "psycopg2", "psycopg_pool"})

#: Pool classes exported by the drivers. `psycopg_pool` supplies the first
#: four; the rest are psycopg2's `psycopg2.pool` module, which is a complete
#: second way to hold a connection budget and has to be named to be seen.
POOL_CONSTRUCTORS = frozenset(
    {
        "ConnectionPool",
        "AsyncConnectionPool",
        "NullConnectionPool",
        "AsyncNullConnectionPool",
        "SimpleConnectionPool",
        "ThreadedConnectionPool",
        "PersistentConnectionPool",
    }
)

#: Raw pool openers: the un-singletoned constructors `pool.py` wraps. Reaching
#: either from anywhere else is rule 2's violation. `aios_db.open_pool` is
#: primarily the AIOS boundary gate's business (only `db/adapter.py` may import
#: that package at all) but is named here too, so that the rule about pools
#: does not depend on a different gate staying switched on.
RAW_POOL_OPENERS = frozenset(
    {"command_center.db.adapter.open_pool", "aios_db.open_pool"}
)

#: The one module allowed to build a pool.
POOL_MODULE = "command_center/db/pool.py"

#: The declared `aios-db` seam. Forwarding `aios_db.open_pool` is the whole
#: reason this file exists (the AIOS boundary gate lets no other module import
#: that package at all), so naming that opener here is not rule 2's violation
#: *in this one file* — but constructing a driver pool still is. Exempting the
#: seam only for the name it is the seam for is the narrowest form that does
#: not fail correct code if the re-export ever becomes a wrapper.
ADAPTER_MODULE = "command_center/db/adapter.py"

#: What rule 3 requires an injectable store to still be able to reach.
POOL_CONNECTION = "command_center.db.pool.connection"

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
    """Every non-test `*.py` in the repository."""
    return [path for path in iter_python_files() if not _is_test_path(_rel(path))]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_test_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return "tests" in parts or parts[-1].startswith("test_")


def _top(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _package_of(rel_path: str | None) -> str | None:
    """The dotted package a file lives in, for resolving relative imports.

    `command_center/db/backlog_store.py` -> `command_center.db`. Returns None
    when the caller did not say which file this tree came from, in which case a
    relative import simply does not resolve — a synthetic tree in a test has no
    package to be relative to.
    """
    if rel_path is None:
        return None
    parts = rel_path.split("/")[:-1]
    return ".".join(parts) if parts else ""


def _absolute_module(node: ast.ImportFrom, rel_path: str | None) -> str | None:
    """`from . import pool` inside `command_center/db/` -> `command_center.db`."""
    module = node.module or ""
    if not node.level:
        return module
    package = _package_of(rel_path)
    if package is None:
        return None
    ancestors = package.split(".") if package else []
    climb = node.level - 1
    if climb:
        if climb > len(ancestors):
            return None
        ancestors = ancestors[:-climb]
    base = ".".join(ancestors)
    if not module:
        return base
    return f"{base}.{module}" if base else module


def _dotted_parts(node: ast.AST) -> list[str] | None:
    """`a.b.c` -> `["a", "b", "c"]`; anything else -> None.

    Only pure name/attribute chains resolve. `f().connect` has a call in the
    middle and denotes whatever `f()` returned, which this scanner does not
    claim to know.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return parts


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


def _aliases(tree: ast.AST, rel_path: str | None) -> dict[str, str]:
    """What every name bound by an import in this module actually denotes.

    Built per file, because the whole point is that a call is judged by what its
    receiver *is*, not by what it is spelled. A file that never imports a driver
    cannot violate rule 1 no matter how many `.connect(...)` calls it makes —
    which is exactly the desktop's situation.

    `import a.b.c` binds `a`, so the alias maps `a` to itself and the rest of
    the chain is read literally off the source; every other form binds the last
    component and maps it to its full dotted path.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[_top(alias.name)] = _top(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, rel_path)
            if module is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                aliases[bound] = f"{module}.{alias.name}" if module else alias.name
        elif isinstance(node, ast.Assign):
            target = _literal_import_target(node.value)
            if target is None and isinstance(node.value, (ast.Name, ast.Attribute)):
                # `pg = psycopg`: an alias for an alias, which is a rename with
                # extra steps and must not be a hiding place.
                parts = _dotted_parts(node.value)
                if parts and parts[0] in aliases:
                    target = ".".join([aliases[parts[0]], *parts[1:]])
            if target is None:
                continue
            for element in node.targets:
                if isinstance(element, ast.Name):
                    aliases[element.id] = target
    return aliases


def resolve_references(
    tree: ast.AST, rel_path: str | None = None
) -> list[tuple[int, str, bool]]:
    """Every imported thing this module names, as `(line, dotted path, called)`.

    One pass, and the three rules are predicates over its output. Chains are
    reported at their full length only — the `psycopg` in
    `psycopg.Connection.connect` is not also reported on its own — so a single
    misuse produces a single finding.
    """
    aliases = _aliases(tree, rel_path)
    if not aliases:
        return []
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    inner = {
        id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        if id(node) in inner:
            continue  # part of a longer chain; the outermost node carries it
        if not isinstance(node.ctx, ast.Load):
            continue
        parts = _dotted_parts(node)
        if not parts or parts[0] not in aliases:
            continue
        resolved = ".".join([aliases[parts[0]], *parts[1:]])
        found.append((node.lineno, resolved, id(node) in called))
    return sorted(found)


def _how(called: bool) -> str:
    return "opens" if called else "hands out"


def find_unpooled_connections(
    tree: ast.AST, rel_path: str | None = None
) -> list[tuple[int, str]]:
    """Rule 1: a PostgreSQL driver's `connect`, other than the declared exemption.

    Matched as "a name that resolves into a driver distribution and ends in
    `connect`", which covers the module-level function (`psycopg.connect`), the
    class methods psycopg 3's own documentation leads with
    (`psycopg.Connection.connect`, `psycopg.AsyncConnection.connect`), and
    psycopg2's `psycopg2.connect`, without needing a clause each.
    """
    if rel_path is not None and rel_path in UNPOOLED_CONNECT_ALLOWED:
        return []
    found: list[tuple[int, str]] = []
    for lineno, resolved, called in resolve_references(tree, rel_path):
        parts = resolved.split(".")
        if parts[0] in DRIVER_MODULES and parts[-1] == "connect":
            found.append(
                (lineno, f"{resolved} {_how(called)} a connection outside the pool")
            )
    return sorted(found)


def find_second_pools(
    tree: ast.AST, rel_path: str | None = None
) -> list[tuple[int, str]]:
    """Rule 2: a pool built outside `pool.py`.

    `pool.open_pool(...)` is *not* a violation and must not be: it is the
    singleton opener, and the entry points (`api/app.py`, `webapi/app.py`,
    `worker/__main__.py`, `db/cli.py`) are supposed to call it at startup. What
    is flagged is reaching past it to a raw constructor — `adapter.open_pool`,
    or any of the drivers' own pool classes — which yields a pool no
    `close_pool()` closes and no `replace_pool()` rotates.
    """
    if rel_path == POOL_MODULE:
        return []
    found: list[tuple[int, str]] = []
    for lineno, resolved, called in resolve_references(tree, rel_path):
        parts = resolved.split(".")
        is_raw_opener = resolved in RAW_POOL_OPENERS and not (
            rel_path == ADAPTER_MODULE and resolved == "aios_db.open_pool"
        )
        is_driver_pool = parts[0] in DRIVER_MODULES and parts[-1] in POOL_CONSTRUCTORS
        if is_raw_opener or is_driver_pool:
            verb = "builds" if called else "hands out"
            found.append((lineno, f"{resolved} {verb} a second pool"))
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

    Detected by the parameter, not by a name or a base class: the stores that
    have it share no ancestor (`PostgresTableMirror` is one of them, the
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


def resolves_the_pool_fallback(tree: ast.AST, rel_path: str | None = None) -> bool:
    """Rule 3: the module reaches `command_center.db.pool.connection` somewhere.

    Asserted at module rather than method granularity on purpose. The stores
    resolve the pool inside `_connection`, but that name is a convention, and
    pinning it would make the gate a rule about a method name — the mistake
    `mirror_discovery` documents for its own first two attempts. What matters is
    that the module still knows how to reach the pool at all; a store that drops
    the fallback has no other way to spell it.

    Every spelling that reaches it counts, which is the point: this rule is the
    one that fails *correct* code when it is too narrow, and a store rewritten
    to say `from . import pool` has not lost anything.
    """
    return any(
        resolved == POOL_CONNECTION
        for _, resolved, _ in resolve_references(tree, rel_path)
    )
