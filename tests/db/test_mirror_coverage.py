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

"The schema" is read out of the migration DDL itself, never from a count and
never from a registry — not even `roles.ALL_TABLES`, which is hand-maintained
and could forget the same table this gate is asking about. A number edited by
hand on every migration measures whether someone remembered, which is precisely
what a gate is for; it cannot also be the gate.

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

from tests.db.mirror_discovery import mirror_classes

ROOT = Path(__file__).resolve().parents[2]
COMMAND_CENTER = ROOT / "command_center"
RUNTIME_DB_PACKAGE = COMMAND_CENTER / "runtime" / "db"
SQL_DIR = COMMAND_CENTER / "db" / "sql"

#: The ledger is the runner's, not a domain table, on both sides.
LEDGER_TABLES = frozenset({"schema_migration", "schema_version"})

#: The trailing `(` is what keeps prose out. `schema.py` discusses
#: "`CREATE TABLE IF NOT EXISTS`," inside a docstring, and a rule that stops
#: at the identifier reads the word `IF` as a table nobody declared — a gate
#: failing on its own documentation, which is how a gate gets weakened.
#:
#: The optional `schema.` qualifier is not decoration. Without it the pattern
#: does not merely mis-name a qualified table, it fails to match the statement
#: at all — `CREATE TABLE public.thing (` would leave `thing` invisible to a
#: gate whose entire claim is that no table escapes it. Silently skipping the
#: one migration written in a different house style is the failure mode this
#: module exists to prevent, so the qualifier is consumed and the last
#: identifier is the table.
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[a-z_][a-z0-9_]*\s*\.\s*)?([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)

#: `DROP` has no `(` to anchor on, so the terminating `;` plays that role: the
#: pattern matches a statement, not the phrase. Required because a set that only
#: ever grows is not "the schema" — it is every table the schema has ever had,
#: and it would keep demanding a mirror for one that has since been removed.
_DROP_TABLE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r"(?:[a-z_][a-z0-9_]*\s*\.\s*)?([a-z_][a-z0-9_]*)\s*"
    r"(?:CASCADE|RESTRICT)?\s*;",
    re.IGNORECASE,
)

#: A commented-out `CREATE TABLE` is not a table, and a commented-out `DROP` is
#: not a removal. Reading past `--` would make the gate demand a mirror for the
#: first and miss the second — and missing a removal is the direction that goes
#: quiet. Safe on these files: every `--` in them follows a closed literal.
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


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
    # The backlog store (0005, BO-S1) is PostgreSQL-native authority from
    # birth, like the work_item family below: there is no SQLite source to
    # dual-write from — the incumbent it replaces is a Markdown FILE, and its
    # reconciliation path is the importer (backlog_store.import_markdown),
    # not the mirror machinery. One exclusion per table so a future table in
    # the family still has to sign in on its own.
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
        task="VOYN-W0-AICC-DAILY-AUDIT-UNMIRRORED",
    ),
    "daily_audit_campaign": Exclusion(
        reason=(
            "As `daily_audit_schedule`: created ad hoc outside `migrate()` by the "
            "daily-audit daemon, with no PostgreSQL target."
        ),
        task="VOYN-W0-AICC-DAILY-AUDIT-UNMIRRORED",
    ),
}


# ---------------------------------------------------------------------------
# Gate helpers: the tests feed them a table that is neither covered nor signed.
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
    """Domain tables derived from every PostgreSQL migration's actual DDL.

    ``roles.ALL_TABLES`` is deliberately not the source here. It is another
    hand-maintained registry (and has its own migration-set parity gate), so
    using it would let a migration add a table that both the roles registry and
    the mirror registry forgot. That is the exact omission this gate exists
    to catch.

    Read in migration order and drops applied, so the answer is what the set
    *leaves standing* rather than everything it has ever declared. No database
    and no import of `command_center.db`: the whole point of this module is that
    it still runs on a laptop with no PostgreSQL client library installed.
    """
    tables: set[str] = set()
    for path in sorted(SQL_DIR.glob("*.up.sql")):
        ddl = _SQL_LINE_COMMENT.sub("", path.read_text(encoding="utf-8"))
        tables.update(match.lower() for match in _CREATE_TABLE.findall(ddl))
        tables.difference_update(match.lower() for match in _DROP_TABLE.findall(ddl))
    return tables - LEDGER_TABLES


def _stage(tmp_path: Path, monkeypatch, **migrations: str) -> Path:
    """The real migration set plus `migrations`, with `SQL_DIR` pointed at it.

    Copied rather than mocked so the tests below exercise the shipped
    `_schema_tables` against the shipped DDL: a stub staged *alone* would fail a
    gate that rejects everything just as readily as it fails the gate we have.
    Keyword names carry a leading `_` because a migration file starts with a
    digit and a Python keyword cannot; it is stripped here.
    """
    staged = tmp_path / "sql"
    staged.mkdir(parents=True)
    for migration in SQL_DIR.glob("*.up.sql"):
        (staged / migration.name).write_text(
            migration.read_text(encoding="utf-8"), encoding="utf-8"
        )
    for name, ddl in migrations.items():
        (staged / f"{name.lstrip('_')}.up.sql").write_text(ddl + "\n", encoding="utf-8")
    # `_schema_tables` reads the module global, so redirecting it is what lets
    # these tests run the shipped gate rather than a paraphrase of it.
    monkeypatch.setitem(globals(), "SQL_DIR", staged)
    return staged


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


