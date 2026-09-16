"""Structural checks on the migration files themselves. No database needed."""

from __future__ import annotations

from pathlib import Path

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


# ---------------------------------------------------------------------------
# Static guards the reviewers of 0029 asked for: things a reader of one diff
# cannot verify, pinned by the whole migration set.
# ---------------------------------------------------------------------------

_SQL_DIR = Path(migrations.__file__).resolve().parent / "sql"


def _function_bodies(text: str, names: tuple[str, ...]) -> dict[str, str]:
    """`name -> body` for every `CREATE [OR REPLACE] FUNCTION name(` in `text`,
    the body running to the closing `$$;`, normalised to `CREATE FUNCTION`."""
    import re

    found: dict[str, str] = {}
    for name in names:
        pattern = re.compile(
            rf"^CREATE (?:OR REPLACE )?FUNCTION {re.escape(name)}\(.*?^\$\$;\n",
            re.DOTALL | re.MULTILINE,
        )
        matches = pattern.findall(text)
        if matches:
            assert len(matches) == 1, (name, len(matches))
            found[name] = matches[0].replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION", 1)
    return found


def test_every_migration_uses_balanced_dollar_quoting() -> None:
    """A bare `$` delimiter is a syntax error PostgreSQL only reports at apply
    time; a reviewer reading a diff envelope cannot tell `$$` from a
    normalised `$`. Every migration must open and close with `$$` (or a
    tagged `$tag$`) an even number of times and never end a line on a bare
    ` $` or ` $;`."""
    import re

    for path in sorted(_SQL_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        assert text.count("$$") % 2 == 0, f"{path.name}: unbalanced $$"
        for number, line in enumerate(text.splitlines(), 1):
            assert not re.search(r"(^|\s)\$;?\s*$", line), f"{path.name}:{number}: bare $"


def test_0029_down_restores_the_0003_bodies_verbatim() -> None:
    """0029's down claims 0003 is the only prior definition of the three
    functions it restores. Both halves are checked here: the restored bodies
    are byte-for-byte the 0003 bodies, and no other up migration defines any
    of them, so the down is a revert to the immediately prior definition."""
    names = ("identity_assert", "identity_issue_db_credential", "enroll_rotate_self")
    origin = _function_bodies(
        (_SQL_DIR / "0003_worker_enrollment.up.sql").read_text(encoding="utf-8"), names
    )
    assert set(origin) == set(names)
    down = _function_bodies(
        (_SQL_DIR / "0029_worker_credential_self_renewal_grace.down.sql").read_text(
            encoding="utf-8"
        ),
        names,
    )
    assert set(down) == set(names)
    for name in names:
        assert down[name] == origin[name], f"{name}: down body differs from 0003"
    for path in sorted(_SQL_DIR.glob("*.up.sql")):
        if path.name.startswith(("0003_", "0029_")):
            continue
        redefined = _function_bodies(path.read_text(encoding="utf-8"), names)
        assert redefined == {}, f"{path.name} redefines {sorted(redefined)}"


def test_0029_down_drops_exactly_the_functions_its_up_created() -> None:
    """The up adds overloads with `CREATE FUNCTION` (never `OR REPLACE`); the
    down must drop exactly those signatures, by exact signature, so no
    grace-capable overload can stay resident beside a restored 0003 body and
    no unrelated function is dropped. Argument NAMES are stripped so the two
    spellings compare as signatures."""
    import re

    up = (_SQL_DIR / "0029_worker_credential_self_renewal_grace.up.sql").read_text(encoding="utf-8")
    down = (_SQL_DIR / "0029_worker_credential_self_renewal_grace.down.sql").read_text(encoding="utf-8")

    def signature(name: str, args: str) -> str:
        types = []
        for arg in filter(None, (a.strip() for a in args.split(","))):
            types.append(arg.split()[-1])  # `p_secret text` -> `text`, `text` -> `text`
        return f"{name}({', '.join(types)})"

    created = {
        signature(name, args)
        for name, args in re.findall(r"^CREATE FUNCTION (\w+)\(([^)]*)\)", up, re.MULTILINE)
    }
    dropped = {
        signature(name, args)
        for name, args in re.findall(r"^DROP FUNCTION IF EXISTS (\w+)\(([^)]*)\);", down, re.MULTILINE)
    }
    assert created == dropped, (created ^ dropped)
    # And nothing the up merely REPLACES is dropped by the down.
    replaced = set(re.findall(r"^CREATE OR REPLACE FUNCTION (\w+)\(", up, re.MULTILINE))
    assert not {d.split("(")[0] for d in dropped} & (replaced - {"identity_assert"}), replaced
