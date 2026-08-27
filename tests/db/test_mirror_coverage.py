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

VOYN-W0-AICC-SRV-07h found the fix above still ambiguous in a different way:
`queue_entry` lived in the *exclusion* registry (`UNMIRRORED_SCHEMA_TABLES`)
next to tables with no SQLite source at all (`work_item`, `principal`, ...).
An exclusion is supposed to mean "nothing to mirror here"; `queue_entry` means
the opposite — it has a source and a working mirror, just not this module's
kind. The two facts read identically to an operator counting entries, which is
the same ambiguity by one level of indirection. `MIRRORED_UNDER_OWN_CONTRACT`
below is the third bucket: covered, by a contract other than
`PostgresTableMirror`. `test_the_domain_table_total_is_reported_not_left_to_a_silent_subtraction`
turns "32 shared + 1 own contract = 33" into an assertion with the numbers in
its failure message, rather than a fact that only holds if nobody miscounts.

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
# Tables mirrored, but not through `PostgresTableMirror` — VOYN-W0-AICC-SRV-07h
# ---------------------------------------------------------------------------
#
# `UNMIRRORED_SCHEMA_TABLES` above is one bucket for two different facts: a
# table with no SQLite source at all (`work_item`, `principal`, ...) and a
# table that *does* have a source and *is* mirrored, just by code this module
# does not scan for. Before SRV-07h `queue_entry` lived in that bucket, and
# the two facts read identically to anyone totting up the count — which is
# exactly how "32 mirrored + queue_entry excluded" and "32 mirrored, 33rd
# table forgotten" became indistinguishable. This registry is the missing
# third answer: covered, by a contract of its own.

