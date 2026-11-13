"""Partial recovery is source-scoped and cannot erase newer observations."""

import pytest


def test_partial_recovery_preserves_active_other_source_and_newer_findings(
    pg_connection_factory, admin_conn
):
    for source, failure in [("control:queue", "queue_stalled"),
                            ("control:queue", "dead_letter_growth"),
                            ("other:queue", "queue_stalled")]:
        admin_conn.execute("SELECT monitor_record_finding(%s,%s)", (source, failure))
    cutoff = admin_conn.execute("SELECT clock_timestamp()").fetchone()[0]
    admin_conn.execute("SELECT monitor_record_finding('control:queue','newer_failure')")
    with pg_connection_factory() as conn:
        args = ("control:queue", '["dead_letter_growth"]', cutoff)
        sql = "SELECT monitor_clear_resolved_findings(%s,%s::jsonb,%s)"
        assert conn.execute(sql, args).fetchone()[0] == 1
        assert conn.execute(sql, args).fetchone()[0] == 0
    rows = admin_conn.execute("SELECT source,failure,state FROM monitor_finding").fetchall()
    assert set(rows) == {("control:queue", "queue_stalled", "cleared"),
                         ("control:queue", "dead_letter_growth", "open"),
                         ("control:queue", "newer_failure", "open"),
                         ("other:queue", "queue_stalled", "open")}
    with pg_connection_factory() as conn:
        assert conn.execute(
            "SELECT monitor_clear_resolved_findings('control:queue','[]',clock_timestamp())"
        ).fetchone()[0] == 2
    # No accidental PUBLIC EXECUTE on the new SECURITY DEFINER entry point.
    assert admin_conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) acl "
        "WHERE p.oid='monitor_clear_resolved_findings(text,jsonb,timestamptz)'::regprocedure "
        "AND acl.grantee=0 AND acl.privilege_type='EXECUTE')"
    ).fetchone()[0]


@pytest.mark.parametrize("active", [None, "null", "{}", '[null]', '[""]', '[1]'])
def test_malformed_snapshot_cannot_clear_findings(pg_connection_factory, admin_conn, active):
    import psycopg

    admin_conn.execute("SELECT monitor_record_finding('control:queue','queue_stalled')")
    with pytest.raises(psycopg.Error), pg_connection_factory() as conn:
        conn.execute("SELECT monitor_clear_resolved_findings(%s,%s::jsonb,clock_timestamp())",
                     ("control:queue", active))
    assert admin_conn.execute("SELECT state FROM monitor_finding").fetchone()[0] == "open"
