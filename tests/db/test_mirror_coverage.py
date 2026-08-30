"""Nothing checked that every table *has* a mirror — only that mirrors behave.

`test_mirror_contract.py` enrols every declared mirror and applies four checks
to it, which is a complete rule about the mirrors that exist and says nothing
about the ones that do not. The only accidental sentry was a hand-written `33`
in the correspondence suite, and it was already wrong: the shared contract
carries **32**, because `queue_entry` was deliberately moved to a contract of
its own (whole-list replacement, no `upsert` — `table_mirror.py` says so). A
count that disagrees with reality by one for a documented reason cannot be
distinguished from a count that disagrees by one because a table was forgotten.

So a table can be added, pass every green gate, and stay unmirrored until the
authority is switched over — the moment the omission is most expensive. This
module closes that: every table in the schema is either mirrored or **signed out
of scope**, with a reason and the task that owns it, and the same rule applies
to tables created in the runtime database that have no PostgreSQL target at all.

Both gates are proved to bite: `test_the_coverage_gate_fails_on_...` feeds each
one a table that is neither mirrored nor declared and asserts it is reported. A
coverage gate that has never been shown to fail is a coverage gate that
guarantees nothing.

A signed exclusion can itself lie. `queue_entry`'s entry says it is mirrored by
`PostgresQueueMirror` rather than unmirrored — prose nothing re-checks, so an
unrelated class at that name, or a class edited to stop touching `queue_entry`
on just one of its two paths, would still leave every gate above green.
`BESPOKE_MIRRORS` and `test_every_bespoke_mirror_is_real` close that: the named
class's read and write paths are each run against their own connection that
only records what it was asked to do, and the SQL each path actually sends is
checked against the table the exclusion claims it covers — separately per
path, because a table present in one path's statements says nothing about
whether the other path still writes it — also proved to bite, the same way.

No database is needed here, deliberately — the declarations are the thing under
test, and they must stay checked on a machine with no PostgreSQL.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from command_center.db import roles
from command_center.db.table_mirror import PostgresTableMirror
from tests.db.mirror_discovery import mirror_classes

ROOT = Path(__file__).resolve().parents[2]
COMMAND_CENTER = ROOT / "command_center"
RUNTIME_DB_PACKAGE = COMMAND_CENTER / "runtime" / "db"

#: The ledger is the runner's, not a domain table, on both sides.
LEDGER_TABLES = frozenset({"schema_migration", "schema_version"})

#: One SQL identifier, quoted or bare. SQLite — the runtime store's engine —
#: accepts double quotes, backticks, and square brackets interchangeably with
#: an unquoted name, and a declaration can lead a table name with a
#: `.`-qualifying schema. A rule that only recognised the bare, unqualified
#: spelling would let `CREATE TABLE "widget" (` or `CREATE TABLE main.widget
#: (` add a table invisible to this scan while the hard-coded expectations
#: elsewhere stayed green.
_IDENTIFIER = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)'

#: The trailing `(` is what keeps prose out. `schema.py` discusses
#: "`CREATE TABLE IF NOT EXISTS`," inside a docstring, and a rule that stops
#: at the identifier reads the word `IF` as a table nobody declared — a gate
#: failing on its own documentation, which is how a gate gets weakened.
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?:{_IDENTIFIER}\s*\.\s*)?({_IDENTIFIER})\s*\(",
    re.IGNORECASE,
)


def _unquote(identifier: str) -> str:
    """Strip one layer of SQL quoting, whichever style `_CREATE_TABLE` matched."""
    if identifier[0] in "\"`[":
        return identifier[1:-1]
    return identifier


@dataclass(frozen=True)
class Exclusion:
    """A signed decision that a table is not mirrored, and why.

    Both fields are required and checked. A reason without an owning task is an
    opinion that nothing will revisit; a task without a reason makes the next
    reader open the backlog to find out what was decided here.
    """

    reason: str
    task: str


# ---------------------------------------------------------------------------
# Signed exclusions — schema tables with no mirror
# ---------------------------------------------------------------------------

UNMIRRORED_SCHEMA_TABLES: dict[str, Exclusion] = {
    "run_finalization_claim": Exclusion(
        reason=(
            "The PostgreSQL target is reserved for a future native authority, "
            "but the current SQLite claim is deliberately host-local: PID and "
            "process identity have meaning only on that execution host and a "
            "best-effort mirror cannot preserve its atomic fencing semantics. "
            "Cutover requires zero open claims and unfinalized runs."
        ),
        task="VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY",
    ),
    # The backlog store (0005, BO-S1) is PostgreSQL-native authority from
    # birth, like the work_item family below: there is no SQLite source to
    # dual-write from — the incumbent it replaces is a Markdown FILE, and its
    # reconciliation path is the importer (backlog_store.import_markdown),
    # not the mirror machinery. One exclusion per table so a future table in
    # the family still has to sign in on its own.
    "backlog_scan_cursor": Exclusion(
        reason=(
            "PostgreSQL-native tick-scheduler state from birth (0015): the "
            "persisted scan cursor exists only so the review/publish/merge "
            "tick windows advance atomically per invocation; there is no "
            "SQLite incumbent and nothing to dual-write."
        ),
        task="VOYN-OPS-AICC-PUBLISH-WINDOW-STARVATION",
    ),
    "backlog_task": Exclusion(
        reason=(
            "PostgreSQL-native authority from birth (BO-S1): the incumbent it "
            "replaces is the Markdown backlog file, reconciled by the importer, "
            "so there is no SQLite source for the mirror machinery to dual-write."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "backlog_dependency": Exclusion(
        reason=(
            "PostgreSQL-native (BO-S1): dependency edges exist only through the "
            "cycle-checked backlog_add_dependency function; no SQLite authority "
            "ever held them, so there is nothing for a mirror to copy from."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "backlog_evidence": Exclusion(
        reason=(
            "PostgreSQL-native (BO-S1): acceptance evidence rows are written only "
            "by backlog_record_evidence and never existed in SQLite; a mirror "
            "would invent a source rather than copy one."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "backlog_event": Exclusion(
        reason=(
            "PostgreSQL-native (BO-S1): the append-only audit trail written by "
            "the store's SECURITY DEFINER functions, the work_event idiom — "
            "born in PostgreSQL with no SQLite counterpart to mirror."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "backlog_writer_lease": Exclusion(
        reason=(
            "PostgreSQL-native (BO-S1): a writer-lease coordination row whose "
            "whole meaning is the transactional takeover protocol; outside "
            "PostgreSQL the row is not a lease, so mirroring it would be noise."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "backlog_task_remediation": Exclusion(
        reason=(
            "PostgreSQL-native (migration 0010): remediation lineage rows exist "
            "only through backlog_record_remediation, written the same "
            "transaction as the REJECTED transition they follow up on; no "
            "SQLite authority ever held them, so there is nothing to mirror."
        ),
        task="VOYN-W0-BACKLOG-ORCHESTRATOR",
    ),
    "queue_entry": Exclusion(
        reason=(
            "Mirrored under a contract of its own rather than the shared one: the "
            "queue synchronises by whole-list replacement (DELETE plus a bulk "
            "re-INSERT) and carries a `position` column the JSON authority does not "
            "have, so it has `replace_entries`/`list_entries` and no `upsert` at "
            "all. Folding it into `PostgresTableMirror` would produce one class "
            "saying something vague about both. Deliberate, pre-existing, and "
            "recorded here because it was previously visible only as an off-by-one "
            "in a hand-written count."
        ),
        task="VOYN-W0-AICC-QUEUE-ENTRY-PARITY",
    ),
    "work_item": Exclusion(
        reason=(
            "PostgreSQL-native authority: it has no SQLite source to mirror from. "
            "The mirror machinery exists to dual-write from the runtime SQLite "
            "authority to the PostgreSQL seam during the migration, and the claim "
            "protocol was never in SQLite — it is the authority from birth. A "
            "mirror declared for it would also fail the contract's own third "
            "property, which requires a reachable non-test caller, so a "
            "just-in-case mirror could not ship green either."
        ),
        task="VOYN-W0-AICC-SRV-04b",
    ),
    "work_attempt": Exclusion(
        reason=(
            "As `work_item`. Additionally it holds `claim_token_hash`, the "
            "capability itself, and is granted to no role; a mirror would move it "
            "through a second process and a second store."
        ),
        task="VOYN-W0-AICC-SRV-04b",
    ),
    "work_result": Exclusion(
        reason=(
            "As `work_item`: PostgreSQL-native, with no SQLite source to mirror "
            "from. It is written only inside the transaction that acknowledges "
            "the attempt it belongs to."
        ),
        task="VOYN-W0-AICC-SRV-04b",
    ),
    "work_event": Exclusion(
        reason=(
            "As `work_item`. It is the protocol's own audit, written inside the "
            "same transaction as the decision it records; mirroring it would give "
            "that audit a second, eventually-consistent copy."
        ),
        task="VOYN-W0-AICC-SRV-04b",
    ),
    "principal": Exclusion(
        reason=(
            "PostgreSQL-native authority with no SQLite source: an identity is "
            "half a database role, and the runtime store has no roles to mirror. "
            "`principal.db_role` is only meaningful against `session_user` on this "
            "server, so a copy in another engine would be a set of strings that "
            "authorise nothing while looking exactly like the rows that do."
        ),
        task="VOYN-W0-AICC-SRV-03",
    ),
    "principal_credential": Exclusion(
        reason=(
            "As `principal`, and more strongly: it holds `secret_hash`, the "
            "capability itself, and is granted to no role at all. A mirror would "
            "move the fleet's credential material through a second process and a "
            "second store, both outside the grant graph that currently makes it "
            "unreadable."
        ),
        task="VOYN-W0-AICC-SRV-03",
    ),
    "principal_event": Exclusion(
        reason=(
            "The protocol's own audit, written inside the same transaction as the "
            "decision it records — including the refusals, which are the half that "
            "matters. Mirroring it would give the theft alarm a second, "
            "eventually-consistent copy that can disagree with the first."
        ),
        task="VOYN-W0-AICC-SRV-03",
    ),
    "enrollment_ticket": Exclusion(
        reason=(
            "As `principal_credential`: it holds `ticket_hash` and is granted to "
            "no role. A ticket is also short-lived by construction, measured in "
            "minutes, so an eventually-consistent copy of one would be a record of "
            "capabilities that have already expired."
        ),
        task="VOYN-W0-AICC-SRV-03",
    ),
    "worker_host_fingerprint": Exclusion(
        reason=(
            "PostgreSQL-native, and deliberately unreachable from the component "
            "most likely to be compromised. It is the record that reveals a clone, "
            "so a second copy maintained by a dual-writing process would be a "
            "second place that evidence could be altered or lost."
        ),
        task="VOYN-W0-AICC-SRV-03",
    ),
}


# ---------------------------------------------------------------------------
# Bespoke mirrors — proving a signed exclusion's claim, not just its prose
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BespokeMirror:
    """Where a table's non-`PostgresTableMirror` mirror lives, and how to prove it.

    A table can be signed out of `_mirrored_tables()` in `UNMIRRORED_SCHEMA_TABLES`
    because it is mirrored by hand-written machinery instead of a
    `PostgresTableMirror` subclass — see `queue_entry`'s entry for why. That
    reason is prose a reviewer reads once and nothing re-checks it: an
    unrelated class could be substituted at the same import path, or the real
    class could be edited to touch a different table, and a check that stopped
    at "the symbol exists and is not a `PostgresTableMirror`" would still pass
    either way. `mutates` and `reads` name the methods this suite actually
    calls, against a connection that only records what it is asked to run, so
    the SQL the class emits is checked against `table` rather than trusted.
    """

    module: str
    class_name: str
    table: str
    mutates: str
    reads: str
    task: str


BESPOKE_MIRRORS: dict[str, BespokeMirror] = {
    "queue_entry": BespokeMirror(
        module="command_center.db.queue_store",
        class_name="PostgresQueueMirror",
        table="queue_entry",
        mutates="replace_entries",
        reads="list_entries",
        task="VOYN-W0-AICC-QUEUE-ENTRY-PARITY",
    ),
}


class _RecordingCursor:
    """Enough of a DB-API cursor to catch the SQL a bespoke mirror sends."""

    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._statements.append(sql)

    def executemany(self, sql: str, rows: object = ()) -> None:
        self._statements.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _RecordingConnection:
    """A connection that answers every call asked of it and remembers every
    statement it was told to run, so a bespoke mirror's real code path can be
    driven with no PostgreSQL server behind it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def transaction(self):
        return self

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.statements)


