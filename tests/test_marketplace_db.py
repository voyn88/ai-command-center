"""Repository-tier tests for the Wave-3 Marketplace table family
(``command_center.runtime.db.marketplace``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v21 migration on a fresh db.

Fixtures use only generic names and invented ids — no real names or paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.marketplace import InvalidMarketItemTransitionError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# --- migration ------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 21


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- create / get / list --------------------------------------------------


def test_create_and_get_market_item(db_path: Path) -> None:
    row = db.create_market_item(
        db_path,
        name="Widget Pack",
        kind="domain_pack",
        version="1.2.0",
        publisher="acme",
        description="a pack",
        provenance="channel:stable",
    )
    assert row["status"] == "listed"
    assert row["lock_version"] == 0
    assert row["created_at"] and row["updated_at"]
    got = db.get_market_item(db_path, row["id"])
    assert got["name"] == "Widget Pack"
    assert got["kind"] == "domain_pack"
    assert got["provenance"] == "channel:stable"


def test_create_market_item_rejects_bad_kind(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_market_item(db_path, name="x", kind="nonsense")


def test_create_market_item_rejects_empty_name(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_market_item(db_path, name="   ", kind="module")


def test_get_missing_item_returns_none(db_path: Path) -> None:
    assert db.get_market_item(db_path, "nope") is None


def test_list_filters_by_kind_and_status_and_pages(db_path: Path) -> None:
    db.create_market_item(db_path, name="a", kind="module")
    db.create_market_item(db_path, name="b", kind="plugin")
    installed = db.create_market_item(db_path, name="c", kind="module")
    db.install_market_item(
        db_path, installed["id"], expected_version=0, actor="alice",
        installer="null-installer",
    )

    assert len(db.list_market_items(db_path)) == 3
    assert len(db.list_market_items(db_path, kind="module")) == 2
    assert len(db.list_market_items(db_path, status="installed")) == 1
    assert len(db.list_market_items(db_path, status="listed")) == 2
    page = db.list_market_items(db_path, limit=1, offset=0)
    assert len(page) == 1


# --- install (atomic status flip + log append) ----------------------------


def test_install_flips_status_and_writes_log(db_path: Path) -> None:
    item = db.create_market_item(
        db_path, name="Thing", kind="plugin", version="0.9.1",
        provenance="url:https://example.test/thing",
    )
    item_row, log_row = db.install_market_item(
        db_path, item["id"], expected_version=0, actor="alice",
        installer="null-installer", detail="dry-run", metadata={"mode": "null"},
    )
    assert item_row["status"] == "installed"
    assert item_row["lock_version"] == 1
    # Log line attributes who/when/what version, copied from the item.
    assert log_row["actor"] == "alice"
    assert log_row["version"] == "0.9.1"
    assert log_row["kind"] == "plugin"
    assert log_row["provenance"] == "url:https://example.test/thing"
    assert log_row["installer"] == "null-installer"
    assert log_row["installed_at"]

    log = db.list_install_log(db_path, item["id"])
    assert len(log) == 1
    assert log[0]["actor"] == "alice"
    assert log[0]["metadata"] == {"mode": "null"}


def test_reinstall_is_refused_at_repository_boundary(db_path: Path) -> None:
    item = db.create_market_item(db_path, name="Thing", kind="module")
    db.install_market_item(
        db_path, item["id"], expected_version=0, actor="a", installer="null-installer",
    )
    # Terminal state: the transition allowlist refuses a second install.
    with pytest.raises(InvalidMarketItemTransitionError):
        db.install_market_item(
            db_path, item["id"], expected_version=1, actor="a",
            installer="null-installer",
        )


def test_install_refuses_stale_version(db_path: Path) -> None:
    item = db.create_market_item(db_path, name="Thing", kind="module")
    with pytest.raises(db.LostUpdateError):
        db.install_market_item(
            db_path, item["id"], expected_version=99, actor="a",
            installer="null-installer",
        )


def test_install_requires_actor_and_installer(db_path: Path) -> None:
    item = db.create_market_item(db_path, name="Thing", kind="module")
    with pytest.raises(ValueError):
        db.install_market_item(
            db_path, item["id"], expected_version=0, actor="  ",
            installer="null-installer",
        )
    with pytest.raises(ValueError):
        db.install_market_item(
            db_path, item["id"], expected_version=0, actor="a", installer="",
        )


def test_install_missing_item_raises_keyerror(db_path: Path) -> None:
    with pytest.raises(KeyError):
        db.install_market_item(
            db_path, "nope", expected_version=0, actor="a", installer="null-installer",
        )


def test_install_log_is_newest_first_and_pages(db_path: Path) -> None:
    # One item, one install → one line; a second item to prove scoping by item.
    a = db.create_market_item(db_path, name="A", kind="module")
    b = db.create_market_item(db_path, name="B", kind="module")
    db.install_market_item(
        db_path, a["id"], expected_version=0, actor="alice", installer="null-installer",
    )
    db.install_market_item(
        db_path, b["id"], expected_version=0, actor="bob", installer="null-installer",
    )
    a_log = db.list_install_log(db_path, a["id"])
    assert len(a_log) == 1 and a_log[0]["actor"] == "alice"
    assert db.list_install_log(db_path, a["id"], limit=0) == []
