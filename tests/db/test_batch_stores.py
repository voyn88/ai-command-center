"""Slice 8: six tables in one slice, because none of them adds a class.

Slices 1–6 each worked out one conversion class and cost a slice apiece; slice 7
made the machinery declarative. What is left in these six tables is a
recombination of solved shapes — `jsonb`, foreign keys, nullable lifecycle
timestamps — so they arrive together, with one test module rather than six
near-identical ones.

That is a deliberate trade and it has a limit: batching is safe *only* because
no table here introduces a conversion the shared machinery has not already been
proved against. The council family keeps its own slice because `council_event`
carries an identity column and its own event ordering.

`audit_run`/`audit_finding` trip the AIOS boundary gate on their **file name**:
`audit` is matched without behavioural corroboration by design. The first
version of this slice withdrew them, on the belief that a baseline edit needed
a separate architectural decision. `docs/AIOS_BOUNDARY.md` says otherwise —
this is its Direction 2 (reclassify a detector false positive), whose remedy is
an ordinary reviewed PR with the justification in its description, which is the
process that was already running. The tables are back, the baseline carries the
entry, and the reasoning lives in `command_center/db/audit_store.py`.

What still gets per-table attention, because it is per-table by nature:

* the column tuple, pinned against `0001_initial.up.sql`;
* whether the authority's readers hand out the stored shape — `audit_run` does
  **not**, and that is slice 4's trap appearing in a second family;
* the reconciliation entry point, staged after every write (slice 5's lesson).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from command_center import record_mirror
from command_center.db.advisor_store import (
    ADVISOR_PROPOSAL_COLUMNS,
    PostgresAdvisorProposalMirror,
)
from command_center.db.advisor_store import divergence as advisor_divergence
from command_center.db.audit_store import (
    AUDIT_FINDING_COLUMNS,
    AUDIT_RUN_COLUMNS,
    PostgresAuditFindingMirror,
    PostgresAuditRunMirror,
    audit_finding_divergence,
    audit_run_divergence,
)
from command_center.db.marketplace_store import (
    INSTALL_LOG_COLUMNS,
    MARKET_ITEM_COLUMNS,
    PostgresInstallLogMirror,
    PostgresMarketItemMirror,
    install_log_divergence,
    market_item_divergence,
)
from command_center.db.networking_store import (
    INVITATION_COLUMNS,
    PostgresInvitationMirror,
    invitation_divergence,
)
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror
from command_center.runtime.db import audit as audit_db
from command_center.runtime.db import marketplace as market_db
from command_center.runtime.db import networking as net_db
from command_center.runtime.db import wave1

ROOT = Path(__file__).resolve().parents[2]

#: Every table this slice adds, with the mirror and the authority-side name.
BATCH = (
    ("networking_invitation", INVITATION_COLUMNS, PostgresInvitationMirror),
    ("market_item", MARKET_ITEM_COLUMNS, PostgresMarketItemMirror),
    ("market_install_log", INSTALL_LOG_COLUMNS, PostgresInstallLogMirror),
    ("audit_run", AUDIT_RUN_COLUMNS, PostgresAuditRunMirror),
    ("audit_finding", AUDIT_FINDING_COLUMNS, PostgresAuditFindingMirror),
    ("advisor_proposal", ADVISOR_PROPOSAL_COLUMNS, PostgresAdvisorProposalMirror),
)


# --- contract and schema, for all six ---------------------------------------


def test_every_mirror_satisfies_the_row_oriented_contract() -> None:
    for table, _columns, mirror in BATCH:
        assert isinstance(
            mirror(connection_factory=lambda: None), record_mirror.RecordMirror
        ), table
        assert mirror.name == "postgres", table
        assert mirror.spec.table == table


def test_every_column_list_matches_the_accepted_postgresql_schema() -> None:
    ddl = (ROOT / "command_center/db/sql/0001_initial.up.sql").read_text(encoding="utf-8")
    for table, expected, _mirror in BATCH:
        body = ddl.split(f"CREATE TABLE {table} (", 1)[1].split(");", 1)[0]
        declared = tuple(
            line.strip().split()[0]
            for line in body.strip().splitlines()
            if line.strip()
            and not line.strip().startswith("--")
            and not line.strip().startswith("UNIQUE")
        )
        assert declared == expected, table


def test_every_mirror_covers_the_columns_the_authority_stores(tmp_path) -> None:
    """Read from the live SQLite schema rather than from source constants: a
    column added to a table and forgotten in the mirror is invisible to
    reconciliation, which compares only what it is given."""
    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)
    with wave1.db.connect(db_path) as conn:
        for table, expected, _mirror in BATCH:
            stored = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert stored == set(expected), table


def test_a_mirror_without_a_declaration_is_refused_at_import() -> None:
    """Slice 7's acceptance called this a footgun for exactly this slice, where
    declarations arrive several at a time. The failure now names the mistake
    instead of surfacing as a swallowed `AttributeError` on the first write."""
    with pytest.raises(TypeError, match="must declare"):

        class Forgot(PostgresTableMirror):
            pass

    class Declared(PostgresTableMirror):
        spec = MirroredTable(table="advisor_proposal", columns=ADVISOR_PROPOSAL_COLUMNS)

    assert Declared.spec.table == "advisor_proposal"


def test_the_reconciliations_carry_their_warnings_at_runtime() -> None:
    """Slice 7 replaced module-level functions with closures and the runtime
    docstrings went silent — including the one that closed slice 4's rejection,
    which tells an operator that reconciliation takes the *stored* reader. The
    warning has to be readable where the mistake is made: a REPL at cutover
    time."""
    from command_center.db.digest_item_store import divergence as digest_divergence
    from command_center.db.model_registry_store import event_divergence

    assert "list_model_events_stored" in (event_divergence.__doc__ or "")
    assert "list_digest_items_stored" in (digest_divergence.__doc__ or "")
    assert "list_audit_runs_stored" in (audit_run_divergence.__doc__ or "")
    # The plain ones still say what they are, rather than nothing.
    assert "advisor_proposal" in (advisor_divergence.__doc__ or "")


# --- the audit family: slice 4's trap in a second family ---------------------


def test_audit_runs_have_a_reconciliation_entry_point(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """`audit_run` decodes on the way out, exactly like `digest_item`.

    Every public reader — `get_audit_run`, `list_audit_runs`, and
    `set_audit_run_status`'s return value — replaces `checks_json` with a
    decoded `checks`. Fed those, reconciliation reports every run divergent on
    the one column that needed converting. `list_audit_runs_stored` is the
    answer, and this pins both halves so nobody has to rediscover which reader
    to call.
    """
    from command_center.db import audit_store

    monkeypatch.setattr(
        audit_store,
        "PostgresAuditRunMirror",
        lambda: PostgresAuditRunMirror(connection_factory=pg_connection_factory),
    )
    runs = PostgresAuditRunMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    audit_db.db.migrate(db_path)
    created = audit_db.create_audit_run(db_path, project_ref="AICC", checks=["lint", "tests"])

    decoded = audit_db.list_audit_runs(db_path)
    assert "checks_json" not in decoded[0]  # the premise
    assert audit_run_divergence(decoded, runs) != []

    assert audit_run_divergence(audit_db.list_audit_runs_stored(db_path), runs) == []
    assert created["checks"] == ["lint", "tests"]


def test_the_audit_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Staged, not end-state: `set_audit_run_status` rewrites the whole run, so
    an end-state check could not see a lost create (slice 5's lesson)."""
    from command_center.db import audit_store

    monkeypatch.setattr(
        audit_store,
        "PostgresAuditRunMirror",
        lambda: PostgresAuditRunMirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        audit_store,
        "PostgresAuditFindingMirror",
        lambda: PostgresAuditFindingMirror(connection_factory=pg_connection_factory),
    )
    runs = PostgresAuditRunMirror(connection_factory=pg_connection_factory)
    findings = PostgresAuditFindingMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    audit_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert audit_run_divergence(audit_db.list_audit_runs_stored(db_path), runs) == [], stage
        assert audit_finding_divergence(audit_db.list_audit_findings(db_path), findings) == [], (
            stage
        )

    run = audit_db.create_audit_run(db_path, project_ref="AICC", checks=["lint"])
    reconciled("run created")
    finding = audit_db.create_audit_finding(
        db_path, run_id=run["id"], category="security", summary="hardcoded secret", owner="ops"
    )
    reconciled("finding created")
    audit_db.set_audit_finding_status(
        db_path, finding["id"], expected_version=finding["version"], status="ack"
    )
    reconciled("finding acknowledged")
    audit_db.set_audit_run_status(
        db_path, run["id"], expected_version=run["version"], status="completed"
    )
    reconciled("run completed")


