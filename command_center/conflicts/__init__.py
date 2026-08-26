"""Wave-2 Conflicts/Incidents engine.

An operator-facing surface built on the runtime store and the in-process event
bus. A :class:`~command_center.api.models.Conflict` is a tracked friction — a
merge collision, a perf regression, a budget overrun, a security exposure — that
moves through ``open → mitigating → resolved`` and may only be resolved once it
carries a mitigation *and* an owner (the engine's core invariant, enforced in
:mod:`command_center.conflicts.service`).

Two entry points:

* :mod:`command_center.conflicts.service` — the service tier behind the
  ``/api/v1/conflicts`` routes (create/list/get + the assign/mitigate/resolve
  workflow), including the BANK/LEGAL redaction policy.
* :class:`command_center.conflicts.intake.ConflictIntake` — subscribes to the
  bus and opens a conflict from every ``IncidentOpened`` event (dedup by
  ``source_ref``).

Production wiring: importing this package installs one :class:`ConflictIntake`
on the process-wide event bus (:func:`install_default_intake`, idempotent) so
incidents open conflicts without any startup edit. Tests construct their own
:class:`ConflictIntake` against an isolated bus and never rely on this global.
"""

from __future__ import annotations

from command_center.conflicts.intake import (
    ConflictIntake,
    ConflictIntakeConfig,
    DEFAULT_INTAKE_CONFIG,
    install_default_intake,
)

__all__ = [
    "ConflictIntake",
    "ConflictIntakeConfig",
    "DEFAULT_INTAKE_CONFIG",
    "install_default_intake",
]

install_default_intake()
