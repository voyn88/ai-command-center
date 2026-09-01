"""AIOS boundary fitness scanner — the mechanics behind AC-01 for this repo.

Doctrine (ADR-0008 here; ADR-0015 in the `aios` repo): AIOS is the single
infrastructure engine; AI Command Center is its operator-facing control plane
and consumes AIOS only through versioned public API/SDK/event contracts.
Subsystems that overlap AIOS Core domains (queue, orchestration, authz, audit,
memory/persistence) predate that doctrine; they are *frozen*, not deleted —
they may keep running, but they may not grow.

This module implements two mechanical gates, consumed by
``tests/architecture/test_aios_boundary_fitness.py``:

1. **Core-internals import ban.** No Python file anywhere in the repository may
   import ``aios`` or any ``aios.*`` submodule (statically or via
   ``importlib.import_module``/``__import__`` with a literal name). Only the
   public SDK namespace ``aios_sdk`` is allowed, if/when it appears.

2. **Anti-engine growth gate.** Every non-test Python file is classified
   against engine *signatures* (see below). The set of currently matching
   files, with their categories, is frozen in
   ``tests/architecture/AIOS_BOUNDARY_BASELINE.json``. The gate fails when the
   detector's output differs from the baseline in the growth direction — a new
   file matches an engine signature, or an existing file gains a new engine
   category — and also when the baseline goes stale (entries for files that no
   longer match), so the snapshot always equals reality.

Engine signatures (deliberately structural, never a grep over comments):

- ``runtime_package``: any file under ``command_center/runtime/`` — the legacy
  local execution engine ADR-0008 froze wholesale.
- import signatures: importing an embedded-database/driver module (sqlite3,
  sqlalchemy, ...) marks ``memory``; importing a task-queue/workflow framework
  (celery, apscheduler, ...) marks ``queue``/``orchestration``; importing an
  auth/crypto-token library (jwt, passlib, ...) marks ``authz``;
  ``multiprocessing`` marks ``orchestration``.
- call signatures: process-lifecycle spawning — ``subprocess.Popen``,
  ``os.fork``/``os.posix_spawn``/``os.spawn*``/``multiprocessing.Process`` —
  marks ``orchestration`` (plain ``subprocess.run`` is not a signature: the
  control plane legitimately shells out to ``git`` and CLIs synchronously).
- name signatures: path segments whose tokens name an engine
  (queue/scheduler/supervisor/store/repository/audit/...). Applied only
  outside pure presentation layers (``command_center/ui``,
  ``command_center/desktop``, ``web``) so that UI *panels over* existing
  engines don't count as engines; presentation layers still fall under the
  import/call signatures, so an engine can't hide there.

  The ``memory`` name tokens (``db``, ``database``, ``store``, ``storage``,
  ``repository``, ``persistence``, ``memory``) are **corroborated**: they
  classify only when the same file also *behaves* like a store -- see
  :func:`_persists_data`. Those tokens name what a file is about as readily as
  what it is; a package directory called ``db/`` says where code lives, not
  whether the code inside owns an engine. Under a name-only rule a
  docstring-only ``__init__.py`` and a module that renders SQL text were
  violations, which made it impossible for the control plane to keep any
  database-adjacent module at all -- a gate policing names rather than
  behaviour. ``orchestration`` and ``authz`` still classify on the name alone:
  nothing loosened for them.

  Strictness is not reduced for a real engine. A file that owns persistence
  imports a driver (statically, aliased, or via ``importlib`` with a literal
  name), calls into a driver it bound itself, or writes durably to disk -- and a
  JSON/JSONL store with no driver at all is still caught by that last clause.
  What no longer counts is executing SQL on a connection someone else opened:
  that is delegation, and the engine is wherever the driver is.

  Acknowledged limit, stated rather than papered over: a driver reached through
  a *non-literal* dynamic import (``__import__(name_from_config)``) is beyond
  static analysis and is not detected. Literal ``importlib``/``__import__`` and
  aliases are.

  The ``audit`` name tokens (``audit``, ``provenance``) are corroborated too,
  by a different substitute: a table-mirror module -- one that declares
  ``MirroredTable``/``PostgresTableMirror`` subclasses over tables that already
  exist and nothing else -- adds no capability of its own, the same
  repository-vs-engine distinction the ``queue`` rule already draws (see
  :func:`_behaves_like_an_audit_engine`). Two such modules
  (``command_center/db/audit_store.py``, ``command_center/db/provenance_store.py``)
  were carried in the baseline with a signed false-positive justification
  before this corroboration existed; both drop out once it does, and pick the
  category back up the moment either gains a function or a class of its own --
  the growth direction stays covered. ``orchestration`` and ``authz`` remain
  name-alone: no equivalent substitute has been demonstrated for them, and
  corroborating a name without one is a straight loss of coverage.

Baseline maintenance (a reviewed change, never a casual one — see
``docs/AIOS_BOUNDARY.md``):

    python -m tests.architecture.aios_boundary            # show drift
    python -m tests.architecture.aios_boundary --write-baseline
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = Path(__file__).resolve().parent / "AIOS_BOUNDARY_BASELINE.json"

#: The one namespace AICC is allowed to import from the AIOS side.
SDK_ALLOWED_TOP_LEVEL = "aios_sdk"
#: The one production adapter allowed to import that public top-level package.
SDK_ADAPTER_PATH = "command_center/application/aios_tasks.py"

#: The second public AIOS distribution: universal PostgreSQL primitives
#: (`aios-db`). Allowed for the same reason `aios_sdk` is — it is a published,
#: independently versioned contract, not Core internals — and confined the same
#: way, to one reviewed adapter module.
DB_ALLOWED_TOP_LEVEL = "aios_db"
DB_ADAPTER_PATH = "command_center/db/adapter.py"

PUBLIC_AIOS_TOP_LEVELS: dict[str, str] = {
    SDK_ALLOWED_TOP_LEVEL: SDK_ADAPTER_PATH,
    DB_ALLOWED_TOP_LEVEL: DB_ADAPTER_PATH,
}
#: The banned core namespace.
CORE_TOP_LEVEL = "aios"

#: Directory names never scanned (VCS, caches, third-party trees, envs).
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".claude",
    }
)

#: The legacy local engine package ADR-0008 froze wholesale.
FROZEN_RUNTIME_PACKAGE = "command_center/runtime"

#: Pure presentation layers: exempt from *name* signatures only.
PRESENTATION_PREFIXES = ("command_center/ui/", "command_center/desktop/", "web/")

# --- import signatures (top-level module name → category) --------------------

DB_DRIVER_MODULES = frozenset(
    {
        "sqlite3",
        "sqlalchemy",
        "psycopg",
        # The pool package is a separate distribution from `psycopg` and was not
        # listed, so a file could open a PostgreSQL connection pool without the
        # gate seeing a driver at all.
        "psycopg_pool",
        "psycopg2",
        "asyncpg",
        "aiosqlite",
        "dbm",
        "shelve",
        "pymongo",
        "redis",
        "tinydb",
        "lmdb",
        "duckdb",
    }
)
QUEUE_FRAMEWORK_MODULES = frozenset(
    {"celery", "rq", "dramatiq", "huey", "arq", "taskiq", "kombu", "pika"}
)
ORCHESTRATION_FRAMEWORK_MODULES = frozenset(
    {
        "apscheduler",
        "schedule",
        "airflow",
        "prefect",
        "dagster",
        "temporalio",
        "luigi",
        "multiprocessing",
    }
)
AUTHZ_LIBRARY_MODULES = frozenset(
    {"jwt", "authlib", "passlib", "bcrypt", "oauthlib", "casbin", "keycloak"}
)

IMPORT_SIGNATURES: dict[str, frozenset[str]] = {
    "memory": DB_DRIVER_MODULES,
    "queue": QUEUE_FRAMEWORK_MODULES,
    "orchestration": ORCHESTRATION_FRAMEWORK_MODULES,
    "authz": AUTHZ_LIBRARY_MODULES,
}

# --- call signatures ---------------------------------------------------------

#: ``os.<attr>`` calls that spawn/adopt processes.
OS_SPAWN_ATTRS_PREFIXES = ("fork", "spawn", "posix_spawn")

# --- name signatures ---------------------------------------------------------

#: Path-segment tokens (segments split on non-alphanumerics) → category.
NAME_TOKEN_SIGNATURES: dict[str, frozenset[str]] = {
    "queue": frozenset({"queue", "queues"}),
    "orchestration": frozenset(
        {
            "scheduler",
            "schedulers",
            "supervisor",
            "orchestrator",
            "orchestration",
            "dispatch",
            "dispatcher",
            "autopilot",
            "autonomy",
            "daemon",
            "executor",
            "executors",
            "runner",
            "runners",
            "worker",
            "workers",
            "pipeline",
            "launcher",
            "engine",
        }
    ),
    "authz": frozenset(
        {"auth", "authn", "authz", "rbac", "acl", "oauth", "sso", "iam", "permission", "permissions"}
    ),
    "audit": frozenset({"audit", "provenance"}),
    "memory": frozenset(
        {
            "store",
            "stores",
            "storage",
            "repository",
            "repositories",
            "persistence",
            "db",
            "database",
            "memory",
        }
    ),
}

#: Whole-stem substrings → category (multi-token names a token split misses).
NAME_SUBSTRING_SIGNATURES: dict[str, tuple[str, ...]] = {
    "audit": ("activity_log", "audit_log", "event_log"),
}

#: Name categories that a path name alone cannot establish: they must be
#: corroborated by behaviour in the same file (see `_CORROBORATION`).
#:
#: `memory` and `queue` qualify because their tokens (`db`, `store`,
#: `repository`, `queue`, ...) name what a file is *about* as readily as what
#: it *is* — a directory called `db/` says nothing about whether the code
#: inside owns an engine. `audit` qualifies for a different reason: a
#: declarative table-mirror module (`MirroredTable`/`PostgresTableMirror`
#: subclasses over a table that already exists, nothing else) adds no
#: capability, and two such modules sat in the baseline as signed false
#: positives before this corroboration existed (see
#: `_behaves_like_an_audit_engine`). `orchestration` and `authz` are
#: deliberately absent: no equivalent substitute has been demonstrated for
#: them, and corroborating a name without one is a straight loss of coverage.
CORROBORATED_NAME_CATEGORIES = frozenset({"memory", "queue", "audit"})

# --- queue-engine behaviour (corroboration for the `queue` name signature) ---

#: Operations that make something a queue *engine* rather than a table of queue
#: rows. Storing and listing entries is what a repository does; handing work out
#: exactly once — claiming, leasing, acking, retrying — is the engine.
#: Two vocabularies, because there are two jobs with very different blast
#: radii. Using one frozenset for both was a real defect: it forced the wide
#: signal's precision requirement onto the narrow signal's coverage
#: requirement, and the coverage lost was the whole point of the name rule.
#:
#: **Corroboration** — applied only to files that already carry a `queue` name
#: token, of which this repository has three. A wide vocabulary is affordable
#: here: a false positive can only land on a file someone already named after a
#: queue, and the cost of a false negative is an undetected engine.
QUEUE_CORROBORATION_TOKENS = frozenset(
    {
        "claim",
        "claims",
        "lease",
        "leases",
        "heartbeat",
        "ack",
        "nack",
        "enqueue",
        "dequeue",
        "requeue",
        "reserve",
        "release",
        "retry",
        "redeliver",
        "pop",
        "push",
        "next",
        "backoff",
        "inflight",
        # The mainstream queue verbs, added after review demonstrated three
        # engines that escaped without them: `poll`/`finish`, `take`/`settle`,
        # `checkout`/`give_back`. These are what `java.util.concurrent`, the Go
        # channel idiom and any worker pool call their operations, so a queue
        # author reaches for them without thinking about this detector at all —
        # missing them was a coverage gap, not an adversary outwitting us.
        # Measured before adding: closes all three, and changes the
        # classification of none of the three queue-named files in this
        # repository.
        "poll",
        "peek",
        "take",
        "offer",
        "drain",
        "checkout",
        "settle",
        "complete",
        "fail",
        "fetch",
    }
)

#: **Unconditional** — applied to every file regardless of name, so precision
#: is the binding constraint. Measured, not guessed: a draft that used the wide
#: vocabulary here flagged twelve modules across the tree — auth *claims*, a
#: FastAPI *dispatch*, UI panels — because those are ordinary English words
#: that queues do not own. What survives is specific enough to mean the
#: mechanism rather than the word.
QUEUE_ENGINE_TOKENS = frozenset({"dequeue", "requeue", "nack", "redeliver"})

#: SQL markers, split the same way. `SKIP LOCKED` exists for exactly one
#: purpose — letting concurrent consumers take different rows — so it is safe
#: unconditionally. `FOR UPDATE` is plain row locking and only means "queue"
#: alongside a queue name, so it corroborates but never classifies on its own.
QUEUE_ENGINE_SQL_MARKERS = ("skip locked",)
QUEUE_CORROBORATION_SQL_MARKERS = ("skip locked", "for update")

# --- persistence behaviour (corroboration for the `memory` name signature) ---

#: ``<module>.<attr>`` calls that put bytes somewhere durable.
DURABLE_WRITE_CALLS: dict[str, frozenset[str]] = {
    "os": frozenset({"replace", "rename", "fdopen", "write", "link", "symlink"}),
    "shutil": frozenset({"copyfile", "copy", "copy2", "copyfileobj", "move", "copytree"}),
    "json": frozenset({"dump"}),
    "pickle": frozenset({"dump"}),
    "csv": frozenset({"writer", "DictWriter"}),
    "tempfile": frozenset({"mkstemp", "NamedTemporaryFile", "TemporaryFile"}),
}

#: ``pathlib.Path`` write methods. Matched on the attribute name alone: the name
#: is distinctive enough that a false positive would have to be a deliberate
#: imitation, and requiring the receiver to resolve to a `Path` would miss
#: every `self._file.write_text(...)`.
PATH_WRITE_ATTRS = frozenset({"write_text", "write_bytes"})

_WRITE_MODE_CHARS = frozenset("wax+")

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _is_excluded_part(part: str) -> bool:
    return (
        part in EXCLUDED_DIR_NAMES
        or part.startswith((".venv", "venv"))
        or part == "site-packages"
    )


def iter_python_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every ``*.py`` under ``root``, skipping excluded directories."""
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if any(_is_excluded_part(part) for part in rel_parts):
            continue
        files.append(path)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_test_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    return "tests" in parts or parts[-1].startswith("test_")


