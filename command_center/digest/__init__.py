"""Wave-1 «Мой день» + Дайджест engine.

Two owner-facing surfaces built on the Wave-1 persistence/events layer:

* :class:`DigestService` — assembles a deterministic, actionable morning digest
  per day from the real in-repo sources, idempotently (rebuild replaces).
* :class:`OwnerAutofill` — mirrors owner-relevant domain events onto «Мой день»,
  plus direct seams for Board/networking sources that land later.

Production wiring: importing this package installs one :class:`OwnerAutofill` on
the process-wide event bus (:func:`install_default_autofill`, idempotent) so
that owner-relevant events auto-fill the day list without any startup edit to
``app.py``. Tests construct their own :class:`OwnerAutofill` against an isolated
bus and never rely on this global.
"""

from __future__ import annotations

from command_center.digest.owner_autofill import OwnerAutofill, complete_owner_item
from command_center.digest.owner_gates import DEFAULT_OWNER_GATES, OwnerGateConfig
from command_center.digest.service import DigestService, today_str
from command_center.events import default_bus

__all__ = [
    "DigestService",
    "today_str",
    "OwnerAutofill",
    "complete_owner_item",
    "OwnerGateConfig",
    "DEFAULT_OWNER_GATES",
    "install_default_autofill",
]

#: Guards against double-registration on the default bus (import is once per
#: process, but an explicit re-import or reload must not stack subscribers).
_DEFAULT_AUTOFILL: OwnerAutofill | None = None


def install_default_autofill() -> OwnerAutofill:
    """Install (once) the default owner-item auto-fill on the process-wide bus
    and return it. Idempotent: repeated calls return the same instance without
    stacking subscriptions."""
    global _DEFAULT_AUTOFILL
    if _DEFAULT_AUTOFILL is None:
        _DEFAULT_AUTOFILL = OwnerAutofill().register(default_bus())
    return _DEFAULT_AUTOFILL


install_default_autofill()