def test_a_finding_needs_its_run_in_the_mirror_first(pg_connection_factory) -> None:
    findings = PostgresAuditFindingMirror(connection_factory=pg_connection_factory)
    with pytest.raises(Exception) as refused:
        findings.upsert(
            {
                "id": "f1",
                "run_id": "absent-run",
                "category": "security",
                "severity": "info",
                "summary": "",
                "file_path": None,
                "loc": None,
                "status": "open",
                "owner": "ops",
                "project_ref": None,
                "promoted_task_id": None,
                "version": 0,
                "created_at": "2026-08-14T00:00:00",
                "updated_at": "2026-08-14T00:00:00",
            }
        )
    assert "foreign key" in str(refused.value).lower()


# --- the marketplace family --------------------------------------------------


def test_the_marketplace_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    from command_center.db import marketplace_store

    monkeypatch.setattr(
        marketplace_store,
        "PostgresMarketItemMirror",
        lambda: PostgresMarketItemMirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        marketplace_store,
        "PostgresInstallLogMirror",
        lambda: PostgresInstallLogMirror(connection_factory=pg_connection_factory),
    )
    items = PostgresMarketItemMirror(connection_factory=pg_connection_factory)
    logs = PostgresInstallLogMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    market_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert market_item_divergence(market_db.list_market_items(db_path), items) == [], stage
        assert install_log_divergence(
            market_db.list_install_log(db_path, item["id"]), logs
        ) == [], stage

    item = market_db.create_market_item(db_path, name="pack", kind="module", version="1.0")
    reconciled("item created")
    market_db.install_market_item(
        db_path,
        item["id"],
        expected_version=item["lock_version"],
        actor="ops",
        installer="cli",
        metadata={"channel": "stable"},
    )
    reconciled("item installed")