def _is_presentation(rel_path: str) -> bool:
    return rel_path.startswith(PRESENTATION_PREFIXES)


def _top_level(module_name: str) -> str:
    return module_name.split(".", 1)[0]


# ---------------------------------------------------------------------------
# Gate 1: core-internals import ban
# ---------------------------------------------------------------------------


def _is_banned_module_name(name: str) -> bool:
    top = _top_level(name)
    return top == CORE_TOP_LEVEL


def _is_forbidden_aios_module(name: str, rel_path: str) -> bool:
    top = _top_level(name)
    if top == CORE_TOP_LEVEL:
        return True
    adapter_path = PUBLIC_AIOS_TOP_LEVELS.get(top)
    if adapter_path is None:
        return False
    # Even the adapter cannot couple to generated/private submodules of a public
    # distribution: every consumed symbol must be a documented top-level export.
    return rel_path != adapter_path or name != top


def _literal_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def find_banned_aios_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, description) for every ``aios`` core import in ``tree``.

    ``aios_sdk`` (the public SDK namespace) is explicitly allowed; only the
    core package ``aios`` and its submodules are banned.
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned_module_name(alias.name):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) are inside this repo by definition.
            if node.level == 0 and node.module and _is_banned_module_name(node.module):
                violations.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Call):
            literal: str | None = None
            func = node.func
            is_dynamic_import = (
                isinstance(func, ast.Name) and func.id == "__import__"
            ) or (isinstance(func, ast.Attribute) and func.attr == "import_module")
            if is_dynamic_import and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    literal = first.value
            if literal is not None and _is_banned_module_name(literal):
                violations.append((node.lineno, f"dynamic import of {literal!r}"))
    return violations


