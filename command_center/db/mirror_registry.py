"""Which classes are mirrors — asked once, answered the same way everywhere.

Originally test-only (`tests/db/mirror_discovery.py`), written for the two
suites that each need this list: the contract, which enrols every mirror in
the per-table checks, and the stored-reader fitness gate, which asks the
authority package a question per mirrored table. They had a copy each, and
both copies read `command_center/db/*_store.py`.

Historical backfill (VOYN-W0-AICC-SRV-07) is the first *production* caller —
it walks the same set, in the same dependency order the contract already
proves against the schema, to copy pre-existing rows into their mirrors. A
third copy of the discovery rule would have been the same duplication the
docstring below was written to end, so this module moved rather than grew a
sibling; `tests/db/mirror_discovery` re-exports these two names for the tests
that still import it by that path.

Slice 9's acceptance showed what a second copy costs. A mirror declared in
`command_center/db/run_mirror.py` with a deliberately wrong key was collected by
neither, passed everything, and the contract still reported seventeen tables —
the blocking defect of that slice, relocated by renaming a file. Fixing one copy
would have left the other, which is the argument for this module existing at
all: a rule with two implementations has two answers eventually.

Slice 10's acceptance then showed that the first fix was still a rule about
*spelling*. Membership was decided by the literal text `PostgresTableMirror`
appearing in a class's bases, so three shapes still escaped, each verified to
pass every check while declaring a wrong key: an aliased base (`import
PostgresTableMirror as Base`), a subclass of an existing mirror, and a class
defined inside a function. Membership is now decided by Python: the sources are
read only to choose which modules to *import*, and the set itself comes from
`PostgresTableMirror.__subclasses__()`, transitively. A class cannot lie to
`issubclass`.

Sources are still read rather than the package imported wholesale:
`command_center/db/pool.py` imports `aios_db`, so importing everything would
make both suites collectible only on a machine with a PostgreSQL client library
— and the serverless configuration is exactly where the declaration checks are
the only ones still running. Historical backfill needs `aios_db`/`psycopg`
regardless (it writes to PostgreSQL), but keeping this module import-light
means it costs nothing extra for the two test suites that do not.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from command_center.db.table_mirror import PostgresTableMirror

__all__ = ["mirror_classes", "modules_declaring_mirrors"]

#: What a module must mention to be worth importing. Deliberately broader than
#: "declares a subclass": the cost of a false positive is one import, and the
#: cost of a false negative is a mirror nothing checks.
_MARKERS = ("table_mirror", "PostgresTableMirror")


def modules_declaring_mirrors() -> list[str]:
    """Dotted names of `command_center.db` modules that might declare a mirror.

    Text, not AST, and that is the point: this decides only what to import, and
    the authoritative answer comes from `issubclass` afterwards. An AST rule
    that looked for the base by name is what slice 10's acceptance walked
    around three different ways.

    `rglob`, so a future `command_center/db/<subpackage>/` cannot hide a
    mirror by being one directory deeper.
    """
    import command_center.db as db_package

    package_root = Path(db_package.__path__[0])
    found: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if not any(marker in source for marker in _MARKERS):
            continue
        relative = path.relative_to(package_root).with_suffix("")
        found.append("command_center.db." + ".".join(relative.parts))
    return found


def _every_subclass(root: type) -> list[type]:
    """`root`'s subclasses, transitively — a subclass of a mirror is a mirror."""
    seen: list[type] = []
    for subclass in root.__subclasses__():
        seen.append(subclass)
        seen.extend(_every_subclass(subclass))
    return seen


def mirror_classes() -> dict[str, tuple[type[PostgresTableMirror], object]]:
    """`{table: (mirror class, its module)}` for every declared mirror.

    The module comes back with the class because the fitness gate asks a
    question of it — where the reconciliation for this table is declared — and
    rediscovering it from the class would be a second rule about layout.

    Two mirrors for one table is refused rather than resolved. The statement
    probe already refused it; this rule silently kept whichever sorted last, so
    a second declaration could hide a broken production mirror from every
    check — slice 10's acceptance proved exactly that, by giving the real
    `task` mirror a wrong key and adding a correct-looking duplicate. A table
    with two mirrors has two opinions about itself, and picking one is not this
    module's decision to make.
    """
    for module_name in modules_declaring_mirrors():
        importlib.import_module(module_name)

    found: dict[str, tuple[type[PostgresTableMirror], object]] = {}
    for subclass in _every_subclass(PostgresTableMirror):
        if not subclass.__module__.startswith("command_center.db"):
            continue
        table = subclass.spec.table
        if table in found and found[table][0] is not subclass:
            first = found[table][0]
            raise AssertionError(
                f"two mirrors declare `{table}`: {first.__module__}.{first.__name__} and "
                f"{subclass.__module__}.{subclass.__name__}. A table with two mirrors has "
                "two opinions about its statements, and the checks would have run against "
                "only one of them."
            )
        found[table] = (subclass, sys.modules[subclass.__module__])
    return dict(sorted(found.items()))
