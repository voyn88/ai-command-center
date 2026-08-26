"""Test-side alias for the mirror registry.

Slice 9's acceptance showed what two copies of "which classes are mirrors"
costs: a mirror declared with a deliberately wrong key was collected by
neither, passed everything, and the contract still reported the old count.

VOYN-W0-AICC-SRV-07's historical backfill made this a production question too
— it walks every mirrored table in dependency order — so the discovery logic
now lives at `command_center.db.mirror_registry` and this module is the thin
re-export both test suites already import. Keeping the name and the import
path stable here means neither suite needed to change a single import.
"""

from __future__ import annotations

from command_center.db.mirror_registry import mirror_classes, modules_declaring_mirrors

__all__ = ["mirror_classes", "modules_declaring_mirrors"]