def find_forbidden_aios_imports(
    tree: ast.AST, rel_path: str
) -> list[tuple[int, str]]:
    """Find core imports and SDK imports outside the sole public adapter."""
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_aios_module(alias.name, rel_path):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level == 0
                and node.module
                and _is_forbidden_aios_module(node.module, rel_path)
            ):
                violations.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Call):
            literal: str | None = None
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
                and node.args
            ):
                literal = _literal_string(node.args[0])
            elif isinstance(node.func, ast.Name) and node.func.id == "__import__" and node.args:
                literal = _literal_string(node.args[0])
            if literal and _is_forbidden_aios_module(literal, rel_path):
                violations.append((node.lineno, f"dynamic import {literal}"))
    return violations


# ---------------------------------------------------------------------------
# Gate 2: anti-engine growth detector
# ---------------------------------------------------------------------------


def _dynamic_import_target(node: ast.AST) -> str | None:
    """The literal module name of an ``import_module``/``__import__`` call.

    A driver reached through ``importlib`` is the same driver; only a
    non-literal argument is genuinely beyond static analysis.
    """
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    is_dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )
    return _literal_string(node.args[0]) if is_dynamic else None


def _module_bindings(tree: ast.AST) -> dict[str, str]:
    """Local name -> top-level module it is bound to.

    Covers ``import x``, ``import x as y`` and ``y = importlib.import_module("x")``,
    so a call site can be attributed to the module it actually reaches rather
    than to whatever the variable happens to be called.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or _top_level(alias.name)] = _top_level(alias.name)
        elif isinstance(node, ast.Assign):
            target = _dynamic_import_target(node.value)
            if target is None:
                continue
            for assigned in node.targets:
                if isinstance(assigned, ast.Name):
                    bindings[assigned.id] = _top_level(target)
    return bindings


def _import_categories(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(categories, local aliases of ``subprocess``/``os``) from import nodes."""
    categories: set[str] = set()
    spawn_capable_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level(alias.name)
                for category, modules in IMPORT_SIGNATURES.items():
                    if top in modules:
                        categories.add(category)
                if top in {"subprocess", "os"}:
                    spawn_capable_aliases.add(alias.asname or _top_level(alias.name))
        elif (dynamic := _dynamic_import_target(node)) is not None:
            top = _top_level(dynamic)
            for category, modules in IMPORT_SIGNATURES.items():
                if top in modules:
                    categories.add(category)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = _top_level(node.module)
            for category, modules in IMPORT_SIGNATURES.items():
                if top in modules:
                    categories.add(category)
            if top == "subprocess":
                for alias in node.names:
                    if alias.name == "Popen":
                        categories.add("orchestration")
            if top == "multiprocessing":
                categories.add("orchestration")
            if top == "os":
                for alias in node.names:
                    if alias.name.startswith(OS_SPAWN_ATTRS_PREFIXES):
                        categories.add("orchestration")
    return categories, spawn_capable_aliases


