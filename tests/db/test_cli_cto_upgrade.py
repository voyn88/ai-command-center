"""The actual public upgrade command cannot bypass the CTO lineage preflight."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center.db import cli
from ops import aicc_cto_migration as guarded


ENVIRONMENT = (
    "AICC_CTO_MIGRATION_MANIFEST",
    "AICC_CTO_MIGRATION_MANIFEST_SHA256",
    "AICC_CTO_MIGRATION_IDENTITY_EVIDENCE",
    "AICC_CTO_MIGRATION_CONNECTION_EVIDENCE",
)


@pytest.fixture
def setup(monkeypatch):
    state = SimpleNamespace(events=[], depth=0, applied=tuple(range(1, 35)),
                            available=tuple(range(1, 37)), checksum_error=False)
    conn = SimpleNamespace(autocommit=True)
    state.conn = conn
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: "existing-config")
    monkeypatch.setattr(cli.pool, "open_pool", lambda config: state.events.append(("open", config)))
    monkeypatch.setattr(cli.pool, "close_pool", lambda: state.events.append("close"))
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(cli.pool, "connection", connection)
    @contextmanager
    def lock(got, key, **kwargs):
        assert got is conn and key == cli.adapter.lock_key("aicc:schema-migration")
        assert kwargs["wait_timeout"] == 10
        state.depth += 1
        state.events.append("lock")
        try:
            yield
        finally:
            state.events.append("unlock")
            state.depth -= 1
    monkeypatch.setattr(cli.adapter, "advisory_lock", lock)
    def discover():
        assert state.depth == 1
        state.events.append("discover")
        return tuple(SimpleNamespace(version=v) for v in state.available)
    def applied_versions(got):
        assert got is conn and state.depth == 1
        state.events.append("applied")
        return state.applied
    def verify(got, migrations):
        assert got is conn and state.depth == 1
        state.events.append("verify")
        if state.checksum_error:
            raise RuntimeError("private-connection-or-sql-value")
    monkeypatch.setattr(cli.migrations, "runner", lambda: SimpleNamespace(
        discover=discover, applied_versions=applied_versions, verify_checksums=verify))
    def upgrade(got):
        assert got is conn and state.depth == 1
        state.events.append("legacy-upgrade")
        return tuple(v for v in state.available if v not in state.applied)
    monkeypatch.setattr(cli.migrations, "upgrade", upgrade)
    def grants(got):
        assert got is conn and state.depth == 1
        state.events.append("grants")
        return 7
    monkeypatch.setattr(cli.roles, "apply_table_grants", grants)
    return state


def _approved(monkeypatch, state):
    for name, value in zip(ENVIRONMENT, ("/approved/release.json", "a" * 64,
                                       "/approved/identity.json", "/approved/connection.json"), strict=True):
        monkeypatch.setenv(name, value)
    manifest = {"reviewed": True}
    def load(path, digest):
        assert state.depth == 1 and state.events[-1] == "verify"
        assert path == Path("/approved/release.json") and digest == "a" * 64
        state.events.append("manifest")
        return manifest
    monkeypatch.setattr(guarded, "load_manifest", load)
    def evidence(path, got):
        assert state.depth == 1 and got is manifest
        state.events.append(path.name)
    monkeypatch.setattr(guarded, "validate_identity_evidence", evidence)
    monkeypatch.setattr(guarded, "validate_connection_evidence", evidence)
    @contextmanager
    def source(directory, got):
        assert state.depth == 1 and got is manifest and directory == cli.migrations.SQL_DIR
        state.events.append("snapshot")
        yield Path("/private/verified-snapshot")
    monkeypatch.setattr(guarded, "approved_source", source)
    def execute(conn, got, snapshot, *, apply):
        assert state.depth == 1 and conn is state.conn and got is manifest
        assert snapshot == Path("/private/verified-snapshot") and apply is True
        state.events.append("guarded-apply")
        return {"applied": list(range(state.applied[-1]+1, 37))}
    monkeypatch.setattr(guarded, "execute", execute)


@pytest.mark.parametrize("last", [34, 35])
def test_actual_upgrade_missing_manifest_refuses_before_ddl_or_grants(setup, last, capsys):
    setup.applied = tuple(range(1, last+1))
    assert cli.main(["upgrade"]) == 1
    assert "cto_migration_manifest_required" in capsys.readouterr().err
    assert "legacy-upgrade" not in setup.events and "grants" not in setup.events
    assert setup.events[-2:] == ["unlock", "close"]


@pytest.mark.parametrize("applied", [(), (1, 2, 4), tuple(range(1, 38))])
def test_empty_gapped_or_ahead_ledger_never_reaches_upgrade(setup, applied):
    setup.applied = applied
    assert cli.main(["upgrade"]) == 1
    assert "verify" not in setup.events
    assert "legacy-upgrade" not in setup.events and "grants" not in setup.events


@pytest.mark.parametrize("last", [34, 35, 36])
def test_checksum_drift_blocks_both_guarded_and_legacy_paths(setup, monkeypatch, last, capsys):
    setup.applied = tuple(range(1, last+1))
    setup.checksum_error = True
    _approved(monkeypatch, setup)
    assert cli.main(["upgrade"]) == 1
    assert "manifest" not in setup.events and "legacy-upgrade" not in setup.events
    assert "grants" not in setup.events
    output = capsys.readouterr()
    assert "private-connection-or-sql-value" not in output.out + output.err


@pytest.mark.parametrize("last", [34, 35])
def test_valid_guarded_upgrade_uses_existing_connection_then_grants(setup, monkeypatch, last, capsys):
    setup.applied = tuple(range(1, last+1))
    _approved(monkeypatch, setup)
    assert cli.main(["upgrade"]) == 0
    assert "legacy-upgrade" not in setup.events
    assert setup.events[-4:] == ["guarded-apply", "grants", "unlock", "close"]
    assert "re-asserted 7 table grants" in capsys.readouterr().out


@pytest.mark.parametrize("failure_at", ["load_manifest", "validate_identity_evidence", "validate_connection_evidence", "approved_source", "execute"])
def test_guarded_failure_has_no_legacy_fallback_or_grants(setup, monkeypatch, failure_at):
    _approved(monkeypatch, setup)
    def refuse(*args, **kwargs):
        raise ValueError("private-path-or-dsn")
    monkeypatch.setattr(guarded, failure_at, refuse)
    assert cli.main(["upgrade"]) == 1
    assert "legacy-upgrade" not in setup.events and "grants" not in setup.events


@pytest.mark.parametrize("source_last,expected", [(36, "already up to date"), (37, "applied: [37]")])
def test_existing_verified_36_retains_normal_noop_and_future_upgrade(setup, source_last, expected, capsys):
    setup.applied = tuple(range(1, 37))
    setup.available = tuple(range(1, source_last+1))
    assert cli.main(["upgrade"]) == 0
    assert setup.events[-5:] == ["verify", "legacy-upgrade", "grants", "unlock", "close"]
    assert expected in capsys.readouterr().out


def test_existing34_source34_still_verifies_and_restores_grants(setup):
    setup.available = tuple(range(1, 35))
    assert cli.main(["upgrade"]) == 0
    assert setup.events[-5:] == ["verify", "legacy-upgrade", "grants", "unlock", "close"]


def test_pool_failure_is_sanitized_without_new_connection_fallback(setup, monkeypatch, capsys):
    def failure(*args):
        raise RuntimeError("private-dsn")
    monkeypatch.setattr(cli.pool, "open_pool", failure)
    assert cli.main(["upgrade"]) == 1
    assert "private-dsn" not in capsys.readouterr().err
    assert setup.events == ["close"]
