"""Bounded lineage refusal and execution ordering; no production connections."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import aicc_cto_migration as cli


def _manifest(catalog=None):
    return {"schema": "aicc-cto-migration/v1", "target": 36,
            "execution_host": "approved-host", "connection_host": "192.0.2.10", "connection_port": 5433,
            "identity_evidence_sha256": "e" * 64, "connection_evidence_sha256": "c" * 64, "migration_session_user": "aicc_migrator",
            "identity": {"dbname": "aicc_preprod", "server_address": "192.0.2.10",
                         "server_port": 5433, "system_identifier": "123456789",
                         "database_oid": 42, "server_major": 16},
            "migrations": [{"version": i, "slug": f"migration_{i}",
                            "checksum": str(i % 10) * 64, "down_checksum": "d" * 64}
                           for i in range(1, 37)],
            "catalog_sha256": {"34": cli.catalog_hash(catalog or []),
                               "35": cli.catalog_hash(catalog or [])}}


class Connection:
    autocommit = True

    def __init__(self, manifest, baseline=34):
        self.events = []
        self.info = SimpleNamespace(host=manifest["connection_host"], port=manifest["connection_port"])
        self.identity = tuple(manifest["identity"].values()) + ("aicc_migrator",)
        self.rows = [(r["version"], r["slug"], r["checksum"])
                     for r in manifest["migrations"][:baseline]]
        self.catalog = []
        self.depth = 0

    def execute(self, sql):
        assert self.depth == 1, "preflight reads must stay under the outer migration lock"
        self.events.append(sql)
        if sql == cli.IDENTITY_SQL:
            return SimpleNamespace(fetchone=lambda: self.identity)
        if sql == cli.LEDGER_SQL:
            return SimpleNamespace(fetchall=lambda: self.rows)
        if sql == cli.CATALOG_SQL:
            return SimpleNamespace(fetchall=lambda: self.catalog)
        assert sql == "SET search_path TO public"
        return None


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "approved-host")
    @contextmanager
    def lock(conn, key, **kwargs):
        assert key == cli.adapter.lock_key("aicc:schema-migration")
        assert kwargs == {"wait_timeout": 10}
        conn.depth += 1
        conn.events.append("lock")
        try:
            yield
        finally:
            conn.events.append("unlock")
            conn.depth -= 1
    monkeypatch.setattr(cli.adapter, "advisory_lock", lock)


@pytest.mark.parametrize("baseline", [34, 35])
def test_plan_validates_without_runner_or_ddl(monkeypatch, locked, baseline):
    manifest = _manifest()
    conn = Connection(manifest, baseline)
    monkeypatch.setattr(cli.migrations, "runner", lambda *a: pytest.fail("plan must not construct runner"))
    result = cli.execute(conn, manifest, Path("unused"))
    assert result == {"mode": "plan", "baseline": baseline, "target": 36,
                      "pending": list(range(baseline+1, 37)), "applied": []}
    assert conn.events[0] == "lock" and conn.events[-1] == "unlock"


@pytest.mark.parametrize("mutation,reason", [
    ("dbname", "target_identity_mismatch"), ("server", "target_identity_mismatch"),
    ("role", "target_identity_mismatch"), ("old36", "unsupported_live_lineage"),
    ("empty", "unsupported_live_lineage"), ("gap", "live_checksum_mismatch"),
    ("checksum", "live_checksum_mismatch"), ("unexpected35", "live_checksum_mismatch"),
    ("old_policy_artifact", "schema_artifact_mismatch"),
    ("unexpected_foundation", "schema_artifact_mismatch"),
])
def test_refusals_precede_runner(monkeypatch, locked, mutation, reason):
    manifest = _manifest()
    conn = Connection(manifest, 35 if mutation == "unexpected35" else 34)
    if mutation in ("dbname", "server", "role"):
        values = list(conn.identity)
        values[{"dbname": 0, "server": 3, "role": 6}[mutation]] = "foreign"
        conn.identity = tuple(values)
    elif mutation == "old36":
        conn.rows += [(35, "old", "x"), (36, "old_policy", "y")]
    elif mutation == "empty":
        conn.rows = []
    elif mutation == "gap":
        conn.rows[4] = conn.rows[5]
    elif mutation in ("checksum", "unexpected35"):
        conn.rows[-1] = (*conn.rows[-1][:2], "bad")
    else:
        conn.catalog = [("relation", "delivery_team_stage" if mutation == "unexpected_foundation" else "cto_assignment_policy", {})]
    monkeypatch.setattr(cli.migrations, "runner", lambda *a: pytest.fail("refused preflight must not construct runner"))
    with pytest.raises(cli.Refusal, match=reason):
        cli.execute(conn, manifest, Path("unused"), apply=True)
    assert conn.depth == 0


def test_apply_runner_uses_same_connection_and_lock(monkeypatch, locked):
    manifest = _manifest()
    conn = Connection(manifest, 35)
    monkeypatch.setattr(cli, "verify_snapshot", lambda *args: None)
    def upgrade(got, *, target):
        assert got is conn and got.depth == 1 and target == 36
        assert got.events[-1] == cli.CATALOG_SQL
        got.events.append("runner upgrade")
        got.rows += [(36, "migration_36", "6" * 64)]
        return (36,)
    monkeypatch.setattr(cli.migrations, "runner", lambda path: SimpleNamespace(upgrade=upgrade))
    assert cli.execute(conn, manifest, Path("approved"), apply=True)["applied"] == [36]
    assert conn.events[-3:] == ["runner upgrade", cli.LEDGER_SQL, "unlock"]


def test_manifest_pin_and_duplicate_keys_fail_closed(tmp_path):
    path = tmp_path / "approval.json"
    raw = json.dumps(_manifest()).encode()
    path.write_bytes(raw)
    assert cli.load_manifest(path, hashlib.sha256(raw).hexdigest())["target"] == 36
    with pytest.raises(cli.Refusal, match="manifest_hash_mismatch"):
        cli.load_manifest(path, "0" * 64)
    raw = raw.replace(b'"target": 36', b'"target": 36, "target": 36')
    path.write_bytes(raw)
    with pytest.raises(cli.Refusal, match="duplicate_manifest_key"):
        cli.load_manifest(path, hashlib.sha256(raw).hexdigest())


def test_approved_source_snapshot_pins_both_directions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manifest = _manifest()
    for row in manifest["migrations"]:
        for direction, key in [("up", "checksum"), ("down", "down_checksum")]:
            raw = f"-- {row['version']} {direction}\nSELECT 1;\n".encode()
            path = source / f"{row['version']:04d}_{row['slug']}.{direction}.sql"
            path.write_bytes(raw)
            row[key] = hashlib.sha256(raw).hexdigest()
    with cli.approved_source(source, manifest) as snapshot:
        original = source / "0035_migration_35.up.sql"
        original.write_text("SELECT 'changed after validation';")
        assert (snapshot / original.name).read_text() == "-- 35 up\nSELECT 1;\n"
    assert not snapshot.exists()
    with pytest.raises(cli.Refusal, match="source_checksum_mismatch"):
        with cli.approved_source(source, manifest):
            pytest.fail("changed source accepted")


def test_catalog_hash_tracks_definition_not_fetch_order():
    rows = [("function", "f()", {"body": "first"}), ("relation", "t", {})]
    assert cli.catalog_hash(rows) == cli.catalog_hash(list(reversed(rows)))
    assert cli.catalog_hash(rows) != cli.catalog_hash([("function", "f()", {"body": "changed"}), rows[1]])


def test_cli_connection_failure_does_not_leak_dsn(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "approved.json"
    raw = json.dumps(_manifest()).encode()
    manifest.write_bytes(raw)
    monkeypatch.setenv("AICC_CTO_MIGRATION_DSN", "private-dsn-do-not-print")
    monkeypatch.setattr(cli, "validate_identity_evidence", lambda *args: None)
    monkeypatch.setattr(cli, "validate_connection_evidence", lambda *args: None)
    @contextmanager
    def source(*args):
        yield tmp_path
    monkeypatch.setattr(cli, "approved_source", source)
    def failing_pool(*args, **kwargs):
        assert kwargs["autocommit"] and kwargs["min_size"] == kwargs["max_size"] == 1
        raise RuntimeError("private-dsn-do-not-print")
    monkeypatch.setattr(cli.adapter, "open_pool", failing_pool)
    assert cli.main(["--manifest", str(manifest), "--manifest-sha256", hashlib.sha256(raw).hexdigest(),
                     "--sql-dir", str(tmp_path), "--identity-evidence", str(manifest), "--connection-evidence", str(manifest)]) == 1
    captured = capsys.readouterr()
    assert "private-dsn" not in captured.out + captured.err
    assert json.loads(captured.out)["reason"] == "migration_preflight_or_apply_failed"


def test_cloned_database_on_wrong_connection_target_is_refused(monkeypatch, locked):
    manifest = _manifest()
    conn = Connection(manifest)
    conn.info.host = "192.0.2.99"
    with pytest.raises(cli.Refusal, match="connection_target_mismatch"):
        cli.execute(conn, manifest, Path("unused"), apply=True)
    assert cli.IDENTITY_SQL not in conn.events


def test_source_changed_after_staging_refuses_before_runner(monkeypatch, locked):
    manifest = _manifest()
    conn = Connection(manifest)
    def changed(*args):
        assert conn.depth == 1
        raise cli.Refusal("source_checksum_mismatch")
    monkeypatch.setattr(cli, "verify_snapshot", changed)
    monkeypatch.setattr(cli.migrations, "runner", lambda *args: pytest.fail("must not run DDL"))
    with pytest.raises(cli.Refusal, match="source_checksum_mismatch"):
        cli.execute(conn, manifest, Path("approved"), apply=True)


def test_identity_evidence_is_bound_to_reviewed_target_and_first34(tmp_path):
    manifest = _manifest()
    source = {"manifest": {
        "cluster_identity": {"system_identifier": "123456789", "server_version_num": "160015", "port": "5433"},
        "database_identity": {"name": "aicc_preprod", "oid": "42"},
        "host_observation": {"hostname": "approved-host"},
        "ledger": manifest["migrations"][:34]}}
    path = tmp_path / "identity.json"
    raw = json.dumps(source).encode()
    path.write_bytes(raw)
    manifest["identity_evidence_sha256"] = hashlib.sha256(raw).hexdigest()
    cli.validate_identity_evidence(path, manifest)
    manifest["identity"]["database_oid"] = 43
    with pytest.raises(cli.Refusal, match="identity_evidence_mismatch"):
        cli.validate_identity_evidence(path, manifest)
    manifest["identity"]["database_oid"] = 42
    manifest["migrations"][0]["checksum"] = "f" * 64
    with pytest.raises(cli.Refusal, match="identity_evidence_lineage_mismatch"):
        cli.validate_identity_evidence(path, manifest)


def test_connection_evidence_binds_endpoint_to_identity_evidence(tmp_path):
    manifest = _manifest()
    manifest["connection_host"] = "/var/run/postgresql"
    source = {"approved_manifest": {"sha256": manifest["identity_evidence_sha256"],
              "database": "aicc_preprod", "database_oid": "42", "system_identifier": "123456789"},
              "selection": {"directory": "/var/run/postgresql", "exists": True, "is_socket": True},
              "snapshot": {"hostname": "approved-host", "port_show": "5433"}}
    raw = json.dumps(source).encode()
    path = tmp_path / "connection.json"
    path.write_bytes(raw)
    manifest["connection_evidence_sha256"] = hashlib.sha256(raw).hexdigest()
    cli.validate_connection_evidence(path, manifest)
    manifest["connection_host"] = "/another/socket"
    with pytest.raises(cli.Refusal, match="connection_evidence_mismatch"):
        cli.validate_connection_evidence(path, manifest)


def test_explicit_approved_existing_migration_login_is_used(locked):
    manifest = _manifest()
    manifest["migration_session_user"] = "approved_existing_migration_login"
    conn = Connection(manifest)
    conn.identity = (*conn.identity[:6], manifest["migration_session_user"])
    assert cli.execute(conn, manifest, Path("unused"))["mode"] == "plan"


def test_unix_socket_identity_keeps_all_pins(locked):
    manifest = _manifest()
    manifest["identity"]["server_address"] = None
    manifest["connection_host"] = "/var/run/postgresql"
    conn = Connection(manifest)
    conn.identity = (*conn.identity[:2], None, *conn.identity[3:])
    assert cli.execute(conn, manifest, Path("unused"))["mode"] == "plan"
