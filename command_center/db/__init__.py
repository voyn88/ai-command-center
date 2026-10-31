"""PostgreSQL foundation for the server deployment of AI Command Center.

Scope of this package (VOYN-W0-AICC-SRV-01a): configuration, pooling, the
migration runner, the least-privilege role matrix and the health probes — the
substrate a server install needs before anything is stored in it. Moving the
runtime store off SQLite onto this seam is the follow-up slice
(VOYN-W0-AICC-SRV-01b); until that lands, `command_center.runtime.db` remains
the authority for existing installs.

The generic PostgreSQL primitives underneath — pool lifecycle, advisory locks,
migration execution, the connectivity probe — are AIOS Core's, consumed through
the single `adapter` module (VOYN-W0-AIOS-DB-01 / VOYN-W0-AICC-SRV-01a). The
schema, the roles and grants, the repositories, the backup policy and the
readiness composition stay here.

Submodules are imported lazily by callers rather than re-exported here, so
importing `command_center.db` does not pull in `aios_db` or `psycopg` — the
desktop and CLI entry points must keep working on machines with no PostgreSQL
client library.
"""

from __future__ import annotations

__all__ = [
    "adapter",
    "config",
    "health",
    "legacy_migration",
    "migrations",
    "mirror_registry",
    "pool",
    "roles",
]