def test_install_metadata_round_trips_through_jsonb(pg_connection_factory, tmp_path) -> None:
    """The writer emits canonical JSON (`sort_keys=True`), which would make a
    text comparison survive here and nowhere else. The declaration compares
    parsed values anyway, so the reconciliation does not depend on one caller
    keeping that flag."""
    items = PostgresMarketItemMirror(connection_factory=pg_connection_factory)
    logs = PostgresInstallLogMirror(connection_factory=pg_connection_factory)

    items.upsert(
        {
            "id": "i1",
            "name": "pack",
            "kind": "module",
            "version": "1.0",
            "publisher": "",
            "description": "",
            "status": "listed",
            "provenance": "",
            "lock_version": 0,
            "created_at": "2026-08-14T00:00:00",
            "updated_at": "2026-08-14T00:00:00",
        }
    )
    logs.upsert(
        {
            "id": "l1",
            "item_id": "i1",
            "actor": "ops",
            "version": "1.0",
            "kind": "module",
            "provenance": "",
            "installer": "cli",
            "detail": "",
            "metadata_json": json.dumps({"b": 1, "a": 2}, sort_keys=True),
            "installed_at": "2026-08-14T00:00:00",
            "created_at": "2026-08-14T00:00:00",
        }
    )

    assert logs.list_records()[0]["metadata_json"] == {"a": 2, "b": 1}


# --- the networking family, completed ----------------------------------------


def test_invitations_reconcile_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    from command_center.db import networking_store

    # The real class, captured *before* the patch. Reading it through the
    # module inside the lambda would resolve to the lambda itself — the mirror
    # would raise, the hook would swallow it, the contact would never land, and
    # the invitation would then be refused by its foreign key. Slice 4's
    # acceptance flagged exactly this shape; writing it here proved the warning
    # was worth keeping.
    real_contact_mirror = networking_store.PostgresContactMirror
    monkeypatch.setattr(
        networking_store,
        "PostgresContactMirror",
        lambda: real_contact_mirror(connection_factory=pg_connection_factory),
    )
    monkeypatch.setattr(
        networking_store,
        "PostgresInvitationMirror",
        lambda: PostgresInvitationMirror(connection_factory=pg_connection_factory),
    )
    invitations = PostgresInvitationMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    net_db.db.migrate(db_path)
    contact = net_db.create_contact(db_path, display_name="invitee")

    def reconciled(stage: str) -> None:
        assert invitation_divergence(net_db.list_invitations(db_path), invitations) == [], stage

    created = net_db.create_invitation(db_path, contact_id=contact["id"], council_ref="c1")
    reconciled("invitation created")
    assert created["responded_at"] is None  # the nullable one, before it is set

    net_db.set_invitation_status(
        db_path, created["id"], expected_version=created["version"], status="accepted"
    )
    reconciled("invitation answered")
    assert net_db.get_invitation(db_path, created["id"])["responded_at"] is not None