def _call_categories(tree: ast.AST, spawn_capable_aliases: set[str]) -> set[str]:
    """Process-spawning call sites: ``subprocess.Popen``, ``os.fork``/``spawn*``."""
    categories: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        base = func.value
        if not (isinstance(base, ast.Name) and base.id in spawn_capable_aliases):
            continue
        if func.attr == "Popen" or func.attr.startswith(OS_SPAWN_ATTRS_PREFIXES):
            categories.add("orchestration")
    return categories


def _persists_data(tree: ast.AST) -> bool:
    """Whether this file actually keeps state somewhere durable.

    This is the corroboration the ``memory`` *name* signature needs (see the
    module docstring). Three shapes count, and they are the three ways a file
    can own persistence rather than merely be handed a connection:

    * it imports a database driver -- statically, under an alias, or through
      ``importlib`` with a literal name (checked in :func:`_import_categories`);
    * it calls into a driver it bound itself -- ``pg.connect(...)``,
      ``sqlite3.connect(...)``, ``importlib.import_module("psycopg").connect()``;
    * it writes durably to the filesystem -- an atomic temp-file swap, an
      append, a copy. A JSON/JSONL store is a persistence engine even though it
      never imports a driver, and treating "driver" as the whole definition
      would let one out of the gate entirely.

    What deliberately does *not* count is executing SQL on a connection someone
    else opened. That is delegation, not ownership: the engine is where the
    driver is. Counting it would flag the SQL-rendering and grant modules that
    the boundary ruling requires to stay in the control plane, while catching no
    engine that the driver rule misses.
    """
    bindings = _module_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            # `open(path, "w")` and friends; a read-only open is not persistence.
            if func.id == "open" and _opens_for_writing(node):
                return True
            continue
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr in PATH_WRITE_ATTRS:
            return True
        # `Path.open` reached as an attribute -- `self._path.open("a")`,
        # `Path(x).open("w")`. Checked before the receiver is resolved, because
        # a store that owns its file typically holds it on `self` and never
        # names a module at the call site. Missing this let an append-only
        # JSONL store in a directory called `db/` classify as no engine at all,
        # which is the exact loosening the corroboration rule must not cause.
        # Still mode-gated: a read-only `.open()` is not persistence.
        # `Path.open` takes the mode as its *first* positional argument, unlike
        # the builtin, where it is the second.
        if func.attr == "open" and _opens_for_writing(node, mode_index=0):
            return True
        base = func.value
        if not isinstance(base, ast.Name):
            continue
        module = bindings.get(base.id)
        if module is None:
            continue
        if module in DB_DRIVER_MODULES:
            return True
        if func.attr in DURABLE_WRITE_CALLS.get(module, frozenset()):
            return True
    return False


