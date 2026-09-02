"""Architecture fitness gates for the AIOS boundary (AC-01 / ADR-0008 / ADR-0015).

These tests are the mechanical half of the AIOS Core acceptance criterion
AC-01 for this product repository: AI Command Center must be *mechanically
prevented* from growing a parallel queue, orchestration, authz, audit, or
memory engine, and must never reach into AIOS Core internals (only the public
SDK/API surface is allowed).

The scanner and its signatures live in ``tests/architecture/aios_boundary.py``;
the frozen inventory lives in ``tests/architecture/AIOS_BOUNDARY_BASELINE.json``;
the policy and the baseline-change procedure are documented in
``docs/AIOS_BOUNDARY.md``.
"""

from __future__ import annotations

import ast

from tests.architecture import aios_boundary as boundary


def test_aios_imports_are_confined_to_the_public_sdk_adapter():
    """No Python file in the repository imports ``aios`` core internals.

    Covers every ``*.py`` in the repo (application code, scripts, tests,
    packaging) — static imports and literal dynamic imports alike. The public
    SDK namespace ``aios_sdk`` is explicitly allowed.
    """
    violations: list[str] = []
    for path in boundary.iter_python_files():
        rel_path = path.relative_to(boundary.REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        for lineno, description in boundary.find_forbidden_aios_imports(tree, rel_path):
            violations.append(f"{rel_path}:{lineno}: {description}")
    assert not violations, (
        "AIOS Core internals must never be imported, and the public SDK "
        f"namespace may be imported only by {boundary.SDK_ADAPTER_PATH}. "
        "Consume AICC's TasksGateway contract everywhere else.\n" + "\n".join(violations)
    )


def test_engine_inventory_matches_frozen_baseline():
    """The frozen-engine inventory equals the baseline snapshot exactly.

    Growth direction (the AC-01 bar): a new file matching a queue /
    orchestration / authz / audit / memory signature, a new file inside
    ``command_center/runtime/``, or an existing file gaining a new engine
    category fails this test. Shrink direction: entries whose files stopped
    matching (retired or converged into AIOS) must be removed from the
    baseline, so the snapshot always equals reality. Either edit to
    ``AIOS_BOUNDARY_BASELINE.json`` is a reviewed architectural decision —
    see docs/AIOS_BOUNDARY.md for the procedure.
    """
    inventory = boundary.compute_engine_inventory()
    baseline = boundary.load_baseline()
    problems = boundary.diff_against_baseline(inventory, baseline)
    assert not problems, (
        "AIOS boundary drift detected (docs/AIOS_BOUNDARY.md, ADR-0008 / "
        "aios ADR-0015): AICC's engine-overlapping subsystems are frozen "
        "until convergence into AIOS Core; new engine capability belongs in "
        "AIOS, consumed via its API/SDK/events.\n" + "\n".join(problems)
    )


def test_scanner_semantics_are_stable():
    """The gate itself must not rot: sdk allowed, core banned, engines detected."""
    allowed = ast.parse("import aios_sdk\n")
    assert boundary.find_forbidden_aios_imports(allowed, boundary.SDK_ADAPTER_PATH) == []
    assert boundary.find_forbidden_aios_imports(allowed, "command_center/other.py")
    deep_sdk = ast.parse("from aios_sdk.client import Client\n")
    assert boundary.find_forbidden_aios_imports(deep_sdk, boundary.SDK_ADAPTER_PATH)

    banned = ast.parse(
        "import aios\n"
        "from aios.core import engine\n"
        "import importlib\n"
        "importlib.import_module('aios.queue')\n"
        "__import__('aios')\n"
    )
    assert len(boundary.find_banned_aios_imports(banned)) == 4

    db_engine = ast.parse("import sqlite3\n")
    assert "memory" in boundary.classify_engine_categories("command_center/new_cache.py", db_engine)

    spawner = ast.parse("import subprocess\nsubprocess.Popen(['sleep', '1'])\n")
    assert "orchestration" in boundary.classify_engine_categories(
        "command_center/new_helper.py", spawner
    )
    # Synchronous tool invocation is not an engine signature.
    runner = ast.parse("import subprocess\nsubprocess.run(['git', 'status'])\n")
    assert boundary.classify_engine_categories("command_center/new_helper.py", runner) == set()

    empty = ast.parse("")
    assert "runtime_package" in boundary.classify_engine_categories(
        "command_center/runtime/new_module.py", empty
    )
    # A `queue` name is corroborated now: the name alone is a question, the
    # work-handout behaviour is the answer.
    handout = ast.parse("def dequeue(conn):\n    ...\n")
    assert boundary.classify_engine_categories("command_center/retry_queue.py", empty) == set()
    assert "queue" in boundary.classify_engine_categories(
        "command_center/retry_queue.py", handout
    )
    # Presentation layers are exempt from name signatures only...
    assert boundary.classify_engine_categories("command_center/ui/queue_panel_v2.py", empty) == set()
    # ...but not from structural ones: an engine cannot hide in the UI layer.
    assert "memory" in boundary.classify_engine_categories(
        "command_center/ui/some_panel.py", db_engine
    )


def test_a_memory_name_alone_is_not_an_engine_signature():
    """`db`/`store`/`repository` name a file's subject as readily as its nature.

    A package directory called `db/` says where code lives, not whether the code
    inside owns an engine. Under a name-only rule the control plane could not
    keep *any* database-adjacent module — not even a docstring-only `__init__`
    or a module that renders SQL text — which is the opposite of a boundary: it
    stops describing what the code does and starts policing what it is called.
    """
    docstring_only = ast.parse('"""Package docstring."""\n__all__ = ["config"]\n')
    assert boundary.classify_engine_categories("command_center/db/__init__.py", docstring_only) == set()

    # Rendering SQL and executing it on a connection the caller opened is
    # delegation, not ownership: the engine is wherever the driver is.
    sql_renderer = ast.parse(
        "def render():\n"
        "    return ['CREATE ROLE app']\n"
        "def apply(conn):\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute(render()[0])\n"
    )
    assert boundary.classify_engine_categories("command_center/db/roles.py", sql_renderer) == set()

    # The same file with a driver of its own is an engine again.
    with_driver = ast.parse("import psycopg\ndef connect(dsn):\n    return psycopg.connect(dsn)\n")
    assert "memory" in boundary.classify_engine_categories("command_center/db/pool.py", with_driver)


def test_a_driver_is_detected_however_it_is_imported():
    """Alias and `importlib` are not hiding places."""
    aliased = ast.parse("import psycopg as pg\ndef go(dsn):\n    return pg.connect(dsn)\n")
    assert "memory" in boundary.classify_engine_categories("command_center/helper.py", aliased)

    dynamic = ast.parse(
        "import importlib\n"
        "driver = importlib.import_module('sqlite3')\n"
        "def go(path):\n"
        "    return driver.connect(path)\n"
    )
    assert "memory" in boundary.classify_engine_categories("command_center/helper.py", dynamic)

    underscore = ast.parse("__import__('psycopg2')\n")
    assert "memory" in boundary.classify_engine_categories("command_center/helper.py", underscore)

    # The connection *pool* package is its own distribution; a file that opens a
    # pool owns an engine just as much as one that opens a connection.
    pooled = ast.parse("from psycopg_pool import ConnectionPool\n")
    assert "memory" in boundary.classify_engine_categories("command_center/helper.py", pooled)


def test_a_file_backed_store_is_an_engine_even_with_no_driver():
    """JSON/JSONL persistence is persistence.

    If "engine" meant "imports a driver", the atomic-write JSON stores this
    repository actually runs on would drop out of the frozen inventory — the
    gate would get quieter while the code got no safer.
    """
    atomic_write = ast.parse(
        "import json\nimport os\n"
        "def save(path, data):\n"
        "    with open(path + '.tmp', 'w') as handle:\n"
        "        json.dump(data, handle)\n"
        "    os.replace(path + '.tmp', path)\n"
    )
    assert "memory" in boundary.classify_engine_categories(
        "command_center/storage.py", atomic_write
    )

    appender = ast.parse(
        "from pathlib import Path\n"
        "def record(path, line):\n"
        "    Path(path).write_text(line)\n"
    )
    assert "memory" in boundary.classify_engine_categories(
        "command_center/tasks_repository.py", appender
    )

    # Reading is not persisting: a module that only loads a store is not one.
    reader = ast.parse(
        "import json\n"
        "def load(path):\n"
        "    with open(path) as handle:\n"
        "        return json.load(handle)\n"
    )
    assert boundary.classify_engine_categories("command_center/db/config.py", reader) == set()


def test_corroboration_leaves_the_uncorroborated_categories_alone():
    """Two signatures are corroborated, not the gate.

    `orchestration`, `authz` and `audit` names still classify on the name
    alone: those tokens name what a module *is*, and unlike `memory` and
    `queue` they have no behavioural substitute that could replace the name
    without losing a home-grown engine.
    """
    empty = ast.parse("")
    for path, category in (
        ("command_center/task_scheduler.py", "orchestration"),
        ("command_center/rbac_rules.py", "authz"),
        ("command_center/audit_trail.py", "audit"),
    ):
        assert category in boundary.classify_engine_categories(path, empty), path


def test_the_second_public_distribution_is_confined_to_its_own_adapter():
    """`aios_db` is allowed on the same terms as `aios_sdk`: one file, top-level only.

    It is a published, independently versioned contract rather than Core
    internals, so importing it is not a boundary crossing — but letting every
    module import it would scatter the coupling that the adapter exists to keep
    in one reviewed place.
    """
    top_level = ast.parse("import aios_db\n")
    assert boundary.find_forbidden_aios_imports(top_level, boundary.DB_ADAPTER_PATH) == []
    assert boundary.find_forbidden_aios_imports(top_level, "command_center/db/pool.py")
    assert boundary.find_forbidden_aios_imports(top_level, boundary.SDK_ADAPTER_PATH)

    # Private submodules are off limits even to the adapter: the contract is
    # what the package exports at the top level, not what happens to be inside.
    deep = ast.parse("from aios_db.migrations import MigrationRunner\n")
    assert boundary.find_forbidden_aios_imports(deep, boundary.DB_ADAPTER_PATH)

    # Core remains banned everywhere, including from the db adapter.
    core = ast.parse("from aios.storage.sql import Database\n")
    assert boundary.find_forbidden_aios_imports(core, boundary.DB_ADAPTER_PATH)


def test_db_adapter_exports_the_frozen_advisory_lock_surface():
    """`command_center/db/adapter.py`'s public names are a reviewed, frozen list.

    The adapter's own docstring commits to this: "a blanket `from aios_db
    import *` would make every future addition to that library part of AICC's
    surface without anyone deciding so." Pinning the exact set here means a new
    `aios_db` re-export shows up as a diff to this test to review, not as a
    silent widening of the seam.
    """
    from command_center.db import adapter

    assert set(adapter.__all__) == {
        "AdvisoryLockError",
        "AdvisoryLockTimeout",
        "AiosDbError",
        "DB_CONTRACT",
        "Migration",
        "MigrationChecksumMismatch",
        "MigrationError",
        "MigrationRunner",
        "PoolError",
        "ProbeResult",
        "advisory_lock",
        "advisory_xact_lock",
        "check_connectivity",
        "discover",
        "lock_key",
        "open_pool",
        "pool_stats",
        "try_advisory_lock",
    }
    # `__all__` names what is actually importable, not just what is declared.
    for name in adapter.__all__:
        assert hasattr(adapter, name)


def test_a_store_that_opens_its_own_file_as_an_attribute_is_still_an_engine():
    """The reviewer's escape: `self._path.open("a")` instead of `open(path, "a")`.

    A store that owns its file usually holds it on `self` and never names a
    module at the call site, so checking only the builtin `open` let an
    append-only JSONL store in a directory called `db/` classify as no engine at
    all — while the old name-only rule had blocked it. That is the loosening the
    corroboration rule exists to avoid, and the docstring claimed it could not
    happen, which is worse than the gap itself.
    """
    attribute_open = ast.parse(
        "from pathlib import Path\n"
        "class EventStore:\n"
        "    def __init__(self, path):\n"
        "        self._path = path\n"
        "    def append(self, record):\n"
        "        with self._path.open('a', encoding='utf-8') as fh:\n"
        "            fh.write(record + '\\n')\n"
    )
    assert "memory" in boundary.classify_engine_categories(
        "command_center/db/eventstore.py", attribute_open
    )

    constructed = ast.parse(
        "from pathlib import Path\n"
        "def save(root, data):\n"
        "    with Path(root, 'tasks.json').open('w') as handle:\n"
        "        handle.write(data)\n"
    )
    assert "memory" in boundary.classify_engine_categories(
        "command_center/task_store.py", constructed
    )

    # Reading is still not persisting, in either form.
    reader = ast.parse(
        "def load(path):\n"
        "    with path.open('r') as handle:\n"
        "        return handle.read()\n"
    )
    assert boundary.classify_engine_categories("command_center/db/config.py", reader) == set()


def test_renaming_cannot_hide_queue_engine_behaviour():
    """A queue engine is caught by what it does, whatever the file is called.

    The old rule fired on a `queue` path token, so it flagged an adapter named
    `queue_store.py` while a hand-rolled claim loop in a file named anything
    else went unseen. Making the name signature semantic without this would
    have been a straight reduction in control; behaviour now classifies
    regardless of the filename, so the detector got stricter here, not looser.
    """
    innocuous_names = (
        "command_center/helpers.py",
        "command_center/portfolio_utils.py",
        "command_center/db/entries.py",
    )

    skip_locked = ast.parse(
        "def next_item(conn):\n"
        "    return conn.execute(\n"
        "        'SELECT id FROM work FOR UPDATE SKIP LOCKED LIMIT 1'\n"
        "    ).fetchone()\n"
    )
    handout = ast.parse("def dequeue(conn):\n    ...\ndef requeue(conn):\n    ...\n")

    for path in innocuous_names:
        assert "queue" in boundary.classify_engine_categories(path, skip_locked), path
        assert "queue" in boundary.classify_engine_categories(path, handout), path

    # ...and the name still counts when the behaviour is there too, so the
    # corroborated path cannot be used to launder an engine into a `queue` file.
    assert "queue" in boundary.classify_engine_categories(
        "command_center/retry_queue.py", handout
    )


def test_a_plain_repository_adapter_passes():
    """Storing and listing rows is what a control-plane repository does.

    `INSERT`, `SELECT ... ORDER BY`, `DELETE` over a table of queue rows is not
    a queue engine — the engine is whatever decides who gets the next one. This
    is the case the previous rule got wrong, and getting it wrong made the
    boundary unimplementable: the domain half that the ruling requires to stay
    in AICC could not be clean under any filename.
    """
    adapter = ast.parse(
        "class PostgresQueueMirror:\n"
        "    def replace_entries(self, conn, entries):\n"
        "        conn.execute('DELETE FROM queue_entry')\n"
        "        conn.executemany('INSERT INTO queue_entry (id) VALUES (%s)', entries)\n"
        "    def list_entries(self, conn):\n"
        "        return conn.execute(\n"
        "            'SELECT id FROM queue_entry ORDER BY position ASC'\n"
        "        ).fetchall()\n"
    )

    for path in ("command_center/queue_store.py", "command_center/db/queue_store.py"):
        assert boundary.classify_engine_categories(path, adapter) == set(), path


def test_a_queue_named_file_containing_a_real_engine_still_classifies():
    """The regression this rule must not have.

    Independent review built these three against the first attempt at a
    semantic `queue` signature and all three escaped, while the old name-only
    rule had caught them. A lease/claim/heartbeat loop in a file called
    `task_queue.py` is the most probable shape of the thing the name signature
    was protecting against.

    The cause was one vocabulary doing two jobs: the narrow set needed for the
    unconditional, tree-wide signal was also being used to corroborate a name,
    where a wide set is affordable because the name has already narrowed the
    candidates to a handful of files.
    """
    postgres_lease = ast.parse(
        "def claim_next(conn, owner):\n"
        "    row = conn.execute(\n"
        "        'SELECT id FROM work ORDER BY priority FOR UPDATE LIMIT 1'\n"
        "    ).fetchone()\n"
        "    conn.execute('UPDATE work SET state = %s WHERE id = %s', ('leased', row[0]))\n"
        "def heartbeat(conn, item_id):\n"
        "    ...\n"
        "def retry_failed(conn):\n"
        "    ...\n"
    )
    in_memory = ast.parse(
        "class Inflight:\n"
        "    def push(self, item):\n        ...\n"
        "    def pop_next(self):\n        ...\n"
        "    def ack_done(self, item):\n        ...\n"
    )
    redis_engine = ast.parse(
        "import redis\n"
        "def complete(client, item):\n    ...\n"
        "def retry(client, item):\n    ...\n"
    )

    assert "queue" in boundary.classify_engine_categories(
        "command_center/task_queue.py", postgres_lease
    )
    assert "queue" in boundary.classify_engine_categories(
        "command_center/queues/inflight.py", in_memory
    )
    assert "queue" in boundary.classify_engine_categories(
        "command_center/work_queue.py", redis_engine
    )


def test_the_wide_vocabulary_stays_behind_the_name_gate():
    """`claim`/`retry`/`next` are ordinary English, so they may only corroborate.

    Applied unconditionally they flagged twelve unrelated modules — auth
    claims, a FastAPI dispatch, UI panels. The wide set is safe *only* because
    a queue-shaped filename has already narrowed the candidates.
    """
    ordinary = ast.parse(
        "def claim(token):\n    ...\n"
        "def retry(request):\n    ...\n"
        "def next_page(cursor):\n    ...\n"
    )

    # Asserting `queue` specifically, not an empty set: `auth_tokens.py` carries
    # an `authz` name token of its own, and conflating "did not become a queue"
    # with "matched nothing" would make this test pass for the wrong reason.
    for path in ("command_center/auth_tokens.py", "command_center/web_client.py"):
        assert "queue" not in boundary.classify_engine_categories(path, ordinary), path


def test_the_mainstream_queue_verbs_are_covered():
    """Review found three engines that escaped the first vocabulary.

    None of them was an evasion: `poll`/`take`/`checkout` are what
    `java.util.concurrent`, the Go channel idiom and any worker pool call their
    operations. A queue author writes them without thinking about this detector,
    which is precisely why omitting them was a coverage gap rather than an
    acceptable limit — and why the owner's constraint (do not reduce control
    over hand-rolled queues) is not satisfied by a vocabulary that only covers
    the words we happened to think of first.
    """
    polling = ast.parse(
        "def poll(conn):\n"
        "    row = conn.execute('SELECT id FROM work ORDER BY priority LIMIT 1').fetchone()\n"
        "    conn.execute('UPDATE work SET state = %s', ('running',))\n"
        "def finish(conn, item):\n    ...\n"
        "def fail(conn, item):\n    ...\n"
    )
    handing_out = ast.parse(
        "class DispatchBox:\n"
        "    def submit(self, item):\n        ...\n"
        "    def checkout(self):\n        ...\n"
        "    def give_back(self, item):\n        ...\n"
    )
    redis_take = ast.parse(
        "import redis\n"
        "def take(client):\n    ...\n"
        "def settle(client, item):\n    ...\n"
    )

    assert "queue" in boundary.classify_engine_categories(
        "command_center/task_queue.py", polling
    )
    assert "queue" in boundary.classify_engine_categories(
        "command_center/queues/dispatchbox.py", handing_out
    )
    assert "queue" in boundary.classify_engine_categories(
        "command_center/work_queue.py", redis_take
    )

    # The widening stays behind the name gate: these verbs are ordinary English
    # and must not classify a module that is not named like a queue.
    for path in ("command_center/report_builder.py", "command_center/web_client.py"):
        assert "queue" not in boundary.classify_engine_categories(path, polling), path