# --- advisor_proposal ---------------------------------------------------------


def test_advisor_proposals_reconcile_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    from command_center.db import advisor_store

    monkeypatch.setattr(
        advisor_store,
        "PostgresAdvisorProposalMirror",
        lambda: PostgresAdvisorProposalMirror(connection_factory=pg_connection_factory),
    )
    proposals = PostgresAdvisorProposalMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert advisor_divergence(wave1.list_advisor_proposals(db_path), proposals) == [], stage

    created = wave1.create_advisor_proposal(
        db_path, kind="trend", title="use jsonb", project_ref="AICC"
    )
    reconciled("proposal created")
    accepted = wave1.set_advisor_proposal_status(
        db_path, created["id"], expected_version=created["version"], status="accepted"
    )
    reconciled("proposal accepted")
    wave1.promote_advisor_proposal(
        db_path, created["id"], expected_version=accepted["version"], task_id="task-1"
    )
    reconciled("proposal promoted")


# --- failure isolation, once for the batch ------------------------------------


def test_a_mirror_failure_cannot_break_any_authoritative_write(tmp_path, monkeypatch, caplog) -> None:
    """One test for six tables: the rule is the same everywhere, and repeating
    it per table would assert the same swallow six times.

    Also covers `VOYN-W0-AICC-MIRROR-SILENT-DROP`'s log-level half: every
    application entry point in this package defaults to INFO
    (``logging.basicConfig(level=logging.INFO)`` in `command_center/db/cli.py`,
    `command_center/worker/__main__.py`, `command_center/worktree_sweep.py`),
    so a swallowed failure logged at DEBUG would never appear anywhere an
    operator actually looks — indistinguishable from not being logged at all.
    `caplog` defaults to WARNING, matching that default, so this only passes
    if the six hooks below actually log at WARNING or louder.
    """
    from command_center.db import advisor_store, audit_store, marketplace_store, networking_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    for module, names in (
        (networking_store, ("PostgresContactMirror", "PostgresInvitationMirror")),
        (marketplace_store, ("PostgresMarketItemMirror", "PostgresInstallLogMirror")),
        (audit_store, ("PostgresAuditRunMirror", "PostgresAuditFindingMirror")),
        (advisor_store, ("PostgresAdvisorProposalMirror",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    wave1.db.migrate(db_path)

    with caplog.at_level(logging.WARNING):
        contact = net_db.create_contact(db_path, display_name="survives")
        invitation = net_db.create_invitation(db_path, contact_id=contact["id"], council_ref="c1")
        item = market_db.create_market_item(db_path, name="pack", kind="module", version="1.0")
        market_db.install_market_item(
            db_path,
            item["id"],
            expected_version=item["lock_version"],
            actor="ops",
            installer="cli",
        )
        run = audit_db.create_audit_run(db_path, project_ref="AICC", checks=["lint"])
        audit_db.create_audit_finding(
            db_path, run_id=run["id"], category="security", summary="s", owner="ops"
        )
        proposal = wave1.create_advisor_proposal(
            db_path, kind="trend", title="t", project_ref="AICC"
        )

    assert net_db.get_invitation(db_path, invitation["id"])["council_ref"] == "c1"
    assert market_db.get_market_item(db_path, item["id"])["name"] == "pack"
    assert len(market_db.list_install_log(db_path, item["id"])) == 1
    assert len(audit_db.list_audit_findings(db_path)) == 1
    assert wave1.get_advisor_proposal(db_path, proposal["id"])["title"] == "t"

    warned_tables = {
        record.getMessage().split()[3]
        for record in caplog.records
        if record.levelno == logging.WARNING and "Could not mirror" in record.getMessage()
    }
    assert warned_tables == {
        "contact",
        "networking_invitation",
        "market_item",
        "market_install_log",
        "audit_run",
        "audit_finding",
        "advisor_proposal",
    }


def test_importing_the_new_stores_needs_no_postgresql_client() -> None:
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import command_center.db.advisor_store, command_center.db.audit_store,"
        " command_center.db.marketplace_store, command_center.db.networking_store;"
        "assert 'aios_db' not in sys.modules;"
        "assert 'psycopg' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr
