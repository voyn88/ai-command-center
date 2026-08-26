"""Open conflicts from owner-relevant domain events.

``ConflictIntake`` subscribes to the in-process event bus and, when an
:class:`~command_center.events.IncidentOpened` is published, opens a
:class:`Conflict` through the ``runtime.db.conflict`` repository. The conflict
board therefore assembles itself from what actually happened operationally,
rather than depending on every incident source to also remember to POST.

Idempotency: every event-opened conflict carries a stable ``source_ref``
(``incident:<id>``); the subscriber skips creation when a conflict with that ref
already exists, so a redelivered or replayed ``IncidentOpened`` never opens a
second conflict (dedup by ``source_ref``).

Redaction: an incident attributed to a BANK/LEGAL project is dropped — no
conflict is opened, so a sensitive incident's reference never lands on the
board. This mirrors the service's write-side rejection.

The mapping from incident to conflict ``kind`` is configuration
(:class:`ConflictIntakeConfig`), not a hard-coded predicate, so an operator can
retarget it without touching the subscriber. An operational incident has no
intrinsic merge/perf/budget/security class, so it defaults to the ``perf``
bucket (the operator reclassifies on the board); the incident ``severity`` is
carried straight through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from command_center.events import EventBus, IncidentOpened, default_bus
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.conflict import CONFLICT_SEVERITIES
from command_center.runtime.db.core import resolve_db_path

# Repo root is three levels up: <root>/command_center/conflicts/intake.py
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ConflictIntakeConfig:
    """What class of conflict an ``IncidentOpened`` opens.

    * ``incident_kind`` — the conflict ``kind`` an operational incident maps to
      (an incident carries no intrinsic kind; ``perf`` is the default bucket).
    * ``incident_severities`` — the severities that open a conflict. Empty (the
      default) means *every* incident opens one; a non-empty set gates intake to
      those severities only.
    """

    incident_kind: str = "perf"
    incident_severities: frozenset[str] = field(default_factory=frozenset)


#: The process default: every incident opens a ``perf`` conflict.
DEFAULT_INTAKE_CONFIG = ConflictIntakeConfig()


class ConflictIntake:
    """Subscribes to the bus and opens conflicts from operational events."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        config: ConflictIntakeConfig = DEFAULT_INTAKE_CONFIG,
    ) -> None:
        self._root = root
        self._config = config
        self._unsubscribes: list = []

    def _db_path(self) -> Path:
        path = resolve_db_path(self._root)
        if db.current_schema_version(path) < db.SCHEMA_VERSION:
            db.migrate(path)
        return path

    # -- registration ------------------------------------------------------

    def register(self, bus: EventBus) -> "ConflictIntake":
        """Subscribe on ``bus``. Returns self so callers keep the instance to
        :meth:`unregister` later (tests, shutdown)."""
        self._unsubscribes.append(bus.subscribe(IncidentOpened, self.on_incident_opened))
        return self

    def unregister(self) -> None:
        for off in self._unsubscribes:
            off()
        self._unsubscribes.clear()

    # -- bus handlers ------------------------------------------------------

    def on_incident_opened(self, event: IncidentOpened) -> dict | None:
        """Open one conflict for ``event`` unless it is gated out, its severity
        is unknown, its project is sensitive, or a conflict already exists for
        the same incident (dedup by ``source_ref``). Returns the created row, or
        ``None`` when nothing was opened."""
        gate = self._config.incident_severities
        if gate and event.severity not in gate:
            return None
        severity = event.severity if event.severity in CONFLICT_SEVERITIES else "sev3"
        project_ref = event.project_ref or None
        if project_ref and is_sensitive(project_ref):
            return None
        source_ref = f"incident:{event.incident_id}"
        path = self._db_path()
        if db.get_conflict_by_source_ref(path, source_ref) is not None:
            return None
        return db.create_conflict(
            path,
            kind=self._config.incident_kind,
            source_ref=source_ref,
            severity=severity,
            project_ref=project_ref,
        )


#: Guards against double-registration on the default bus (import is once per
#: process, but an explicit re-import or reload must not stack subscribers).
_DEFAULT_INTAKE: ConflictIntake | None = None


def install_default_intake() -> ConflictIntake:
    """Install (once) the default incident→conflict intake on the process-wide
    bus and return it. Idempotent: repeated calls return the same instance
    without stacking subscriptions."""
    global _DEFAULT_INTAKE
    if _DEFAULT_INTAKE is None:
        _DEFAULT_INTAKE = ConflictIntake().register(default_bus())
    return _DEFAULT_INTAKE
