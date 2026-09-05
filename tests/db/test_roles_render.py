"""The rendered grant matrix, checked without a database.

`test_role_privileges.py` proves the database enforces this matrix. These tests
prove the matrix says what it is supposed to say — cheap, and they run on every
machine, including ones with no PostgreSQL available.
"""

from __future__ import annotations

import pytest

from command_center.db import roles


def test_public_is_stripped_of_schema_privileges() -> None:
    statements = roles.render_grants()
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC;" in statements
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;" in statements


def test_only_the_migrator_may_create_objects() -> None:
    statements = roles.render_grants()
    creators = [s for s in statements if "CREATE ON SCHEMA" in s]
    assert creators == ["GRANT USAGE, CREATE ON SCHEMA public TO aicc_migrator;"]


def test_no_role_is_granted_delete() -> None:
    """This schema is an append/update ledger; deletes are an owner operation."""
    assert not [s for s in roles.render_grants() if "DELETE" in s]


def test_no_role_is_granted_truncate_or_ddl() -> None:
    for forbidden in ("TRUNCATE", "REFERENCES", "TRIGGER"):
        assert not [s for s in roles.render_grants() if forbidden in s], forbidden


def test_worker_cannot_reach_governance_tables() -> None:
    """A compromised execution host must not read or forge the decision record."""
    off_limits = {
        "proposal",
        "proposal_event",
        "proposal_evidence",
        "provenance_evidence",
        "run_provenance",
        "motion",
        "council_vote",
        "council_decision",
        "council_event",
        "audit_run",
        "audit_finding",
        "market_item",
        "market_install_log",
        "model_entry",
        "model_event",
    }
    granted = set(roles.PRIVILEGES[roles.WORKER_ROLE])
    assert not (granted & off_limits)


def test_worker_cannot_enqueue_work() -> None:
    """Workers claim queue entries; only the dispatcher creates them."""
    assert "INSERT" not in roles.PRIVILEGES[roles.WORKER_ROLE]["queue_entry"]


QUEUE_TABLES = ("work_item", "work_attempt", "work_result", "work_event")


# ---------------------------------------------------------------------------
# The grants two tasks lose when neither is wrong
# ---------------------------------------------------------------------------
# `render_table_grants()` opens with `REVOKE ALL ... FROM <role>`, which is what
# makes it a complete replacement rather than additive drift — and is exactly
# why the matrix it re-grants from has to be a *union*. Two tasks adding rows
# for the same table is the normal case as the schema grows. Under a dict
# update the later one wins, the earlier one's grants vanish with no error and
# no warning, and both tasks' own suites stay green because each is correct
# alone. The defect only exists once both have landed, which is the one
# arrangement neither task's tests cover.


def test_merging_two_contributions_keeps_both() -> None:
    first = {"shared_table": frozenset({"SELECT"}), "only_first": frozenset({"INSERT"})}
    second = {
        "shared_table": frozenset({"UPDATE"}),
        "only_second": frozenset({"SELECT"}),
    }

    merged = roles.merge_privileges(first, second)

    assert merged["shared_table"] == frozenset({"SELECT", "UPDATE"})
    assert merged["only_first"] == frozenset({"INSERT"})
    assert merged["only_second"] == frozenset({"SELECT"})


def test_a_later_contribution_cannot_silently_replace_an_earlier_one() -> None:
    """The regression, stated as the thing that must not happen.

    Written against the shape the mistake takes rather than against the helper:
    `dict(first) | second` is what anyone reaches for, it type-checks, and it
    drops `SELECT` here.
    """
    first = {"shared_table": frozenset({"SELECT"})}
    second = {"shared_table": frozenset({"UPDATE"})}

    naive = dict(first) | second
    assert naive["shared_table"] == frozenset({"UPDATE"}), "the mistake, reproduced"
    assert "SELECT" not in naive["shared_table"]

    assert "SELECT" in roles.merge_privileges(first, second)["shared_table"]


def test_the_blanket_default_never_widens_a_narrowed_table() -> None:
    """A union can only grow, so the default must stay outside the merge.

    `work_item` is `SELECT` for the app on purpose; if the per-table default
    were merged in as a contribution it would come back as full DML and the
    control plane would regain the direct write the protocol exists to remove.
    """
    assert roles.PRIVILEGES[roles.APP_ROLE]["work_item"] == frozenset({"SELECT"})
    assert roles.PRIVILEGES[roles.APP_ROLE]["run"] == frozenset(
        {"SELECT", "INSERT", "UPDATE"}
    )


