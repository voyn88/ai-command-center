"""VOYN-W0-AICC-BACKLOG-ELIGIBLE-VIEW-OWNER-AFTER-0019(-REM): a migration that
(re)creates a view must restore its ownership to the migrator role in the
same or a later migration -- ordered by numeric version -- because
SECURITY DEFINER functions owned by `aicc_migrator` read it and the role
that runs an upgrade is not fixed (control-01 ran 0019 as `aicc_admin` and
every planner tick died with "permission denied for view backlog_eligible").

The first version of this test only asked whether an owner restore existed
*anywhere* in the series, so a later recreate after an earlier restore would
have passed (adversarial review of b9b82a9e). This one tracks the LAST
create per view and requires an owner restore at that version or after."""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "command_center" / "db" / "sql"
CREATE_VIEW = re.compile(r"CREATE(?: OR REPLACE)? VIEW\s+(\w+)", re.IGNORECASE)
OWNER = re.compile(r"ALTER VIEW\s+(\w+)\s+OWNER TO aicc_migrator", re.IGNORECASE)
#: 0001..0006 created the schema under the migrator itself; the policy
#: applies to every recreate after that baseline.
BASELINE_VERSION = 6


def _events(paths):
    """[(version, kind, view)] in ascending version order, kind in {create, own}."""
    events = []
    for path in sorted(paths, key=lambda p: int(p.name.split("_", 1)[0])):
        version = int(path.name.split("_", 1)[0])
        text = path.read_text(encoding="utf-8")
        for match in CREATE_VIEW.finditer(text):
            events.append((version, "create", match.group(1)))
        for match in OWNER.finditer(text):
            events.append((version, "own", match.group(1)))
    return events


def _violations(events):
    last_create: dict[str, int] = {}
    last_own: dict[str, int] = {}
    for version, kind, view in events:
        if kind == "create":
            last_create[view] = version
        else:
            last_own[view] = version
    return {
        view: version
        for view, version in last_create.items()
        if version > BASELINE_VERSION and last_own.get(view, -1) < version
    }


def test_the_last_recreate_of_every_view_is_followed_by_an_owner_restore():
    missing = _violations(_events(SQL_DIR.glob("*.up.sql")))
    assert not missing, f"views recreated without a same-or-later owner restore: {missing}"


def test_policy_rejects_a_recreate_after_the_last_owner_restore(tmp_path):
    (tmp_path / "0019_a.up.sql").write_text("CREATE VIEW v AS SELECT 1;\n")
    (tmp_path / "0020_b.up.sql").write_text("ALTER VIEW v OWNER TO aicc_migrator;\n")
    (tmp_path / "0021_c.up.sql").write_text("DROP VIEW v; CREATE VIEW v AS SELECT 2;\n")
    assert _violations(_events(tmp_path.glob("*.up.sql"))) == {"v": 21}
    # Restoring in the same migration as the recreate satisfies the policy.
    (tmp_path / "0021_c.up.sql").write_text(
        "DROP VIEW v; CREATE VIEW v AS SELECT 2;\nALTER VIEW v OWNER TO aicc_migrator;\n"
    )
    assert _violations(_events(tmp_path.glob("*.up.sql"))) == {}
    # Numeric, not lexical, ordering: 0100 comes after 0099.
    (tmp_path / "0100_d.up.sql").write_text("CREATE VIEW w AS SELECT 3;\n")
    (tmp_path / "0099_e.up.sql").write_text("ALTER VIEW w OWNER TO aicc_migrator;\n")
    assert _violations(_events(tmp_path.glob("*.up.sql"))) == {"w": 100}


def test_0020_restores_backlog_eligible_owner_and_read_grant():
    text = (SQL_DIR / "0020_backlog_eligible_view_owner.up.sql").read_text(encoding="utf-8")
    assert "ALTER VIEW backlog_eligible OWNER TO aicc_migrator" in text
    assert "GRANT SELECT ON backlog_eligible TO aicc_app" in text
    # Guarded: a database without the production roles must not fail.
    assert "pg_roles" in text