def _opens_for_writing(node: ast.Call, *, mode_index: int = 1) -> bool:
    """Whether an `open` call asks for a writable mode.

    `mode_index` differs by call form: the builtin `open(file, mode)` carries it
    second, `Path.open(mode)` first. Getting that wrong silently answers "not a
    write" for every attribute-form open, which is how an append-only store
    escaped this check once already.
    """
    mode_nodes = list(node.args[mode_index : mode_index + 1])
    mode_nodes += [kw.value for kw in node.keywords if kw.arg == "mode"]
    for mode in mode_nodes:
        literal = _literal_string(mode)
        if literal and set(literal) & _WRITE_MODE_CHARS:
            return True
    return False


def _runs_queue_operations(
    tree: ast.AST,
    tokens: frozenset[str] = QUEUE_ENGINE_TOKENS,
    sql_markers: tuple[str, ...] = QUEUE_ENGINE_SQL_MARKERS,
) -> bool:
    """Whether this file hands work out, rather than merely storing it.

    Two signals, both structural: a **defined** function whose name carries a
    work-handout verb, and SQL that only a handout path writes.

    Deliberately *not* signals: `INSERT`, `SELECT ... ORDER BY`, `DELETE`. That
    is a repository over queue rows, which a control plane is allowed to own;
    the engine is whatever decides who gets the next one.

    Acknowledged limits, stated because a reader auditing coverage will
    otherwise assume they are covered: a *called* but not defined operation
    (`engine.dequeue()`) and an operation bound by assignment
    (`dequeue = lambda ...`) are not detected. Both were true of the previous
    rule as well; neither is a regression, and closing them needs
    cross-module resolution this single-file scanner does not have.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if set(_TOKEN_SPLIT.split(node.name.lower())) & tokens:
                return True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if any(marker in lowered for marker in sql_markers):
                return True
    return False


def _corroborates_queue_name(tree: ast.AST) -> bool:
    """The wide vocabulary, affordable because the name already narrowed the set."""
    return _runs_queue_operations(
        tree, QUEUE_CORROBORATION_TOKENS, QUEUE_CORROBORATION_SQL_MARKERS
    )


# --- audit-engine behaviour (corroboration for the `audit` name signature) --

#: The one base class every declarative table-mirror module builds on
#: (`command_center/db/table_mirror.py`). A class that names it and adds
#: nothing else is a table declaration, not an engine.
_MIRROR_BASE_CLASS = "PostgresTableMirror"


def _defines_a_function(tree: ast.AST) -> bool:
    """Whether this file defines any function or method of its own.

    Every real audit-engine file matched by the name signature — the checks,
    the runner, the registry, the Wave-2 write service, the runtime tables —
    defines at least one. A pure table-mirror module defines none: its
    `MirroredTable` instances, `PostgresTableMirror` subclasses and
    `divergence_against(...)` calls are all assignments and class bodies of one
    line, never a `def`.
    """
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(tree)
    )


def _defines_a_non_mirror_class(tree: ast.AST) -> bool:
    """Whether this file defines a class that is not a bare mirror declaration.

    `class PostgresAuditRunMirror(PostgresTableMirror): spec = AUDIT_RUN` is
    one class-level assignment naming a table that already exists — the same
    repository shape `test_a_plain_repository_adapter_passes` already accepts
    for `queue`. Any other class — a Pydantic schema, a value object, a class
    with no base at all — is not that shape, whatever the file is named.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {
            base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
            for base in node.bases
        }
        if _MIRROR_BASE_CLASS not in base_names:
            return True
    return False


def _behaves_like_an_audit_engine(tree: ast.AST) -> bool:
    """The corroboration the `audit` name signature needs (see module docstring).

    Two positive shapes, either enough on its own: a defined function, or a
    class that is not a trivial `PostgresTableMirror` declaration. A module
    with neither — only table declarations and reconciliation wiring over data
    that already exists — adds no behaviour of its own, so the name alone does
    not make it an engine.
    """
    return _defines_a_function(tree) or _defines_a_non_mirror_class(tree)


#: Category -> the behaviour that has to corroborate its name signature.
_CORROBORATION: dict[str, Callable[[ast.AST], bool]] = {
    "memory": _persists_data,
    "queue": _corroborates_queue_name,
    "audit": _behaves_like_an_audit_engine,
}


def _name_categories(rel_path: str) -> set[str]:
    """Engine-named path segments (skipped for pure presentation layers)."""
    if _is_presentation(rel_path):
        return set()
    categories: set[str] = set()
    segments = rel_path.lower().removesuffix(".py").split("/")
    # The repository/package roots themselves are not signal-bearing names.
    tokens = {
        token
        for segment in segments[1:] or segments
        for token in _TOKEN_SPLIT.split(segment)
        if token
    }
    stem = segments[-1]
    for category, signature_tokens in NAME_TOKEN_SIGNATURES.items():
        if tokens & signature_tokens:
            categories.add(category)
    for category, substrings in NAME_SUBSTRING_SIGNATURES.items():
        if any(sub in stem for sub in substrings):
            categories.add(category)
    return categories