def test_merge_column_privileges_keeps_both_contributions() -> None:
    """`COLUMN_PRIVILEGES` carries the same risk one level deeper: role -> table
    -> privilege -> columns. Two contributions naming different tables for the
    same role must both survive the merge."""
    first = {"first_table": {"INSERT": ("a", "b")}}
    second = {"second_table": {"UPDATE": ("c",)}}

    merged = roles.merge_column_privileges(first, second)

    assert merged["first_table"] == {"INSERT": ("a", "b")}
    assert merged["second_table"] == {"UPDATE": ("c",)}


def test_a_later_column_contribution_cannot_silently_replace_an_earlier_one() -> None:
    """The regression, one level deeper than `merge_privileges`' own.

    A second contribution narrowing a table another contribution already
    narrowed must add to it, not replace its whole per-privilege dict — which
    is what the obvious `dict(first) | second` does.
    """
    first = {"shared_table": {"INSERT": ("a",)}}
    second = {"shared_table": {"UPDATE": ("b",)}}

    naive = dict(first) | second
    assert naive["shared_table"] == {"UPDATE": ("b",)}, "the mistake, reproduced"
    assert "INSERT" not in naive["shared_table"]

    merged = roles.merge_column_privileges(first, second)
    assert merged["shared_table"] == {"INSERT": ("a",), "UPDATE": ("b",)}


def test_merge_column_privileges_unions_columns_for_the_same_privilege() -> None:
    """Two contributions can each widen the same table's *same* privilege
    carve-out — the column list must union, not have the second replace the
    first's columns outright."""
    first = {"completion": {"UPDATE": ("review_verdict",)}}
    second = {"completion": {"UPDATE": ("review_summary",)}}

    merged = roles.merge_column_privileges(first, second)

    assert merged["completion"]["UPDATE"] == ("review_verdict", "review_summary")


def test_the_rendered_matrix_covers_every_declared_privilege() -> None:
    """End to end: nothing declared may be missing from the rendered SQL.

    The merge above is only worth anything if the renderer consumes the merged
    result, so this reads the statements rather than the mapping.
    """
    statements = roles.render_table_grants()
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        for table, privileges in roles.PRIVILEGES[role].items():
            if not privileges or table in roles.COLUMN_PRIVILEGES.get(role, {}):
                continue
            granted = [
                s for s in statements if s.endswith(f"ON public.{table} TO {role};")
            ]
            assert len(granted) == 1, f"{role}.{table}"
            for privilege in privileges:
                assert privilege in granted[0], f"{role}.{table}.{privilege}"


def test_no_role_holds_a_table_privilege_on_the_claim_protocol() -> None:
    """The exclusivity argument is a property of the grant graph, not of a WHERE.

    If any role could `UPDATE work_item` directly there would be a second route
    to a claim, and `queue_claim()` would stop being the only place the
    exclusivity has to hold.
    """
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        for table in QUEUE_TABLES:
            assert not {
                p for p in roles.PRIVILEGES[role][table] if p in {"INSERT", "UPDATE"}
            }, f"{role} may write {table}"

    # `work_attempt` holds the capability itself and is readable by nobody.
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        assert roles.PRIVILEGES[role]["work_attempt"] == frozenset()


def test_the_worker_reaches_the_queue_only_through_the_four_protocol_steps() -> None:
    """Two assertions rather than one, because the role now carries two layers.

    The queue half must stay exactly four steps, and the whole set must stay
    exactly what both tasks declared. A single equality would have to be edited
    by every later task that adds a function, and editing it is indistinguishable
    from widening it.
    """
    granted = {s.split("(")[0] for s in roles.FUNCTION_PRIVILEGES[roles.WORKER_ROLE]}
    assert {name for name in granted if name.startswith("queue_")} == {
        "queue_claim",
        "queue_heartbeat",
        "queue_complete",
        "queue_fail",
    }
    # And the enrolment layer: prove its own identity, read only the
    # server-authoritative expiry of that proved credential, rotate its own
    # secret, and nothing else — a worker cannot mint or redeem an enrolment.
    assert granted == {
        "queue_claim",
        "queue_heartbeat",
        "queue_complete",
        "queue_fail",
        "identity_assert",
        "identity_current_credential",
        "enroll_rotate_self",
    }
    assert roles.VIEW_PRIVILEGES[roles.WORKER_ROLE] == {}