def _bespoke_mirror_statements(bespoke: BespokeMirror) -> dict[str, str]:
    """The SQL `bespoke`'s read and write paths send, kept as two recordings.

    Drives the class's own production code — not a description of it — against
    a connection that only records what it is asked to run. Each method gets
    its own connection and its own joined string, rather than one string for
    both: `replace_entries` dropping `queue_entry` while `list_entries` still
    names it (or the reverse) must show up as *that* method's statements
    missing the table, not be absorbed by the other method's statements still
    containing it. A single concatenated string cannot tell the two apart.
    """
    module = importlib.import_module(bespoke.module)
    cls = getattr(module, bespoke.class_name)
    statements: dict[str, str] = {}
    for method in (bespoke.reads, bespoke.mutates):
        connection = _RecordingConnection()
        instance = cls(connection_factory=lambda: connection)
        if method == bespoke.mutates:
            getattr(instance, method)([])
        else:
            getattr(instance, method)()
        statements[method] = "\n".join(connection.statements)
    return statements


class _DecoyBespokeMirror:
    """Same shape as `PostgresQueueMirror`, mentions no table at all.

    Exists only for `test_the_bespoke_mirror_gate_fails_when_it_stops_mirroring_its_table`,
    which needs a class that would satisfy "the symbol exists and is not a
    `PostgresTableMirror`" while having stopped mirroring anything.
    """

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory

    def list_entries(self) -> list:
        with self._factory() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchall()
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")