MIRRORED_UNDER_OWN_CONTRACT: dict[str, Exclusion] = {
    "queue_entry": Exclusion(
        reason=(
            "Mirrored under a contract of its own rather than the shared one: the "
            "queue synchronises by whole-list replacement (DELETE plus a bulk "
            "re-INSERT) and carries a `position` column the JSON authority does not "
            "have, so it has `replace_entries`/`list_entries` and no `upsert` at "
            "all (`command_center/db/queue_store.py`). Folding it into "
            "`PostgresTableMirror` would produce one class saying something vague "
            "about both. Deliberate, pre-existing, and recorded here — rather than "
            "in `UNMIRRORED_SCHEMA_TABLES` — because it is covered, not excluded: "
            "conflating the two is what let the 33rd table hide as an off-by-one "
            "in a hand-written count."
        ),
        task="VOYN-W0-AICC-QUEUE-ENTRY-PARITY",
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


def _covered_tables() -> set[str]:
    """Every domain table with a working mirror, whatever contract it uses.

    The shared `PostgresTableMirror` subclasses plus the small set declared in
    `MIRRORED_UNDER_OWN_CONTRACT` — the two answers that both mean "this table
    is not the omission Gate A exists to catch," as opposed to a signed
    exclusion, which means "there is nothing here to mirror at all."
    """
    return _mirrored_tables() | set(MIRRORED_UNDER_OWN_CONTRACT)


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
        _schema_tables(), _covered_tables(), UNMIRRORED_SCHEMA_TABLES
    )
    assert uncovered == [], (
        "tables with neither a mirror nor a signed exclusion: "
        f"{uncovered}. Declare a `PostgresTableMirror` subclass for each, or add "
        "an entry to UNMIRRORED_SCHEMA_TABLES with the reason and the owning task."
    )


def test_no_mirror_exclusion_outlives_its_reason() -> None:
    stale = _stale(_schema_tables(), _covered_tables(), UNMIRRORED_SCHEMA_TABLES)
    assert stale == [], f"exclusions for tables that are mirrored or gone: {stale}"


def test_the_coverage_gate_fails_on_a_table_that_is_neither() -> None:
    """The gate, shown to bite.

    A coverage rule that has only ever been observed passing is indistinguishable
    from one that computes the empty set. This runs it against a schema carrying
    a table nobody declared anything about.
    """
    tables = _schema_tables() | {"a_table_nobody_declared"}
    assert _uncovered(tables, _covered_tables(), UNMIRRORED_SCHEMA_TABLES) == [
        "a_table_nobody_declared"
    ]

    # And the same table, once signed, passes — so the gate is answering the
    # question it claims to and not simply rejecting anything unfamiliar.
    signed = dict(
        UNMIRRORED_SCHEMA_TABLES,
        a_table_nobody_declared=Exclusion(reason="test double", task="VOYN-TEST"),
    )
    assert _uncovered(tables, _covered_tables(), signed) == []


def test_the_staleness_gate_fails_on_an_exclusion_for_a_mirrored_table() -> None:
    covered = _covered_tables() | {"a_table_nobody_declared"}
    stale_declarations = dict(
        UNMIRRORED_SCHEMA_TABLES,
        a_table_nobody_declared=Exclusion(reason="test double", task="VOYN-TEST"),
    )
    assert _stale(_schema_tables(), covered, stale_declarations) == [
        "a_table_nobody_declared"
    ]


def test_queue_entry_lives_in_the_own_contract_registry_not_the_exclusions() -> None:
    """The fact this whole registry split exists to keep visible.

    `queue_entry` has a real mirror (`command_center/db/queue_store.py`); it is
    not excluded from anything. Before SRV-07h it sat in
    `UNMIRRORED_SCHEMA_TABLES` next to tables that have no SQLite source at
    all, which is what let a genuinely covered table read as a gap-with-a-note.
    """
    assert "queue_entry" not in UNMIRRORED_SCHEMA_TABLES
    assert "queue_entry" in MIRRORED_UNDER_OWN_CONTRACT
    assert "queue_entry" not in _mirrored_tables(), (
        "queue_entry has no PostgresTableMirror subclass — if this now fails, "
        "its own contract was folded into the shared one and the "
        "MIRRORED_UNDER_OWN_CONTRACT entry is stale"
    )


def test_no_own_contract_exclusion_outlives_its_reason() -> None:
    """As `test_no_mirror_exclusion_outlives_its_reason`, for the other registry.

    A table stays declared here only while it is mirrored by its own contract
    and by nothing else; once a `PostgresTableMirror` subclass also claims it,
    or the table is dropped, the declaration is noise.
    """
    stale = _stale(_schema_tables(), _mirrored_tables(), MIRRORED_UNDER_OWN_CONTRACT)
    assert stale == [], (
        f"own-contract declarations for tables that moved or gone: {stale}"
    )


def test_the_domain_table_total_is_reported_not_left_to_a_silent_subtraction() -> None:
    """The property SRV-07h exists for: 33 covered, stated, not 32-and-silence.

    `_mirrored_tables()` alone answers "32" — correct for the shared contract
    and silent about `queue_entry`. Reporting only that figure is indistinguishable
    from a schema that grew a 33rd table nobody mirrored at all, which is exactly
    the ambiguity `table_mirror.py`'s docstring names as the original defect.
    This test makes the total, and its two components, an assertion with numbers
    in the failure message rather than a fact left implicit in set arithmetic.
    """
    shared = _mirrored_tables()
    own_contract = set(MIRRORED_UNDER_OWN_CONTRACT)
    assert not shared & own_contract, (
        "a table declared in both registries has two conflicting mirrors"
    )
    domain_tables = _schema_tables() - set(UNMIRRORED_SCHEMA_TABLES)
    covered = shared | own_contract
    assert covered == domain_tables, (
        f"{len(covered)} covered ({len(shared)} shared contract + "
        f"{len(own_contract)} own contract) vs {len(domain_tables)} SQLite-"
        f"authority domain tables — difference: {sorted(domain_tables ^ covered)}"
    )
    assert len(domain_tables) == 33, (
        f"the SQLite-authority domain table count moved to {len(domain_tables)}; "
        "update docs/srv01b-schema-map.md's reconciliation and this comment "
        "together, the same rule test_schema_correspondence.py applies to the "
        "raw migration count"
    )


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
# The signatures themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "registry",
    [
        UNMIRRORED_SCHEMA_TABLES,
        MIRRORED_UNDER_OWN_CONTRACT,
        RUNTIME_TABLES_WITHOUT_A_TARGET,
    ],
    ids=["schema", "own_contract", "runtime"],
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
