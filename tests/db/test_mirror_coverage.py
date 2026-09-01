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

No database is needed here, deliberately — the declarations are the thing under
test, and they must stay checked on a machine with no PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from command_center.db import roles
from command_center.db.queue_store import PostgresQueueMirror

from tests.db.mirror_discovery import mirror_classes

ROOT = Path(__file__).resolve().parents[2]
COMMAND_CENTER = ROOT / "command_center"
RUNTIME_DB_PACKAGE = COMMAND_CENTER / "runtime" / "db"

#: The ledger is the runner's, not a domain table, on both sides.
LEDGER_TABLES = frozenset({"schema_migration", "schema_version"})

#: The trailing `(` is what keeps prose out. `schema.py` discusses
#: "`CREATE TABLE IF NOT EXISTS`," inside a docstring, and a rule that stops
#: at the identifier reads the word `IF` as a table nobody declared — a gate
#: failing on its own documentation, which is how a gate gets weakened.
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE
)


@dataclass(frozen=True)
class Exclusion:
    """A signed decision that a table is not mirrored, and why.

    Both fields are required and checked. A reason without an owning task is an
    opinion that nothing will revisit; a task without a reason makes the next
    reader open the backlog to find out what was decided here.
    """

    reason: str
    task: str
    #: True for a table mirrored by hand outside `PostgresTableMirror` — Gate A
    #: would otherwise demand it as if nothing mirrored it at all. A table
    #: signed this way must also be registered in `BESPOKE_MIRRORS`, and the
    #: correspondence is checked both directions: see
    #: `test_every_bespoke_exclusion_matches_a_registered_mirror`.
    bespoke: bool = False


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
        bespoke=True,
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
        found[path] = {match.lower() for match in _CREATE_TABLE.findall(source)}
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
# Gate C — every table signed out of Gate A as "bespoke" really is mirrored
# ---------------------------------------------------------------------------
#
# A `bespoke=True` exclusion is a claim, not a mirror: without something that
# exercises the class it names, deleting `PostgresQueueMirror`'s body — or
# swapping in an unrelated class, or one that mirrors a different table, or one
# whose read and write halves have traded places — would leave `queue_entry`
# excused from Gate A and checked by nothing at all. That gap survived three
# review rounds under different shapes: a check that only proved the mapped
# symbol existed; a check that concatenated the read and mutation SQL before
# looking for the table, so either path alone could stop touching it
# unnoticed; a check that matched the table name as a bare substring, so
# `queue_entry_archive` or a quoted string literal satisfied it without a real
# reference; and a check that accepted the same `FROM|INTO|UPDATE|JOIN` shape
# for both methods, so a read that issued `DELETE` or a write reduced to a
# lone `SELECT` — or a write that kept its `DELETE` half and lost the `INSERT`
# — still passed.
#
# So this gate does not read source text at all. It calls the real methods
# against a connection that only records what was executed, per method, and
# checks the *recorded statement* against a verb the method must contain:
# `SELECT ... FROM <table>` for the read, `INSERT INTO <table>` or
# `UPDATE <table>` for the mutation. A table reference is required at a word
# boundary, so `<table>_archive` and `'<table>'` inside an unrelated statement
# both fail to satisfy it.


@dataclass(frozen=True)
class BespokeMirror:
    """A table mirrored by hand-written methods, not `PostgresTableMirror`.

    `mutation_args` is a full call, not a placeholder: `replace_entries([])`
    executes only the `DELETE`, because the `INSERT` is skipped when there are
    no rows, which would make the write check pass on a method call that never
    inserted anything. At least one row is required so the recorded statements
    actually include the write being verified.
    """

    cls: type
    table: str
    read_attr: str
    mutation_attr: str
    mutation_args: tuple


#: `{table: declaration}` — the bespoke half of the mirror inventory.
#: `test_every_bespoke_exclusion_matches_a_registered_mirror` ties this to
#: `UNMIRRORED_SCHEMA_TABLES` in both directions.
BESPOKE_MIRRORS: dict[str, BespokeMirror] = {
    "queue_entry": BespokeMirror(
        cls=PostgresQueueMirror,
        table="queue_entry",
        read_attr="list_entries",
        mutation_attr="replace_entries",
        mutation_args=(
            [
                {
                    "id": "gate-c-sample",
                    "task_id": "task-gate-c-sample",
                    "project": "gate-c",
                    "state": "queued",
                    "reason": None,
                    "run_id": None,
                    "added_at": "2026-08-13T00:00:00",
                    "evaluated_at": None,
                    "launched_at": None,
                }
            ],
        ),
    ),
}