class _HalfDecoyBespokeMirror:
    """Still mirrors `queue_entry` on read, but its write path has gone quiet.

    Exists only for
    `test_the_bespoke_mirror_gate_fails_when_only_one_path_stops_mirroring_its_table`.
    A class shaped like this is exactly what a single concatenated
    read-plus-write string cannot catch: `queue_entry` is present overall
    because `list_entries` still says it, even though `replace_entries` no
    longer writes anything meaningful. Checking each method's statements
    separately is what turns this into a failure.
    """

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory

    def list_entries(self) -> list:
        with self._factory() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM queue_entry ORDER BY position ASC")
            cur.fetchall()
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")


@pytest.mark.parametrize(
    "bespoke", list(BESPOKE_MIRRORS.values()), ids=list(BESPOKE_MIRRORS)
)
def test_every_bespoke_mirror_is_real(bespoke: BespokeMirror) -> None:
    """The gate for `UNMIRRORED_SCHEMA_TABLES` entries that are not really
    unmirrored — they are mirrored by something this suite cannot discover on
    its own.

    Importing the named class and checking it is not a `PostgresTableMirror`
    proves only that *a* symbol exists at that path; an unrelated class could
    stand in for it just as well. This instead runs the class's own read and
    write paths and asserts the table named in the exclusion is the table the
    SQL it actually sends names — a rename or a swapped-in class is caught
    here instead of first showing up as a silently stale mirror in production.

    Checked one method at a time rather than against the two paths' combined
    output: a mirror only has to keep mirroring the table on *every* path it
    claims to cover, and a method that stopped naming the table must not be
    let off because the other method still does.
    """
    module = importlib.import_module(bespoke.module)
    cls = getattr(module, bespoke.class_name)
    assert not issubclass(cls, PostgresTableMirror), (
        f"{bespoke.class_name} is a PostgresTableMirror now — it belongs in the "
        "discovered contract (see mirror_discovery.mirror_classes), not a "
        "signed exclusion with a hand-maintained proof"
    )

    statements = _bespoke_mirror_statements(bespoke)
    for method in (bespoke.reads, bespoke.mutates):
        assert bespoke.table in statements[method], (
            f"{bespoke.class_name}.{method} never mentioned `{bespoke.table}` — "
            "it has stopped mirroring the table its exclusion in "
            "UNMIRRORED_SCHEMA_TABLES claims it covers"
        )