def test_the_coverage_gate_fails_on_a_table_that_is_neither(
    monkeypatch, tmp_path: Path
) -> None:
    """The gate, shown to bite — on the real schema plus one stub table.

    A coverage rule that has only ever been observed passing is indistinguishable
    from one that computes the empty set. So this adds a migration the way a
    migration is actually added, next to the real ones rather than instead of
    them, and calls the shipped gate function itself. Staging the stub *alone*
    would prove far less: a directory holding one undeclared table fails a gate
    that merely rejects everything just as readily as it fails the gate we have.
    """
    _stage(
        tmp_path,
        monkeypatch,
        _9999_undeclared="CREATE TABLE a_table_nobody_declared (id bigint PRIMARY KEY);",
    )

    assert "a_table_nobody_declared" in _schema_tables(), "the stub was not picked up"
    with pytest.raises(AssertionError, match="a_table_nobody_declared"):
        test_every_schema_table_is_mirrored_or_signed_out_of_scope()

    # And the same schema, with that one table signed, passes — so the gate is
    # answering the question it claims to and not simply rejecting anything
    # unfamiliar. This half re-proves the real schema alongside the stub, which
    # is the reason the real migrations were staged rather than replaced.
    signed = dict(
        UNMIRRORED_SCHEMA_TABLES,
        a_table_nobody_declared=Exclusion(reason="test double", task="VOYN-TEST"),
    )
    assert _uncovered(_schema_tables(), _mirrored_tables(), signed) == []


def test_a_table_a_later_migration_drops_stops_being_demanded(
    monkeypatch, tmp_path: Path
) -> None:
    """Drops are applied, so the gate does not demand a mirror for a dead table.

    Nothing in the live migration set drops a table yet, which is exactly why
    this is written now: the first migration that does would otherwise turn the
    gate red for a table that no longer exists, and the cheapest way to quiet
    that is a signed exclusion for something imaginary — a false signature, in
    the one registry whose whole value is that its signatures are true.
    """
    _stage(
        tmp_path,
        monkeypatch,
        _9998_scaffold="CREATE TABLE a_temporary_scaffold (id bigint PRIMARY KEY);",
    )
    assert "a_temporary_scaffold" in _schema_tables()

    _stage(
        tmp_path / "again",
        monkeypatch,
        _9998_scaffold="CREATE TABLE a_temporary_scaffold (id bigint PRIMARY KEY);",
        _9999_teardown="DROP TABLE a_temporary_scaffold CASCADE;",
    )
    assert "a_temporary_scaffold" not in _schema_tables()
    test_every_schema_table_is_mirrored_or_signed_out_of_scope()


def test_commented_out_ddl_is_not_schema(monkeypatch, tmp_path: Path) -> None:
    """Neither half of a `--` line counts, and the drop half is the dangerous one.

    A commented `CREATE` that counted would demand a mirror for a table that was
    never created — noisy, but visible. A commented `DROP` that counted would
    remove a real table from the set and take its coverage requirement with it,
    which is the same silent hole this module was written to close.
    """
    _stage(
        tmp_path,
        monkeypatch,
        _9999_prose=(
            "-- CREATE TABLE a_table_only_discussed (id bigint PRIMARY KEY);\n"
            "-- DROP TABLE queue_entry;"
        ),
    )
    tables = _schema_tables()
    assert "a_table_only_discussed" not in tables
    assert "queue_entry" in tables


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
    targets = _schema_tables()
    uncovered = _uncovered(_runtime_tables(), targets, RUNTIME_TABLES_WITHOUT_A_TARGET)
    assert uncovered == [], (
        "runtime tables with no PostgreSQL target and no signed exclusion: "
        f"{uncovered}. Add the table to the migration set, or record it in "
        "RUNTIME_TABLES_WITHOUT_A_TARGET with the reason and the owning task."
    )


def test_no_runtime_exclusion_outlives_its_reason() -> None:
    stale = _stale(_runtime_tables(), _schema_tables(), RUNTIME_TABLES_WITHOUT_A_TARGET)
    assert stale == [], (
        f"exclusions for runtime tables that are gone or migrated: {stale}"
    )


def test_the_runtime_gate_fails_on_an_undeclared_ad_hoc_table() -> None:
    tables = _runtime_tables() | {"another_daemon_table"}
    assert _uncovered(tables, _schema_tables(), RUNTIME_TABLES_WITHOUT_A_TARGET) == [
        "another_daemon_table"
    ]


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
