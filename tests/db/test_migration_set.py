"""Structural checks on the migration files themselves. No database needed."""

from __future__ import annotations

import json

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


# --- a shipped migration file is immutable ---------------------------------
# VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH. See `migrations.py`'s section
# comment for the incident: a comment-only edit to an already-applied 0022
# made `db upgrade` refuse on the ledger checksum, and the four queue
# migrations that were the actual fix (0024-0027) never reached control-01.
# Every test in this file was green throughout -- nothing here could see it,
# because the authority that refuses lives in a database.


def test_every_migration_file_matches_the_checksum_it_shipped_with() -> None:
    """The guard the incident asked for, in the place the edit is made.

    `verify_released_checksums` compares the same SHA-256 the database's
    `schema_migration` ledger holds, so a red test here is exactly the red
    deploy it prevents -- not a proxy for one.
    """
    migrations.verify_released_checksums()


def test_an_edited_migration_is_refused_by_its_recorded_checksum(tmp_path) -> None:
    """The failure this exists to produce, driven through the real lock.

    The edit is deliberately cosmetic and appended to a COMMENT -- the exact
    shape of the change that caused the incident, and the one that looks
    harmless in review.
    """
    victim = migrations.discover()[1]  # 0002_queue_claim, applied everywhere
    for migration in migrations.discover():
        for path in (migration.up_path, migration.down_path):
            (tmp_path / path.name).write_bytes(path.read_bytes())
    edited = tmp_path / victim.up_path.name
    edited.write_text(
        edited.read_text(encoding="utf-8") + "\n-- typo fixed\n", encoding="utf-8"
    )

    with pytest.raises(migrations.MigrationError) as refusal:
        migrations.verify_released_checksums(tmp_path)
    message = str(refusal.value)
    assert f"{victim.name}.up.sql changed after it shipped" in message
    # The message has to carry the consequence, or the next reader "fixes" it
    # by re-locking: the edit does not break the file it touched, it blocks
    # every LATER migration from being applied.
    assert "blocks every LATER migration" in message


def test_a_new_migration_must_be_recorded_before_it_ships(tmp_path) -> None:
    """A lock that only covered what it already knew would let the next
    migration ship unpinned, and the guard would decay to whatever it was
    created with."""
    for migration in migrations.discover():
        for path in (migration.up_path, migration.down_path):
            (tmp_path / path.name).write_bytes(path.read_bytes())
    _write_pair(tmp_path, len(migrations.discover()) + 1, "brand_new")

    with pytest.raises(migrations.MigrationError, match="not recorded in"):
        migrations.verify_released_checksums(tmp_path)


def test_a_shipped_migration_cannot_be_deleted(tmp_path) -> None:
    """Deleting the file does not delete the ledger row that names it: a
    database that applied it reports a version this build no longer defines
    and refuses to migrate at all."""
    keep = migrations.discover()[:-1]
    for migration in keep:
        for path in (migration.up_path, migration.down_path):
            (tmp_path / path.name).write_bytes(path.read_bytes())

    with pytest.raises(migrations.MigrationError, match="no file defines them"):
        migrations.verify_released_checksums(tmp_path)


def test_regenerating_the_lock_is_a_no_op_for_an_unchanged_set() -> None:
    """`--write` must not produce a diff on a clean tree, or the file becomes
    noise in every branch that runs it and the one meaningful diff hides."""
    assert (
        migrations.render_released_lock()
        == migrations.RELEASED_LOCK_PATH.read_text(encoding="utf-8")
    )


def test_an_unreadable_lock_is_a_verdict_not_a_traceback(monkeypatch, tmp_path) -> None:
    """The guard's own failure path obeys the rule the guard exists for.

    `migration-lock` catches `MigrationError` and prints it; anything else
    reaches the operator as the traceback that hid the real answer last time
    (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH). A deleted lock is the
    likely way in -- removing the file that is refusing is the obvious wrong
    move -- so the message also has to say why `--write` is not the repair.
    """
    monkeypatch.setattr(migrations, "RELEASED_LOCK_PATH", tmp_path / "gone.json")
    with pytest.raises(migrations.MigrationError, match="is missing") as refusal:
        migrations.released_lock()
    assert "Restore it from version control" in str(refusal.value)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(migrations, "RELEASED_LOCK_PATH", malformed)
    with pytest.raises(migrations.MigrationError, match="not readable as a migration lock"):
        migrations.released_lock()


def test_the_lock_names_the_command_that_regenerates_it() -> None:
    """The note is the only instruction a reader gets at the moment they are
    staring at a refusal, so it has to name a command that exists. It moved
    once already (`python -m command_center.db migration-lock --write` ->
    `scripts/migration_lock.py --write`), and a note pointing at a removed
    flag would send the reader to an argparse error instead of the repair."""
    note = json.loads(
        migrations.RELEASED_LOCK_PATH.read_text(encoding="utf-8")
    )["note"]
    assert "python scripts/migration_lock.py --write" in note
    assert "python -m command_center.db migration-lock" in note
    assert "command_center.db migration-lock --write" not in note


def test_the_regeneration_script_writes_exactly_what_render_produces(
    monkeypatch, tmp_path, capsys
) -> None:
    """`--write` is `render_released_lock()` and a write, nothing else.

    Regeneration lives in `scripts/` rather than on `python -m
    command_center.db` because a durable write inside a package named `db` is
    precisely the persistence-engine signature the AIOS boundary gate reads
    (docs/AIOS_BOUNDARY.md); see `test_the_database_cli_is_not_a_persistence_
    engine` in tests/db/test_cli_queue.py.
    """
    from scripts.migration_lock import main as lock_main

    destination = tmp_path / "released.lock.json"
    monkeypatch.setattr(migrations, "RELEASED_LOCK_PATH", destination)

    assert lock_main(["--write"]) == 0
    assert destination.read_text(encoding="utf-8") == migrations.render_released_lock()
    assert str(destination) in capsys.readouterr().out


def test_the_regeneration_script_reports_drift_as_a_verdict(
    monkeypatch, capsys
) -> None:
    """Without `--write` the script is the same check the CLI runs, and it
    obeys the same rule: exit 2 and a readable message, never a traceback."""
    from scripts.migration_lock import main as lock_main

    assert lock_main([]) == 0
    assert f"{len(migrations.discover())} migrations unchanged" in capsys.readouterr().out

    def refuse(sql_dir=None):
        raise migrations.MigrationError("0022_queue_fail_lease_wait.up.sql changed")

    monkeypatch.setattr(migrations, "verify_released_checksums", refuse)
    assert lock_main([]) == 2
    assert "migration lock: 0022_queue_fail_lease_wait.up.sql changed" in (
        capsys.readouterr().err
    )
