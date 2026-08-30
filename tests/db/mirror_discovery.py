"""Backward-compatible re-export.

The discovery rule this module used to define now lives in
`command_center.db.mirror_registry` (VOYN-W0-AICC-SRV-07): historical backfill
became a second production caller that needs the same "every declared mirror,
in dependency order" answer the test suites already relied on, and a third
hand-copy of the rule was the exact duplication its docstring was written to
end. This file stays so the tests that already `from tests.db.mirror_discovery
import mirror_classes` keep working unchanged.
"""

from __future__ import annotations

from command_center.db.mirror_registry import (  # noqa: F401
    mirror_classes,
    modules_declaring_mirrors,
)

__all__ = ["mirror_classes", "modules_declaring_mirrors"]
