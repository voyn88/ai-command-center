"""Which classes are mirrors — asked once, answered the same way everywhere.

Originally test-only (`tests/db/mirror_discovery.py`): the mirror contract and
the stored-reader fitness gate both needed "every declared
`PostgresTableMirror`" and each carried its own copy that read
`command_center/db/*_store.py`.

VOYN-W0-AICC-SRV-07's historical backfill is a third caller, and the first
production one — it walks every mirrored table in dependency order to copy
pre-dual-write rows into PostgreSQL, so it needs the same answer the tests
already trust. Moving the module here rather than importing test code from
production keeps the dependency direction the right way round; the tests
import it back (see `tests/db/mirror_discovery.py`) so neither suite gained a
second copy.

Membership is decided by Python, not by spelling: the sources are read only to
choose which modules to *import*, and the set itself comes from
`PostgresTableMirror.__subclasses__()`, transitively. A class cannot lie to
`issubclass`.
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
    the authoritative answer comes from `issubclass` afterwards.

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

    The module comes back with the class because a caller sometimes needs to
    ask a question of it — the fitness gate looks for where the reconciliation
    is declared, the backfill looks for `<table>_divergence` by name —  and
    rediscovering it from the class would be a second rule about layout.

    Two mirrors for one table is refused rather than resolved: a table with two
    mirrors has two opinions about itself, and picking one is not this
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
