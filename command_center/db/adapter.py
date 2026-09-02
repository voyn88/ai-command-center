"""The single seam between AI Command Center and the `aios-db` library.

Every generic PostgreSQL primitive this package uses — pool lifecycle, advisory
locks, migration execution, the connectivity probe — is owned by AIOS Core and
consumed through here. Nothing else in this repository imports `aios_db`, for
the same reason nothing but `command_center/application/aios_tasks.py` imports
`aios_sdk`: one file to review when the contract changes, and one file for the
boundary gate to allow.

What stays on this side of the seam is the domain: the 33-table schema and its
migrations, the `aicc_*` roles and grants, the repositories, the backup and
restore policy, and the composition that decides what "ready" means for this
service. AIOS knows none of those, and this module does not export anything
that would let it learn them.

The re-exports are deliberately narrow. A blanket `from aios_db import *` would
make every future addition to that library part of AICC's surface without
anyone deciding so.
"""

from __future__ import annotations

from aios_db import (
    AdvisoryLockError,
    AdvisoryLockTimeout,
    AiosDbError,
    DB_CONTRACT,
    Migration,
    MigrationChecksumMismatch,
    MigrationError,
    MigrationRunner,
    PoolError,
    ProbeResult,
    advisory_lock,
    advisory_xact_lock,
    check_connectivity,
    discover,
    lock_key,
    open_pool,
    pool_stats,
    try_advisory_lock,
)

__all__ = [
    "AdvisoryLockError",
    "AdvisoryLockTimeout",
    "AiosDbError",
    "DB_CONTRACT",
    "Migration",
    "MigrationChecksumMismatch",
    "MigrationError",
    "MigrationRunner",
    "PoolError",
    "ProbeResult",
    "advisory_lock",
    "advisory_xact_lock",
    "check_connectivity",
    "discover",
    "lock_key",
    "open_pool",
    "pool_stats",
    "try_advisory_lock",
]
