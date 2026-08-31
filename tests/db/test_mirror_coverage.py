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

A third gate closes a narrower hole the first two cannot see. `queue_entry` is
excluded from Gate A not because nothing mirrors it, but because something
does — `PostgresQueueMirror`, which predates `PostgresTableMirror` and was
never folded into it (see `table_mirror.py` and `queue_store.py` for why).
Signing that off as prose — "it's mirrored elsewhere, trust the docstring" —
is indistinguishable from an omission once the exclusion exists: the symbol
could be renamed, replaced with an unrelated class, or quietly stop touching
the table, and Gate A would still see a signed exclusion and stay green. Gate
C proves the claim instead of accepting it: `BESPOKE_MIRRORS` names the class
and the two methods — one that reads, one that writes — and
`assert_bespoke_mirror_covers_its_table` parses each method's own source for a
real SQL reference to the table, not a substring or a same-named value. The
two are also proved to agree: an exclusion cannot claim "bespoke mirror" while
`BESPOKE_MIRRORS` forgets it, or vice versa.

No database is needed here, deliberately — the declarations are the thing under
test, and they must stay checked on a machine with no PostgreSQL.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
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

    `reason` and `task` are both required and checked. A reason without an
    owning task is an opinion that nothing will revisit; a task without a
    reason makes the next reader open the backlog to find out what was
    decided here.

    `bespoke_mirror` is a narrower, machine-checked claim: it says the table
    *is* mirrored, just not through `PostgresTableMirror`, and that the entry
    naming it lives in `BESPOKE_MIRRORS` below. Default `False` — most
    exclusions mean "genuinely unmirrored" — and
    `test_bespoke_mirrors_and_their_exclusions_agree` holds the two registries
    to the same set of tables, so setting this flag without adding the
    `BESPOKE_MIRRORS` entry (or the reverse) fails on its own, rather than
    resting on the reason text saying so.
    """

    reason: str
    task: str
    bespoke_mirror: bool = False


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
        bespoke_mirror=True,
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
# Gate C — a table excluded as "mirrored under a bespoke contract" really is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BespokeMirror:
    """A table mirrored by hand-written code instead of a `PostgresTableMirror`.

    `read_attr`/`mutation_attr` name the two methods that have to prove the
    claim: one on the path a reconciliation read would take, one on the path a
    write would take. Naming both, and checking each on its own, is what
    catches a class that keeps writing the table but stops reading it back (or
    the reverse) — concatenating the two methods' SQL before checking would
    not, because the other method's reference would still be in the pool.
    """

    module: str
    class_name: str
    read_attr: str
    mutation_attr: str

    def load(self) -> type:
        return getattr(importlib.import_module(self.module), self.class_name)


BESPOKE_MIRRORS: dict[str, BespokeMirror] = {
    "queue_entry": BespokeMirror(
        module="command_center.db.queue_store",
        class_name="PostgresQueueMirror",
        read_attr="list_entries",
        mutation_attr="replace_entries",
    ),
}

#: A real reference to `table`, not the table as a substring or a quoted
#: value. Requires a clause keyword immediately before the identifier, so
#: `SELECT 'queue_entry'` (the name as a value) and `FROM queue_entry_archive`
#: (the name as a prefix) both fail to match — `\b` alone accepts both, since
#: neither a quote nor an underscore breaks a word boundary.
def _table_reference_pattern(table: str) -> re.Pattern[str]:
    return re.compile(
        rf'\b(?:FROM|INTO|UPDATE|JOIN)\s+"?{re.escape(table)}"?\b', re.IGNORECASE
    )


def _literal_text(node: ast.AST) -> str:
    """The fixed text of a string or f-string node.

    An f-string's `{...}` slots hold column lists and placeholders, never a
    table name a caller would choose per-call, so only the surrounding
    `Constant` pieces are read. Implicit adjacent-literal concatenation (two
    string literals back to back) is already one `JoinedStr`/`Constant` node
    by the time the parser hands it over, so no special case is needed for it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            piece.value
            for piece in node.values
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str)
        )
    return ""


def _touches_table(cls: type, method_name: str, table: str) -> bool:
    """True when `method_name`, read from `cls` alone, sends `table` a real
    statement.

    Scoped to one method's own AST — a table mentioned only in a sibling
    method, a docstring, or a comment does not count, because none of those
    is this method touching the table.
    """
    method = inspect.getattr_static(cls, method_name)
    assert inspect.isfunction(method), (
        f"{cls.__name__}.{method_name} is not a method — nothing to inspect"
    )
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    pattern = _table_reference_pattern(table)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executemany")
        ):
            continue
        for arg in node.args:
            if pattern.search(_literal_text(arg)):
                return True
    return False


