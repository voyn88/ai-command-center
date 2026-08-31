"""What a caller may do here: the authorization half, AICC-owned and closed.

Deliberately the same shape as :mod:`command_center.db.roles`: one declarative
inventory that is the single source of truth, rendered nowhere else and
asserted from both sides by tests. ``roles.py`` answers the question for
PostgreSQL roles; this answers it for HTTP principals. Same idiom, different
subject — not a second identity authority.

The split this module exists to enforce::

    the platform says WHO you are   -- principal_id, from whoami
    AICC says WHAT you may do here  -- this inventory, keyed by that id

That split is load-bearing, not decoration. ``GET /api/v1/whoami`` returns a
principal id, a tenant id and a handful of *platform-global* capabilities.
None of them mentions AICC, and there is no audience field. So a 200 from
``whoami`` proves only that the caller holds a live credential *somewhere in
the platform*. Every service account any operator ever issued, for any
purpose, gets a 200. Treating that as permission would hand write access to
AICC to the platform's entire principal set — the confused-deputy failure a
naive "call whoami, accept if 200" implementation walks straight into.

Hence: authentication is delegated, authorization is local and deny-by-default.
An operation absent from :data:`OPERATIONS` is unreachable; a principal absent
from the grant map can do nothing.

Where the grants live. The map is configuration, not source: a JSON file named
by ``AICC_HTTP_GRANTS_FILE`` mapping ``principal_id -> [operation, ...]``. An
absent file is not an error and not an implicit allow — it is an empty map, so
an unconfigured deployment refuses every mutating request. A file naming an
operation outside :data:`OPERATIONS` raises at load: a typo must not quietly
become a grant that never matches, nor a guard that never fires.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "OPERATIONS",
    "GrantsConfigurationError",
    "UnknownOperationError",
    "is_permitted",
    "load_grants",
    "reset_grants_cache",
]

#: Environment variable naming the JSON grant map.
GRANTS_FILE_ENV = "AICC_HTTP_GRANTS_FILE"


class UnknownOperationError(LookupError):
    """Code asked about an operation outside the closed inventory.

    A typo in an operation name must fail loudly at the call site. Returning
    ``False`` instead would turn a misspelled guard into a permanently denying
    one that looks like it works; returning ``True`` is worse. Neither is
    acceptable for an access-control primitive, so an unknown name is a
    programming error.
    """


class GrantsConfigurationError(RuntimeError):
    """The grant map exists but cannot be trusted. Refuse rather than guess."""


#: The closed inventory of mutating operations at the AICC HTTP boundary.
#: Every entry is reachable from exactly one row of
#: :data:`command_center.http_auth.routing.ROUTE_OPERATIONS`, and the fitness
#: tests assert the two agree in both directions — an operation nothing routes
#: to is dead, and a route with no operation cannot be authorized.
OPERATIONS: frozenset[str] = frozenset(
    {
        # command_center/api/wave1_routes.py
        "proposals:create",
        "proposals:promote",
        "tasks:reorder",
        "advisor:run",
        "owner-items:create",
        "owner-items:complete",
        "digest:build",
        "digest:create",
        # command_center/api/conflict_routes.py
        "conflicts:create",
        "conflicts:assign",
        "conflicts:mitigate",
        "conflicts:resolve",
        # command_center/api/audit_routes.py
        "audit:run",
        "audit:finding:status",
        "audit:finding:promote",
        # command_center/api/council_routes.py
        "council:motion:create",
        "council:motion:vote",
        "council:motion:close",
        # command_center/api/marketplace_routes.py
        "marketplace:item:create",
        "marketplace:item:install",
        # command_center/api/model_registry_routes.py
        "models:register",
        "models:download",
        "models:assign",
        # command_center/api/networking_routes.py
        "networking:contact:create",
        "networking:message:send",
        "networking:feedback:submit",
        "networking:invite",
        # command_center/webapi/queue_routes.py (VOYN-W0-APP-CONTROL-S1/S4)
        "queue:audit:enqueue",
        # command_center/dispatch/api.py
        "dispatch:assign",
        "dispatch:policy:update",
        # command_center/api/backlog_intake_routes.py (VOYN-W0-APP-CONTROL-S6a)
        "backlog:intake:draft",
        "backlog:intake:confirm",
    }
)

_EMPTY: MappingProxyType[str, frozenset[str]] = MappingProxyType({})

_cache: tuple[str, float, MappingProxyType[str, frozenset[str]]] | None = None


def reset_grants_cache() -> None:
    """Drop the memoised grant map. Used by tests and by config reloads."""
    global _cache
    _cache = None


def _parse(raw: object, source: str) -> MappingProxyType[str, frozenset[str]]:
    if not isinstance(raw, dict):
        raise GrantsConfigurationError(
            f"{source}: expected a JSON object at the top level"
        )
    parsed: dict[str, frozenset[str]] = {}
    for principal_id, operations in raw.items():
        if not isinstance(principal_id, str) or not principal_id:
            raise GrantsConfigurationError(
                f"{source}: principal ids must be non-empty strings"
            )
        if not isinstance(operations, list) or not all(
            isinstance(o, str) for o in operations
        ):
            raise GrantsConfigurationError(
                f"{source}: grants for {principal_id!r} must be a list of operation names"
            )
        unknown = sorted(set(operations) - OPERATIONS)
        if unknown:
            # Fail closed and loud. An unrecognised operation name is either a
            # typo (a grant that would never match) or a stale name left behind
            # by a rename (a guard nobody notices stopped applying).
            raise GrantsConfigurationError(
                f"{source}: unknown operation(s) for {principal_id!r}: {', '.join(unknown)}"
            )
        parsed[principal_id] = frozenset(operations)
    return MappingProxyType(parsed)


def load_grants() -> MappingProxyType[str, frozenset[str]]:
    """Return the configured grant map, memoised per (path, mtime).

    An unset or absent file yields an empty map: a deployment that has not been
    told who may do what refuses everyone, which is the correct default for a
    control plane and the only one that cannot be reached by forgetting a step.
    """
    global _cache
    configured = os.environ.get(GRANTS_FILE_ENV, "").strip()
    if not configured:
        return _EMPTY

    path = Path(configured)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return _EMPTY
    except OSError as exc:
        raise GrantsConfigurationError(f"{path}: unreadable ({exc})") from exc

    if _cache is not None and _cache[0] == str(path) and _cache[1] == mtime:
        return _cache[2]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GrantsConfigurationError(f"{path}: {exc}") from exc

    grants = _parse(raw, str(path))
    _cache = (str(path), mtime, grants)
    return grants


def is_permitted(principal_id: str, operation: str) -> bool:
    """Deny by default. An unknown *operation* is an error, not a denial."""
    if operation not in OPERATIONS:
        raise UnknownOperationError(operation)
    return operation in load_grants().get(principal_id, frozenset())