class _RecordingCursor:
    """A cursor that records the SQL text of every statement it is given."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._log.append(sql)

    def executemany(self, sql: str, seq: object) -> None:
        self._log.append(sql)

    def fetchall(self) -> list:
        return []

    def fetchone(self) -> None:
        return None


class _RecordingTransaction:
    def __enter__(self) -> "_RecordingTransaction":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _RecordingConnection:
    """A stand-in for `psycopg`'s connection: no PostgreSQL, no side effects.

    Only `cursor()` and `transaction()` are implemented, because those are all
    `PostgresQueueMirror` — or a bespoke mirror shaped like it — asks of a
    connection. It never touches a network or a file, so this gate keeps the
    module's own promise of needing no database.
    """

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._log)

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction()


def _recorded_statements(cls: type, attr: str, args: tuple) -> list[str]:
    """Every statement `cls(...).<attr>(*args)` executed, against a recorder."""
    log: list[str] = []
    instance = cls(connection_factory=lambda: _RecordingConnection(log))
    getattr(instance, attr)(*args)
    return log


def _reads_table(statements: list[str], table: str) -> bool:
    pattern = re.compile(
        rf"\bSELECT\b.*\bFROM\s+{re.escape(table)}\b", re.IGNORECASE | re.DOTALL
    )
    return any(pattern.search(statement) for statement in statements)


def _writes_table(statements: list[str], table: str) -> bool:
    pattern = re.compile(
        rf"\b(?:INSERT\s+INTO|UPDATE)\s+{re.escape(table)}\b", re.IGNORECASE
    )
    return any(pattern.search(statement) for statement in statements)


def _bespoke_violations(entry: BespokeMirror) -> list[str]:
    """What is wrong with `entry`, or `[]` if it is a real mirror.

    The two halves are checked against statements recorded from two separate
    calls, never pooled — a `list_entries` that stopped reading and a
    `replace_entries` that still writes must not average out to "fine".
    """
    violations: list[str] = []

    read_statements = _recorded_statements(entry.cls, entry.read_attr, ())
    if not _reads_table(read_statements, entry.table):
        violations.append(
            f"{entry.cls.__name__}.{entry.read_attr} recorded no `SELECT ... FROM "
            f"{entry.table}`: {read_statements!r}"
        )

    mutation_statements = _recorded_statements(
        entry.cls, entry.mutation_attr, entry.mutation_args
    )
    if not _writes_table(mutation_statements, entry.table):
        violations.append(
            f"{entry.cls.__name__}.{entry.mutation_attr} recorded no `INSERT INTO "
            f"{entry.table}` or `UPDATE {entry.table}`: {mutation_statements!r}"
        )

    return violations


@pytest.mark.parametrize(
    "table", sorted(BESPOKE_MIRRORS), ids=sorted(BESPOKE_MIRRORS)
)
def test_every_bespoke_mirror_is_real(table: str) -> None:
    assert _bespoke_violations(BESPOKE_MIRRORS[table]) == []


def _bespoke_mismatch(
    bespoke: dict[str, BespokeMirror], exclusions: dict[str, Exclusion]
) -> tuple[list[str], list[str]]:
    """`(claimed but unregistered, registered but unclaimed)`.

    Both directions matter: a `bespoke=True` exclusion with no matching entry
    here is the unverified-prose failure Gate A exists to close, and an entry
    here with no matching exclusion means Gate A still demands a
    `PostgresTableMirror` for a table that has a different kind of mirror —
    the false positive on the other side.
    """
    claimed = {table for table, exclusion in exclusions.items() if exclusion.bespoke}
    registered = set(bespoke)
    return sorted(claimed - registered), sorted(registered - claimed)


def test_every_bespoke_exclusion_matches_a_registered_mirror() -> None:
    claimed_only, registered_only = _bespoke_mismatch(
        BESPOKE_MIRRORS, UNMIRRORED_SCHEMA_TABLES
    )
    assert claimed_only == [], (
        f"tables excused from Gate A as bespoke with no BESPOKE_MIRRORS entry: "
        f"{claimed_only}. Register the mirror, or drop `bespoke=True`."
    )
    assert registered_only == [], (
        f"tables in BESPOKE_MIRRORS with no matching bespoke exclusion: "
        f"{registered_only}. Add `bespoke=True` to their UNMIRRORED_SCHEMA_TABLES "
        "entry, or remove the registration."
    )


def test_the_bespoke_registration_gate_fails_when_they_disagree() -> None:
    """The gate above, shown to bite in both directions, via the function it
    actually uses — not a hand-rolled restatement of it."""
    unregistered = dict(BESPOKE_MIRRORS)
    del unregistered["queue_entry"]
    assert _bespoke_mismatch(unregistered, UNMIRRORED_SCHEMA_TABLES) == (
        ["queue_entry"],
        [],
    )

    unclaimed = dict(UNMIRRORED_SCHEMA_TABLES)
    del unclaimed["queue_entry"]
    assert _bespoke_mismatch(BESPOKE_MIRRORS, unclaimed) == ([], ["queue_entry"])


#: A single row, shaped like `PostgresQueueMirror` expects, for the decoys
#: below — none of which is `queue_entry`'s real mirror.
_DECOY_ROW = BESPOKE_MIRRORS["queue_entry"].mutation_args[0][0]


class _ReadThatDeletesInstead:
    """`list_entries` issues the mutation's verb, not its own."""

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory or (lambda: None)

    def list_entries(self) -> list:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM queue_entry")
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM queue_entry")
                    if entries:
                        cur.executemany(
                            "INSERT INTO queue_entry (id) VALUES (%s)",
                            [(entry["id"],) for entry in entries],
                        )


class _MutationThatOnlyReads:
    """`replace_entries` reads the table it was supposed to write."""

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory or (lambda: None)

    def list_entries(self) -> list:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM queue_entry")
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM queue_entry")


class _MutationMissingItsInsert:
    """The whole-list replace keeps its `DELETE` and silently drops the
    `INSERT` — the write half of the contract disappears while the method
    still touches the table."""

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory or (lambda: None)

    def list_entries(self) -> list:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM queue_entry")
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM queue_entry")


class _MirrorForADifferentTable:
    """Both halves are real, and both name a table that is not `queue_entry` —
    the substring `queue_entry` still occurs in every statement, which is
    exactly what a bare-substring check would miss."""

    def __init__(self, connection_factory: object = None) -> None:
        self._factory = connection_factory or (lambda: None)

    def list_entries(self) -> list:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM queue_entry_archive")
        return []

    def replace_entries(self, entries: list) -> None:
        with self._factory() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM queue_entry_archive")
                    if entries:
                        cur.executemany(
                            "INSERT INTO queue_entry_archive (id) VALUES (%s)",
                            [(entry["id"],) for entry in entries],
                        )


@pytest.mark.parametrize(
    "decoy_cls",
    [_ReadThatDeletesInstead, _MirrorForADifferentTable],
    ids=["read_issues_delete", "read_names_a_different_table"],
)
def test_the_bespoke_gate_fails_when_the_read_method_does_not_read(
    decoy_cls: type,
) -> None:
    entry = BespokeMirror(
        cls=decoy_cls,
        table="queue_entry",
        read_attr="list_entries",
        mutation_attr="replace_entries",
        mutation_args=([_DECOY_ROW],),
    )
    violations = _bespoke_violations(entry)
    assert any("list_entries" in violation for violation in violations), violations


@pytest.mark.parametrize(
    "decoy_cls",
    [_MutationThatOnlyReads, _MutationMissingItsInsert, _MirrorForADifferentTable],
    ids=["mutation_only_selects", "mutation_loses_its_insert", "mutation_names_a_different_table"],
)
def test_the_bespoke_gate_fails_when_the_mutation_method_does_not_write(
    decoy_cls: type,
) -> None:
    entry = BespokeMirror(
        cls=decoy_cls,
        table="queue_entry",
        read_attr="list_entries",
        mutation_attr="replace_entries",
        mutation_args=([_DECOY_ROW],),
    )
    violations = _bespoke_violations(entry)
    assert any("replace_entries" in violation for violation in violations), violations


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
