"""The fleet's single-panel view over enrolled worker-host devices.

VOYN-MIN-FARM (edge-fleet): a Sales/SRE acceptance framed as "10 devices
managed by one operational panel." The devices are the `principal` rows
0003_worker_enrollment created for `kind = 'worker_host'` — each one an
execution host admitted through the enrolment protocol, carrying its own
lifecycle state, credential and audit trail. Nothing before this module could
answer "what does the fleet look like right now" in one query: an operator
had to read `principal`, `principal_credential_public` and `principal_event`
separately and correlate them by hand.

This is deliberately as thin as `work_queue_admin.WorkQueueAdmin`: the
database owns every rule that matters (which columns a role may read, who may
revoke a device), and this module is only the operator seam — the callable the
CLI reaches. `list_devices()` runs under whatever role connects, so its
result already reflects that role's grants; `suspend()` calls
`identity_revoke_principal()`, which is granted to `aicc_operator` alone
(revoking a host is an incident decision, not a routine one — see
`command_center/db/roles.py`), so a control-plane connection raises
`psycopg.errors.InsufficientPrivilege` rather than this module inventing its
own, second copy of that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["FleetDevice", "FleetAdmin", "UnknownDeviceError"]


class UnknownDeviceError(Exception):
    """Raised by `FleetAdmin.suspend` when `principal_id` names no enrolled
    worker-host device — refused before the privileged call, so a typo never
    reaches (and is never mistaken for a denial from) `identity_revoke_principal`."""


@dataclass(frozen=True, slots=True)
class FleetDevice:
    """One worker-host `principal`, with the credential and audit context an
    operator needs to act on it — the row a fleet panel renders."""

    principal_id: str
    display_name: str
    host: str | None
    state: str
    trust_tier: int
    enrolled_by: str | None
    created_at: str
    updated_at: str
    #: Expiry of the live (unrevoked) credential, or `None` if the device has
    #: never been issued one.
    credential_expires_at: str | None
    #: The most recent `principal_event` recorded for this device.
    last_event_type: str | None
    last_event_outcome: str | None
    last_event_at: str | None


class FleetAdmin:
    """List and manage enrolled worker-host devices — the fleet — over the
    identity protocol 0003_worker_enrollment shipped."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    # -- the panel --------------------------------------------------------

    def list_devices(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[FleetDevice]:
        """Every enrolled worker-host device, newest first — the single query
        an operational panel renders from. `state=None` lists the whole
        fleet; narrowing to `"active"`/`"suspended"`/`"retired"` answers
        "which devices need attention" without a second round trip."""
        sql = (
            "SELECT p.principal_id, p.display_name, p.host, p.state,"
            " p.trust_tier, p.enrolled_by, p.created_at, p.updated_at,"
            " c.expires_at, e.event_type, e.outcome, e.created_at"
            " FROM principal p"
            " LEFT JOIN LATERAL ("
            "   SELECT expires_at FROM principal_credential_public"
            "   WHERE principal_id = p.principal_id AND revoked_at IS NULL"
            "   ORDER BY created_at DESC LIMIT 1"
            " ) c ON true"
            " LEFT JOIN LATERAL ("
            "   SELECT event_type, outcome, created_at FROM principal_event"
            "   WHERE principal_id = p.principal_id"
            "   ORDER BY seq DESC LIMIT 1"
            " ) e ON true"
            " WHERE p.kind = 'worker_host'"
        )
        params: list[Any] = []
        if state is not None:
            sql += " AND p.state = %s"
            params.append(state)
        sql += " ORDER BY p.created_at DESC LIMIT %s"
        params.append(max(int(limit), 1))
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [
            FleetDevice(
                principal_id=row[0],
                display_name=row[1],
                host=row[2],
                state=row[3],
                trust_tier=int(row[4]),
                enrolled_by=row[5],
                created_at=str(row[6]),
                updated_at=str(row[7]),
                credential_expires_at=str(row[8]) if row[8] is not None else None,
                last_event_type=row[9],
                last_event_outcome=row[10],
                last_event_at=str(row[11]) if row[11] is not None else None,
            )
            for row in rows
        ]

    # -- lifecycle ----------------------------------------------------------

    def suspend(self, principal_id: str, reason: str) -> int:
        """Suspend a device and revoke its live credentials.

        Returns the number of credentials revoked. Raises `UnknownDeviceError`
        for a `principal_id` that names no worker-host device; raises
        `psycopg.errors.InsufficientPrivilege` (unchanged, not wrapped) when
        the connected role is not `aicc_operator` — that denial is the grant
        graph's, and this module does not soften or duplicate it.
        """
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM principal"
                    " WHERE principal_id = %s AND kind = 'worker_host'",
                    (principal_id,),
                )
                if cur.fetchone() is None:
                    raise UnknownDeviceError(principal_id)
                cur.execute(
                    "SELECT identity_revoke_principal(%s, %s)",
                    (principal_id, reason),
                )
                return int(cur.fetchone()[0])
