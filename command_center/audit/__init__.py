"""The Audit engine (VOYN-W2-AUD) — pluggable in-repo checks that produce
findings, each always carrying a status and an owner.

Pipeline::

    checks -> Finding value objects -> AuditRunner.collect (dedup)
           -> api/audit_service (persist audit_run + audit_findings, +events)

Public surface::

    from command_center.audit import AuditRunner, default_registry, Finding

The domain here (checks, registry, runner, value objects) is storage-free and
network-free; persistence, BANK/LEGAL redaction, status/owner enforcement and
event publishing live in the Wave-2 write service
(:mod:`command_center.api.audit_service`), the single seam that touches the
``audit_run``/``audit_finding`` repository and the tasks single-writer.
"""

from __future__ import annotations

from command_center.audit.registry import CheckRegistry, default_registry
from command_center.audit.runner import AuditRunner, CollectResult
from command_center.audit.types import (
    CheckContext,
    Finding,
    default_owner_for,
)

__all__ = [
    "AuditRunner",
    "CollectResult",
    "CheckContext",
    "CheckRegistry",
    "Finding",
    "default_owner_for",
    "default_registry",
]