def classify_engine_categories(rel_path: str, tree: ast.AST) -> set[str]:
    """All engine categories ``rel_path`` matches (empty set = not an engine)."""
    categories: set[str] = set()
    if rel_path.startswith(FROZEN_RUNTIME_PACKAGE + "/"):
        categories.add("runtime_package")
    import_cats, spawn_aliases = _import_categories(tree)
    categories |= import_cats
    categories |= _call_categories(tree, spawn_aliases)

    name_cats = _name_categories(rel_path)
    categories |= name_cats - CORROBORATED_NAME_CATEGORIES
    # A `memory` or `queue` name is a question, not a verdict. It becomes one
    # only if the file also *behaves* like the thing it is named after: a path
    # segment says where something lives, not what it does, and a file whose
    # name is its only engine signal is a file the gate would freeze for being
    # called `db` or `queue`.
    for category in name_cats & CORROBORATED_NAME_CATEGORIES:
        if _CORROBORATION[category](tree):
            categories.add(category)
    # Queue-engine behaviour classifies regardless of the filename. This is the
    # half the old name-only rule missed entirely: it flagged an adapter called
    # `queue_store.py` while a hand-rolled claim/lease loop in a file named
    # anything else went unseen. Corroborating the name without adding this
    # would have been a straight reduction in control.
    if _runs_queue_operations(tree):
        categories.add("queue")
    return categories


