"""Structural checks on the migration files themselves. No database needed."""

from __future__ import annotations

import pytest

from command_center.db import migrations


def _write_pair(directory, version: int, slug: str, up: str = "SELECT 1;") -> None:
    (directory / f"{version:04d}_{slug}.up.sql").write_text(up, encoding="utf-8")
    (directory / f"{version:04d}_{slug}.down.sql").write_text("SELECT 1;", encoding="utf-8")


def test_repository_migration_set_is_valid() -> None:
    found = migrations.discover()
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    assert found[0].slug == "initial"


def test_every_migration_has_a_downgrade() -> None:
    for migration in migrations.discover():
        assert migration.down_path.exists(), migration.slug


def test_missing_downgrade_is_rejected(tmp_path) -> None:
    (tmp_path / "0001_initial.up.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrations.MigrationError, match="no down-migration"):
        migrations.discover(tmp_path)


def test_version_gap_is_rejected(tmp_path) -> None:
    _write_pair(tmp_path, 1, "initial")
    _write_pair(tmp_path, 3, "later")
    with pytest.raises(migrations.MigrationError, match="contiguous"):
        migrations.discover(tmp_path)


def test_unparseable_filename_is_rejected(tmp_path) -> None:
    _write_pair(tmp_path, 1, "initial")
    (tmp_path / "hotfix.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrations.MigrationError, match="does not match"):
        migrations.discover(tmp_path)


def test_checksum_tracks_file_contents(tmp_path) -> None:
    _write_pair(tmp_path, 1, "initial", up="SELECT 1;")
    before = migrations.discover(tmp_path)[0].checksum
    (tmp_path / "0001_initial.up.sql").write_text("SELECT 2;", encoding="utf-8")
    after = migrations.discover(tmp_path)[0].checksum
    assert before != after


def test_the_migration_set_covers_the_declared_table_inventory() -> None:
    """The DDL and `roles.ALL_TABLES` must not drift apart.

    `ALL_TABLES` drives the grant matrix, so a table added to the schema
    without a matching entry there would end up with no declared access policy.

    Every migration, not only the first. Reading `discover()[0]` was correct
    while there was one migration and would have gone on passing afterwards
    while checking nothing about the second — the shape of assertion that
    weakens silently as the thing it guards grows.
    """
    from command_center.db import roles

    created = {
        line.split()[2].rstrip("(")
        for migration in migrations.discover()
        for line in migration.up_sql.splitlines()
        if line.startswith("CREATE TABLE ")
    }
    # `schema_migration` is created by the runner itself, not by a migration.
    assert created == set(roles.ALL_TABLES) - {"schema_migration"}


def test_the_migration_set_covers_the_declared_view_inventory() -> None:
    """Same rule for views, which carry their own grants."""
    from command_center.db import roles

    created = {
        line.split()[2]
        for migration in migrations.discover()
        for line in migration.up_sql.splitlines()
        if line.startswith("CREATE VIEW ")
    }
    assert created == set(roles.ALL_VIEWS)


def test_duplicate_down_migration_is_rejected(tmp_path) -> None:
    _write_pair(tmp_path, 1, "initial")
    (tmp_path / "0001_other.down.sql").write_text("SELECT 1;", encoding="utf-8")
    # Lower-cased since the message now comes from `aios-db`, which follows the
    # library convention of lower-case exception text.
    with pytest.raises(migrations.MigrationError, match="duplicate down-migration"):
        migrations.discover(tmp_path)


def test_orphan_down_migration_is_rejected(tmp_path) -> None:
    _write_pair(tmp_path, 1, "initial")
    (tmp_path / "0002_stray.down.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrations.MigrationError, match="no matching up-migration"):
        migrations.discover(tmp_path)


def test_expected_schema_version_tracks_the_migration_set() -> None:
    """A hand-maintained constant here would eventually go un-bumped.

    The failure mode is severe: the deploy migrates successfully, then every
    replica reports schema_mismatch and 503s.
    """
    from command_center.db import health

    assert health.EXPECTED_SCHEMA_VERSION == len(migrations.discover())