def test_the_bespoke_mirror_gate_fails_when_it_stops_mirroring_its_table() -> None:
    """The gate, shown to bite: a class that no longer touches its table is caught."""
    decoy = BespokeMirror(
        module=__name__,
        class_name="_DecoyBespokeMirror",
        table="queue_entry",
        mutates="replace_entries",
        reads="list_entries",
        task="VOYN-TEST",
    )
    statements = _bespoke_mirror_statements(decoy)
    assert decoy.table not in statements[decoy.reads]
    assert decoy.table not in statements[decoy.mutates]


def test_the_bespoke_mirror_gate_fails_when_only_one_path_stops_mirroring_its_table() -> None:
    """The blind spot a single concatenated string could not see, shown closed.

    `_HalfDecoyBespokeMirror` still names `queue_entry` on `list_entries`, so a
    check against the two methods' combined statements would find the table
    present and pass — even though `replace_entries` has quietly stopped
    writing anything meaningful. Checking each method's own statements is what
    catches this.
    """
    decoy = BespokeMirror(
        module=__name__,
        class_name="_HalfDecoyBespokeMirror",
        table="queue_entry",
        mutates="replace_entries",
        reads="list_entries",
        task="VOYN-TEST",
    )
    statements = _bespoke_mirror_statements(decoy)
    assert decoy.table in statements[decoy.reads]
    assert decoy.table not in statements[decoy.mutates]