def compute_engine_inventory() -> dict[str, list[str]]:
    """Current detector output: rel path → sorted engine categories.

    Test files are out of scope (tests exercise the frozen engines; they do not
    ship capability), but they remain fully covered by the import ban.
    """
    inventory: dict[str, list[str]] = {}
    for path in iter_python_files():
        rel_path = _rel(path)
        if _is_test_path(rel_path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        categories = classify_engine_categories(rel_path, tree)
        if categories:
            inventory[rel_path] = sorted(categories)
    return inventory


def load_baseline() -> dict[str, list[str]]:
    raw = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    return {path: sorted(categories) for path, categories in raw["entries"].items()}


def diff_against_baseline(
    inventory: dict[str, list[str]], baseline: dict[str, list[str]]
) -> list[str]:
    """Human-readable drift lines; empty list means the gate is green."""
    problems: list[str] = []
    for rel_path in sorted(inventory):
        current = set(inventory[rel_path])
        frozen = set(baseline.get(rel_path, []))
        if rel_path not in baseline:
            problems.append(
                f"NEW ENGINE MODULE: {rel_path} matches frozen categories "
                f"{sorted(current)} but is not in the baseline"
            )
        elif current - frozen:
            problems.append(
                f"ENGINE GROWTH: {rel_path} gained categories "
                f"{sorted(current - frozen)} (baseline: {sorted(frozen)})"
            )
    for rel_path in sorted(set(baseline) - set(inventory)):
        problems.append(
            f"STALE BASELINE ENTRY: {rel_path} no longer matches any engine "
            "signature (or was removed) — shrink the baseline to keep the "
            "snapshot equal to reality"
        )
    for rel_path in sorted(set(baseline) & set(inventory)):
        lost = set(baseline[rel_path]) - set(inventory[rel_path])
        if lost:
            problems.append(
                f"STALE BASELINE CATEGORIES: {rel_path} no longer matches "
                f"{sorted(lost)} — shrink the baseline entry"
            )
    return problems


def _write_baseline(inventory: dict[str, list[str]]) -> None:
    payload = {
        "policy": (
            "Frozen inventory of AICC-native subsystems overlapping AIOS Core "
            "domains (queue/orchestration/authz/audit/memory). These predate "
            "ADR-0008 (this repo) / ADR-0015 (aios repo) and are frozen until "
            "their convergence into AIOS Core (post-AIOS-CORE-ACCEPTED): they "
            "keep running, but growth is prohibited — no new files in these "
            "categories, no new engine categories in existing files. Any edit "
            "to this file is a reviewed architectural decision; see "
            "docs/AIOS_BOUNDARY.md for the procedure."
        ),
        "references": [
            "docs/AIOS_BOUNDARY.md",
            "docs/adr/0008-aios-first-control-plane-boundary.md",
            "aios repo: ADR-0015 (single-core doctrine), docs/AIOS_CORE_ACCEPTANCE.md AC-01",
        ],
        "entries": {path: inventory[path] for path in sorted(inventory)},
    }
    BASELINE_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    inventory = compute_engine_inventory()
    if "--write-baseline" in argv:
        _write_baseline(inventory)
        print(f"wrote {len(inventory)} entries to {_rel(BASELINE_FILE)}")
        return 0
    if not BASELINE_FILE.exists():
        print("no baseline yet; run with --write-baseline to create it")
        return 1
    problems = diff_against_baseline(inventory, load_baseline())
    if problems:
        print("\n".join(problems))
        return 1
    print(f"boundary fitness green: {len(inventory)} frozen entries, no drift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
