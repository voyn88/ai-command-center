"""Authentication and authorization for the AI Command Center HTTP surfaces.

Both FastAPI apps — ``command_center.api.app`` (27 mutating routes) and
``command_center.webapi.app`` (2) — mount one dependency, :func:`routing.enforce`,
and call :func:`routing.validate_routing` before returning. The division of
labour is deliberate and is the whole design::

    the platform says WHO you are   -- identity.py, GET /api/v1/whoami
    AICC says WHAT you may do here  -- authz.py, a closed deny-by-default map
    the table says WHICH routes ask -- routing.py, checked against the router
                                       tree at startup

Read the module docstrings in that order: ``identity`` for why authentication
is delegated and never cached, ``authz`` for why a 200 from ``whoami`` is not
permission, ``routing`` for why coverage is a table with a boot check rather
than a decorator repeated 29 times.
"""

from __future__ import annotations

from command_center.http_auth.authz import UnknownOperationError, is_permitted
from command_center.http_auth.identity import Principal, PlatformUnavailable
from command_center.http_auth.routing import (
    RouteInventoryError,
    enforce,
    validate_routing,
)

__all__ = [
    "Principal",
    "PlatformUnavailable",
    "RouteInventoryError",
    "UnknownOperationError",
    "enforce",
    "is_permitted",
    "validate_routing",
]