def test_every_bespoke_mirror_has_a_gate_a_exclusion() -> None:
    """`BESPOKE_MIRRORS` proves an exclusion's claim; it does not replace one.

    A table here with no entry in `UNMIRRORED_SCHEMA_TABLES` would be proven
    "real" without ever having to justify why it is missing from
    `_mirrored_tables()` in the first place.
    """
    missing = sorted(set(BESPOKE_MIRRORS) - set(UNMIRRORED_SCHEMA_TABLES))
    assert missing == [], missing


# ---------------------------------------------------------------------------
# Signed exclusions — runtime-database tables with no PostgreSQL target
# ---------------------------------------------------------------------------

RUNTIME_TABLES_WITHOUT_A_TARGET: dict[str, Exclusion] = {
    "schema_version": Exclusion(
        reason=(
            "The runtime store's own migration ledger, not a domain table. Its "
            "counterpart on the PostgreSQL side is `schema_migration`, written by "
            "the migration runner; the two are the same role under different names "
            "and neither is data to be carried across."
        ),
        task="VOYN-W0-AICC-SRV-01b",
    ),
    "daily_audit_schedule": Exclusion(
        reason=(
            "Created ad hoc by the daily-audit daemon at start-up "
            "(`command_center/daily_audit.py`), not by the runtime store's "
            "`migrate()`, so it is invisible to the schema-correspondence suite — "
            "which compares what `migrate()` produces against the PostgreSQL "
            "schema and therefore never sees it. It has no PostgreSQL target. "
            "Recorded rather than silently absent: on an operating install these "
            "daemon tables are the ones actually carrying live rows, so an "
            "unnoticed omission would be a data-loss migration, not a cosmetic "
            "one."
        ),
        task="VOYN-W0-AICC-RUNTIME-ADHOC-TABLES",
    ),
    "daily_audit_campaign": Exclusion(
        reason=(
            "As `daily_audit_schedule`: created ad hoc outside `migrate()` by the "
            "daily-audit daemon, with no PostgreSQL target."
        ),
        task="VOYN-W0-AICC-RUNTIME-ADHOC-TABLES",
    ),
}


# ---------------------------------------------------------------------------
# The gates, as functions, so the tests can feed them a table that is neither
# ---------------------------------------------------------------------------


def _uncovered(
    tables: set[str], covered: set[str], declared: dict[str, Exclusion]
) -> list[str]:
    """Tables that are neither covered nor signed out of scope."""
    return sorted(tables - covered - set(declared))


def _stale(
    tables: set[str], covered: set[str], declared: dict[str, Exclusion]
) -> list[str]:
    """Declarations that have outlived their reason.

    An exclusion for a table that has since been mirrored, or that no longer
    exists, is a note nobody will read that quietly widens what the gate lets
    through next time.
    """
    return sorted(name for name in declared if name not in tables or name in covered)


def _schema_tables() -> set[str]:
    return set(roles.ALL_TABLES) - LEDGER_TABLES


def _mirrored_tables() -> set[str]:
    return set(mirror_classes())