def assert_bespoke_mirror_covers_its_table(
    cls: type, table: str, read_attr: str, mutation_attr: str
) -> None:
    """The one function every real check and every control below calls.

    A control that hand-rolls its own string comparison instead of calling
    this proves only that the control's own logic works — the production gate
    could be deleted, inverted, or changed to concatenate both paths and the
    control would not notice. Routing everything through this function means
    a change here that weakens the check turns every one of the negative
    controls green, which is the failure the controls exist to catch.
    """
    assert not issubclass(cls, PostgresTableMirror), (
        f"{cls.__name__} is a PostgresTableMirror subclass — it belongs in the "
        "generic contract (test_mirror_contract.py), not BESPOKE_MIRRORS."
    )
    for attr in (read_attr, mutation_attr):
        if not _touches_table(cls, attr, table):
            raise AssertionError(
                f"{cls.__name__}.{attr} sends {table!r} no FROM/INTO/UPDATE/JOIN "
                "statement of its own — it does not mirror this table on this path."
            )


@pytest.mark.parametrize(
    ("table", "mirror"), sorted(BESPOKE_MIRRORS.items()), ids=list(BESPOKE_MIRRORS)
)
def test_every_bespoke_mirror_is_real(table: str, mirror: BespokeMirror) -> None:
    """The exclusion's prose says "mirrored elsewhere" — proved, not trusted."""
    assert_bespoke_mirror_covers_its_table(
        mirror.load(), table, mirror.read_attr, mirror.mutation_attr
    )


def test_bespoke_mirrors_and_their_exclusions_agree() -> None:
    """Both directions of one claim, checked as a single set equality.

    A `BESPOKE_MIRRORS` entry with no matching exclusion would be a table
    the two other gates have never heard needs signing off. The reverse is the
    gap a previous review found: an exclusion marked `bespoke_mirror=True`
    survives on its own even after its `BESPOKE_MIRRORS` entry is deleted,
    because nothing but this test reads the two registries together — deleting
    the entry silently drops `test_every_bespoke_mirror_is_real`'s parametrized
    proof for that table while Gate A stays green on the exclusion's prose
    alone. Equality between the two sets closes it from either direction.
    """
    claimed = {
        table
        for table, exclusion in UNMIRRORED_SCHEMA_TABLES.items()
        if exclusion.bespoke_mirror
    }
    assert set(BESPOKE_MIRRORS) == claimed, (
        f"BESPOKE_MIRRORS declares {sorted(BESPOKE_MIRRORS)}; exclusions claiming "
        f"bespoke_mirror=True declare {sorted(claimed)}. Every bespoke mirror needs "
        "both, naming the same table."
    )


# --- the check proved to bite, via the same function it uses in earnest -----


class _BespokeDecoyMissingReadReference:
    """Writes the table, but the read path never mentions it."""

    def read(self) -> None:
        self._cursor.execute("SELECT 1")

    def write(self) -> None:
        self._cursor.execute("DELETE FROM queue_entry")


class _BespokeDecoyMissingMutationReference:
    """Reads the table, but the write path never mentions it."""

    def read(self) -> None:
        self._cursor.execute("SELECT * FROM queue_entry")

    def write(self) -> None:
        self._cursor.execute("SELECT 1")


class _BespokeDecoySubstringAndValueOnly:
    """Names the table on both paths, but never as a table reference."""

    def read(self) -> None:
        self._cursor.execute("SELECT * FROM queue_entry_archive")

    def write(self) -> None:
        self._cursor.execute("SELECT 'queue_entry' AS label")


def test_the_bespoke_check_fails_when_the_read_path_drops_the_table() -> None:
    with pytest.raises(AssertionError):
        assert_bespoke_mirror_covers_its_table(
            _BespokeDecoyMissingReadReference, "queue_entry", "read", "write"
        )


def test_the_bespoke_check_fails_when_the_mutation_path_drops_the_table() -> None:
    with pytest.raises(AssertionError):
        assert_bespoke_mirror_covers_its_table(
            _BespokeDecoyMissingMutationReference, "queue_entry", "read", "write"
        )


def test_the_bespoke_check_rejects_a_substring_or_a_quoted_value() -> None:
    with pytest.raises(AssertionError):
        assert_bespoke_mirror_covers_its_table(
            _BespokeDecoySubstringAndValueOnly, "queue_entry", "read", "write"
        )


def test_the_bespoke_check_passes_the_real_mirror() -> None:
    """The positive control for the same shared function the controls above
    use negatively — without it, an `assert_bespoke_mirror_covers_its_table`
    that always raised would pass every test in this section."""
    mirror = BESPOKE_MIRRORS["queue_entry"]
    assert_bespoke_mirror_covers_its_table(
        mirror.load(), "queue_entry", mirror.read_attr, mirror.mutation_attr
    )


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
