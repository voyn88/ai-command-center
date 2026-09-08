"""VOYN-W0-AICC-BACKLOG-ELIGIBLE-VIEW-OWNER-AFTER-0019: a migration that
(re)creates a view must restore its ownership to the migrator role in the
same migration series, because SECURITY DEFINER functions owned by
`aicc_migrator` read it and the role that runs an upgrade is not fixed
(control-01 ran 0019 as `aicc_admin` and every planner tick died with
"permission denied for view backlog_eligible")."""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "command_center" / "db" / "sql"
CREATE_VIEW = re.compile(r"CREATE(?: OR REPLACE)? VIEW\s+(\w+)", re.IGNORECASE)
OWNER = re.compile(r"ALTER VIEW\s+(\w+)\s+OWNER TO aicc_migrator", re.IGNORECASE)


def _ups():
    return sorted(SQL_DIR.glob("*.up.sql"))


def test_every_view_recreated_after_0006_gets_its_owner_restored():
    created_after: dict[str, str] = {}
    owned: set[str] = set()
    for path in _ups():
        version = int(path.name.split("_", 1)[0])
        text = path.read_text(encoding="utf-8")
        for match in CREATE_VIEW.finditer(text):
            if version > 6:
                created_after.setdefault(match.group(1), path.name)
        for match in OWNER.finditer(text):
            owned.add(match.group(1))
    missing = {view: where for view, where in created_after.items() if view not in owned}
    assert not missing, f"views recreated without an owner restore: {missing}"


def test_0020_restores_backlog_eligible_owner_and_read_grant():
    text = (SQL_DIR / "0020_backlog_eligible_view_owner.up.sql").read_text(encoding="utf-8")
    assert "ALTER VIEW backlog_eligible OWNER TO aicc_migrator" in text
    assert "GRANT SELECT ON backlog_eligible TO aicc_app" in text
    # Guarded: a database without the production roles must not fail.
    assert "pg_roles" in text