def _modules_creating_runtime_tables() -> dict[Path, set[str]]:
    """`{module: tables it creates in the runtime database}`.

    Membership is "creates a table *and* is part of the runtime store or reaches
    it" — the second half matters, because several modules under
    `command_center/` create tables in databases of their own
    (`resolve_db_path()` returning a different file), and pulling those in would
    make this gate a demand that unrelated subsystems justify themselves here.

    Deliberately over-broad on the modules it inspects and narrow on the rule:
    the cost of a false positive is one signed line, and the cost of a false
    negative is the omission this whole module exists to prevent. The known case
    is asserted below, so the rule is not merely plausible.
    """
    found: dict[Path, set[str]] = {}
    for path in sorted(COMMAND_CENTER.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "CREATE TABLE" not in source.upper():
            continue
        reaches_runtime_store = RUNTIME_DB_PACKAGE in path.parents or (
            "command_center.runtime.db" in source
            or "from command_center.runtime import db" in source
        )
        if not reaches_runtime_store:
            continue
        found[path] = {_unquote(match).lower() for match in _CREATE_TABLE.findall(source)}
    return found


def _runtime_tables() -> set[str]:
    tables: set[str] = set()
    for created in _modules_creating_runtime_tables().values():
        tables |= created
    return tables


# ---------------------------------------------------------------------------
# Gate A — every schema table is mirrored or signed out of scope
# ---------------------------------------------------------------------------


def test_every_schema_table_is_mirrored_or_signed_out_of_scope() -> None:
    uncovered = _uncovered(
        _schema_tables(), _mirrored_tables(), UNMIRRORED_SCHEMA_TABLES
    )
    assert uncovered == [], (
        "tables with neither a mirror nor a signed exclusion: "
        f"{uncovered}. Declare a `PostgresTableMirror` subclass for each, or add "
        "an entry to UNMIRRORED_SCHEMA_TABLES with the reason and the owning task."
    )


def test_no_mirror_exclusion_outlives_its_reason() -> None:
    stale = _stale(_schema_tables(), _mirrored_tables(), UNMIRRORED_SCHEMA_TABLES)
    assert stale == [], f"exclusions for tables that are mirrored or gone: {stale}"


def test_the_coverage_gate_fails_on_a_table_that_is_neither() -> None:
    """The gate, shown to bite.

    A coverage rule that has only ever been observed passing is indistinguishable
    from one that computes the empty set. This runs it against a schema carrying
    a table nobody declared anything about.
    """
    tables = _schema_tables() | {"a_table_nobody_declared"}
    assert _uncovered(tables, _mirrored_tables(), UNMIRRORED_SCHEMA_TABLES) == [
        "a_table_nobody_declared"
    ]

    # And the same table, once signed, passes — so the gate is answering the
    # question it claims to and not simply rejecting anything unfamiliar.
    signed = dict(
        UNMIRRORED_SCHEMA_TABLES,
        a_table_nobody_declared=Exclusion(reason="test double", task="VOYN-TEST"),
    )
    assert _uncovered(tables, _mirrored_tables(), signed) == []


def test_the_staleness_gate_fails_on_an_exclusion_for_a_mirrored_table() -> None:
    covered = _mirrored_tables() | {"queue_entry"}
    assert _stale(_schema_tables(), covered, UNMIRRORED_SCHEMA_TABLES) == [
        "queue_entry"
    ]


# ---------------------------------------------------------------------------
# Gate B — runtime tables with no PostgreSQL target
# ---------------------------------------------------------------------------


def test_the_runtime_scan_finds_the_tables_created_outside_migrate() -> None:
    """The positive control for the scanner, without which Gate B proves nothing.

    A scan that matched no module would report perfect coverage. The known case
    is the daily-audit daemon, which creates two tables at start-up rather than
    through the runtime store's `migrate()` — exactly the shape that escapes the
    schema-correspondence suite.
    """
    discovered = _modules_creating_runtime_tables()
    daemon = COMMAND_CENTER / "daily_audit.py"
    assert daemon in discovered, sorted(p.name for p in discovered)
    assert discovered[daemon] == {"daily_audit_schedule", "daily_audit_campaign"}

    # And the runtime store itself, so the scan is not finding only the outlier.
    assert any(RUNTIME_DB_PACKAGE in path.parents for path in discovered)


def test_the_compliance_stores_are_not_dragged_into_this_gate() -> None:
    """They keep their own database files, so they are not runtime tables.

    Stated as a test because the scanning rule is the kind that quietly widens:
    matching on `CREATE TABLE` alone would demand that several unrelated
    subsystems justify themselves in this file, and the resulting wall of
    signatures would make the two entries that matter unreadable.
    """
    discovered = {path.name for path in _modules_creating_runtime_tables()}
    assert "aml_store.py" not in discovered
    assert "alert_store.py" not in discovered


def test_the_create_table_scan_recognizes_quoting_and_schema_qualification() -> None:
    """The positive control `_CREATE_TABLE` needs: SQLite accepts every one of
    these spellings for the same statement, so a scan that recognised only the
    bare, unqualified form would let a table declared with any of the others
    slip past this gate uncovered while `test_every_runtime_table_has_a_...`
    kept passing.
    """
    samples = {
        "bare": "CREATE TABLE widget (id INTEGER PRIMARY KEY)",
        "if_not_exists": "CREATE TABLE IF NOT EXISTS widget (id INTEGER PRIMARY KEY)",
        "double_quoted": 'CREATE TABLE "widget" (id INTEGER PRIMARY KEY)',
        "backtick_quoted": "CREATE TABLE `widget` (id INTEGER PRIMARY KEY)",
        "bracket_quoted": "CREATE TABLE [widget] (id INTEGER PRIMARY KEY)",
        "schema_qualified": "CREATE TABLE main.widget (id INTEGER PRIMARY KEY)",
        "schema_qualified_quoted": 'CREATE TABLE "main"."widget" (id INTEGER PRIMARY KEY)',
        "indented": "    CREATE TABLE\n        widget (\n            id INTEGER PRIMARY KEY)",
        "lowercase": "create table if not exists widget (id integer primary key)",
    }
    for label, source in samples.items():
        found = {_unquote(match).lower() for match in _CREATE_TABLE.findall(source)}
        assert found == {"widget"}, f"{label}: matched {found}"


def test_the_create_table_scan_does_not_read_prose_as_a_table() -> None:
    """The negative control the comment on `_CREATE_TABLE` already promises but
    nothing checked: a docstring that merely discusses `CREATE TABLE IF NOT
    EXISTS`, with no trailing `(`, must not be read as a declaration.
    """
    source = "This module says `CREATE TABLE IF NOT EXISTS` a lot, in prose."
    assert _CREATE_TABLE.findall(source) == []


def test_every_runtime_table_has_a_postgres_target_or_a_signed_exclusion() -> None:
    targets = set(roles.ALL_TABLES)
    uncovered = _uncovered(_runtime_tables(), targets, RUNTIME_TABLES_WITHOUT_A_TARGET)
    assert uncovered == [], (
        "runtime tables with no PostgreSQL target and no signed exclusion: "
        f"{uncovered}. Add the table to the migration set, or record it in "
        "RUNTIME_TABLES_WITHOUT_A_TARGET with the reason and the owning task."
    )


def test_no_runtime_exclusion_outlives_its_reason() -> None:
    stale = _stale(
        _runtime_tables(), set(roles.ALL_TABLES), RUNTIME_TABLES_WITHOUT_A_TARGET
    )
    assert stale == [], (
        f"exclusions for runtime tables that are gone or migrated: {stale}"
    )


def test_the_runtime_gate_fails_on_an_undeclared_ad_hoc_table() -> None:
    tables = _runtime_tables() | {"another_daemon_table"}
    assert _uncovered(
        tables, set(roles.ALL_TABLES), RUNTIME_TABLES_WITHOUT_A_TARGET
    ) == ["another_daemon_table"]


# ---------------------------------------------------------------------------
# The signatures themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "registry",
    [UNMIRRORED_SCHEMA_TABLES, RUNTIME_TABLES_WITHOUT_A_TARGET],
    ids=["schema", "runtime"],
)
def test_every_exclusion_is_signed(registry: dict[str, Exclusion]) -> None:
    """A reason and an owning task, both non-empty and both meaning something.

    The failure this prevents is an exclusion added under deadline with an empty
    string, which reads as a decision in the diff and is an omission in fact.
    """
    assert registry, "an empty registry would satisfy every check above"
    for table, exclusion in registry.items():
        assert len(exclusion.reason.split()) >= 10, (
            f"{table}: the reason is not a reason"
        )
        assert re.fullmatch(r"VOYN-[A-Z0-9-]+[a-z]?", exclusion.task), (
            f"{table}: {exclusion.task!r} is not a central backlog task id"
        )