def test_the_control_plane_cannot_claim() -> None:
    """Dispatch is not execution.

    Granting the app `queue_claim()` would restore the shape the claim protocol
    exists to remove: a privileged process recording an executor it was merely
    told about.
    """
    granted = {s.split("(")[0] for s in roles.FUNCTION_PRIVILEGES[roles.APP_ROLE]}
    assert "queue_claim" not in granted
    assert {name for name in granted if name.startswith("queue_")} == {
        "queue_enqueue",
        "queue_reap",
        "queue_redrive",
    }
    # The enrolment layer (0003). `identity_revoke_principal` is deliberately
    # absent: taking a host offline is the operator's lever, so a compromised
    # control plane can add to the fleet and cannot take it down.
    # The backlog store (0005, BO-S1): the control plane's whole write path —
    # every mutation is a SECURITY DEFINER function, tables are read-only.
    assert granted == {
        "queue_enqueue",
        "queue_reap",
        "queue_redrive",
        "enroll_mint_ticket",
        "enroll_redeem_ticket",
        "enroll_revoke_ticket",
        "enroll_sweep_expired",
        "identity_sweep_expired",
        "backlog_upsert_task",
        "backlog_transition",
        "backlog_record_evidence",
        "backlog_record_remediation",
        "backlog_add_dependency",
        "backlog_lease_acquire",
        "backlog_lease_heartbeat",
        "backlog_lease_release",
        # BO-S2 (0006) + BO-S3 (0007): dispatch, ingest, return-to-pool.
        "backlog_dispatch",
        "backlog_ingest_results",
        "backlog_return_to_pool",
        # VOYN-W0-AICC-DEFER-AUTO-RESUME (0014): the machine exit from
        # DEFER_TO_USER for technical parks.
        "backlog_resume_deferred",
        # VOYN-OPS-AICC-PUBLISH-WINDOW-STARVATION (0015): the persisted
        # scan cursor for the tick windows.
        "backlog_scan_claim",
        "backlog_triage",
    }


def test_the_worker_can_no_longer_write_the_queue_mirror() -> None:
    """`queue_entry` is a mirror; a claim written there is lost on the next sync."""
    assert roles.PRIVILEGES[roles.WORKER_ROLE]["queue_entry"] == frozenset({"SELECT"})


def test_internal_queue_helpers_are_granted_to_nobody() -> None:
    """`_queue_audit` and `_queue_owns` are SECURITY DEFINER over everything."""
    for role, signatures in roles.FUNCTION_PRIVILEGES.items():
        assert not [s for s in signatures if s.startswith("_")], role


def test_function_grants_are_revoked_before_they_are_reapplied() -> None:
    """Otherwise re-running the matrix widens rather than replaces."""
    statements = roles.render_table_grants()
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        revoke = f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role};"
        assert revoke in statements
        first_grant = next(
            i
            for i, s in enumerate(statements)
            if s.startswith("GRANT EXECUTE ON FUNCTION") and s.endswith(f"TO {role};")
        )
        assert statements.index(revoke) < first_grant


@pytest.mark.parametrize(
    "bad", ["queue_claim(text; DROP TABLE task)", "queue_claim(text"]
)
def test_function_signature_guard_rejects_injection(bad: str) -> None:
    with pytest.raises(ValueError):
        roles._require_function_signature(bad)


def test_a_worker_host_role_is_a_login_member_of_the_worker_group() -> None:
    """Per-host identity with no new machinery, and no password in the SQL."""
    statement = roles.render_worker_host_role("aicc_worker_host_a")[0]
    assert "CREATE ROLE aicc_worker_host_a LOGIN IN ROLE aicc_worker;" in statement
    assert "PASSWORD" not in statement


def test_app_covers_every_table() -> None:
    assert set(roles.PRIVILEGES[roles.APP_ROLE]) == set(roles.ALL_TABLES)


def test_migrator_holds_no_table_grants() -> None:
    """Its rights come from ownership, so a stray grant here would be a smell."""
    assert roles.PRIVILEGES[roles.MIGRATOR_ROLE] == {}


def test_sequence_usage_accompanies_every_insert_grant() -> None:
    statements = roles.render_grants()
    for role in (roles.APP_ROLE, roles.WORKER_ROLE):
        for table, sequence in roles.IDENTITY_SEQUENCES.items():
            expected = f"GRANT USAGE ON SEQUENCE public.{sequence} TO {role};"
            if "INSERT" in roles.PRIVILEGES[role].get(table, frozenset()):
                assert expected in statements, f"{role} inserts into {table}"
            else:
                assert expected not in statements, f"{role} must not advance {sequence}"


def test_identity_sequences_match_the_ddl() -> None:
    """Sequence names are guessed from PostgreSQL's naming rule; verify the guess."""
    from command_center.db import migrations

    identity_tables = set()
    for migration in migrations.discover():
        current: str | None = None
        for line in migration.up_sql.splitlines():
            if line.startswith("CREATE TABLE "):
                current = line.split()[2].rstrip("(")
            elif "GENERATED ALWAYS AS IDENTITY" in line and current:
                identity_tables.add(current)
    assert identity_tables == set(roles.IDENTITY_SEQUENCES)


@pytest.mark.parametrize("bad", ["public; DROP TABLE task", "1schema", "sch-ema"])
def test_identifier_guard_rejects_injection(bad: str) -> None:
    with pytest.raises(ValueError, match="not a safe"):
        roles.render_grants(bad)
