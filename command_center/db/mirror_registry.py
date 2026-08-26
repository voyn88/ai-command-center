"""Which classes are mirrors — asked once, answered the same way everywhere.

Originally a test helper (`tests/db/mirror_discovery.py`): two suites needed
this list, the contract that enrols every mirror in the per-table checks and
the stored-reader fitness gate that asks the authority package a question per
mirrored table. A third consumer arrived with the cutover reconciliation gate
(`command_center.db.reconcile`, VOYN-W0-AICC-SRV-07b) — production code that
must discover every mirrored table to run its own `divergence` function
against it, and production code cannot depend on `tests/`. Moved rather than
imported across the boundary, so the one rule about what counts as a mirror
has one home instead of a production copy drifting from the test one.

Slice 9's acceptance showed what a second implementation costs. A mirror
declared in `command_center/db/run_mirror.py` with a deliberately wrong key was
collected by neither existing copy, passed everything, and the contract still
reported seventeen tables — the blocking defect of that slice, relocated by
renaming a file. Fixing one copy would have left the other, which is the
argument for this module existing at all: a rule with two implementations has
two answers eventually.

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
make every consumer collectible only on a machine with a PostgreSQL client
library — and the serverless configuration is exactly where the declaration
checks are the only ones still running.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import TypeVar

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


_M = TypeVar("_M", bound=PostgresTableMirror)


def _every_subclass(root: type[_M]) -> list[type[_M]]:
    """`root`'s subclasses, transitively — a subclass of a mirror is a mirror.

    Bound to `PostgresTableMirror` rather than plain `type` so the result
    still carries `.spec` for `mirror_classes()` below — a bare `type` return
    would type-check `mirror_classes()`'s `subclass.spec.table` as an error on
    every call rather than the missing declaration `__init_subclass__` already
    refuses at import time.
    """
    seen: list[type[_M]] = []
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
