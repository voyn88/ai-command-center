"""Re-export of the mirror registry, kept for the suites already importing it.

The discovery logic itself moved to `command_center/db/mirror_registry.py`
(`VOYN-W0-AICC-SRV-07`): the legacy importer is a third caller that needs the
same table set and dependency order, and it is not a test, so a production
module cannot import it from `tests/`. This module is now a thin alias so
`tests/db/test_mirror_contract.py`, `test_mirror_coverage.py` and
`test_stored_reader_fitness.py` keep working unchanged.
"""

from __future__ import annotations

from command_center.db.mirror_registry import (  # noqa: F401
    mirror_classes,
    modules_declaring_mirrors,
)

__all__ = ["mirror_classes", "modules_declaring_mirrors"]
