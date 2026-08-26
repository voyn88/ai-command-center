"""Every mutating route, the operation it needs, and the boot check that proves it.

The endpoint nobody remembered is the one that stays open, and it is the one
failure nobody notices after the fact. So coverage here is a *table*, not a
decorator repeated 29 times, and the table is checked against the live router
tree when the process starts.

Three properties follow from that shape, none of which a per-route decorator
gives you:

* **A new mutating route cannot ship unrouted.** :func:`validate_routing`
  raises, and it is called from both app factories, so the process refuses to
  start. A CI test catches a forgotten guard only if CI runs; a boot check also
  catches a route added by a hotfix, a plugin, or a merge that skipped the
  suite — and it fails in the environment that matters rather than in a report.
* **A renamed path cannot silently lose its guard**, because the entry stops
  matching and the route becomes unrouted.
* **The inventory is reviewable in one place**, next to the operation names an
  operator has to write into the grant file.

Reads are deliberately out of scope (``VOYN-W0-AICC-AUTH-HTTP-02``): the
dependency is mounted on every included route, and returns ``None`` without
touching the network for anything that is not a mutating verb.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, FastAPI, HTTPException, Request

from command_center.http_auth import authz
from command_center.http_auth.identity import (
    Principal,
    PlatformUnavailable,
    bearer_token,
)
from command_center.http_auth.identity import _whoami as whoami

__all__ = [
    "MUTATING_VERBS",
    "ROUTE_OPERATIONS",
    "CLIENT_IDENTITY_CARVE_OUTS",
    "CarveOut",
    "RouteInventoryError",
    "enforce",
    "require_principal",
    "authenticate",
    "operation_for",
    "mutating_routes",
    "validate_routing",
]

MUTATING_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: ``(method, path template) -> operation``. Exhaustive over both HTTP
#: surfaces: ``command_center/api/app.py`` (27) and
#: ``command_center/webapi/app.py`` (3: two dispatch writes and the queue
#: audit enqueue, VOYN-W0-APP-CONTROL-S1/S4).
ROUTE_OPERATIONS: dict[tuple[str, str], str] = {
    # -- command_center/api/wave1_routes.py -------------------------------
    ("POST", "/api/v1/proposals"): "proposals:create",
    ("POST", "/api/v1/proposals/{proposal_id}/promote"): "proposals:promote",
    ("POST", "/api/v1/tasks/reorder"): "tasks:reorder",
    ("POST", "/api/v1/advisor/run"): "advisor:run",
    ("POST", "/api/v1/owner-items"): "owner-items:create",
    ("POST", "/api/v1/owner-items/{item_id}/complete"): "owner-items:complete",
    ("POST", "/api/v1/digest/build"): "digest:build",
    ("POST", "/api/v1/digest"): "digest:create",
    # -- command_center/api/conflict_routes.py ----------------------------
    ("POST", "/api/v1/conflicts"): "conflicts:create",
    ("POST", "/api/v1/conflicts/{conflict_id}/assign"): "conflicts:assign",
    ("POST", "/api/v1/conflicts/{conflict_id}/mitigation"): "conflicts:mitigate",
    ("POST", "/api/v1/conflicts/{conflict_id}/resolve"): "conflicts:resolve",
    # -- command_center/api/audit_routes.py -------------------------------
    ("POST", "/api/v1/audit/run"): "audit:run",
    ("POST", "/api/v1/audit/findings/{finding_id}/status"): "audit:finding:status",
    ("POST", "/api/v1/audit/findings/{finding_id}/promote"): "audit:finding:promote",
    # -- command_center/api/council_routes.py -----------------------------
    ("POST", "/api/v1/council/motions"): "council:motion:create",
    ("POST", "/api/v1/council/motions/{motion_id}/vote"): "council:motion:vote",
    ("POST", "/api/v1/council/motions/{motion_id}/close"): "council:motion:close",
    # -- command_center/api/marketplace_routes.py -------------------------
    ("POST", "/api/v1/marketplace/items"): "marketplace:item:create",
    ("POST", "/api/v1/marketplace/items/{item_id}/install"): "marketplace:item:install",
    # -- command_center/api/model_registry_routes.py ----------------------
    ("POST", "/api/v1/models"): "models:register",
    ("POST", "/api/v1/models/{model_id}/download"): "models:download",
    ("POST", "/api/v1/models/{model_id}/assign"): "models:assign",
    # -- command_center/api/networking_routes.py --------------------------
    ("POST", "/api/v1/networking/contacts"): "networking:contact:create",
    ("POST", "/api/v1/networking/messages"): "networking:message:send",
    ("POST", "/api/v1/networking/feedback"): "networking:feedback:submit",
    ("POST", "/api/v1/networking/invite"): "networking:invite",
    # -- command_center/dispatch/api.py -----------------------------------
    ("POST", "/api/v1/dispatch/assign"): "dispatch:assign",
    ("PUT", "/api/v1/dispatch/policy"): "dispatch:policy:update",
    # -- command_center/webapi/queue_routes.py ----------------------------
    ("POST", "/api/v1/queue/audit"): "queue:audit:enqueue",
}


@dataclass(frozen=True, slots=True)
class CarveOut:
    """A route that is authenticated but still reads an identity from its body.

    Authentication and authorization apply to every route in
    :data:`ROUTE_OPERATIONS` without exception. A carve-out records something
    narrower and still real: the handler *also* takes an identity-shaped field
    from the client, so the recorded actor may not be the authenticated caller.

    Removing such a field is a per-endpoint product decision (is the assignee
    always the caller? almost certainly not), not a mechanical rename, so each
    one is signed here with a reason and a task rather than deleted blind or —
    the failure this registry exists to prevent — passed over in silence.
    """

    fields: tuple[str, ...]
    reason: str
    task: str


#: Field names that name a person or service. Any *request body* field with one
#: of these names, on a mutating route, must appear in a signed carve-out — see
#: ``tests/http_auth/test_routing_coverage.py``. This is what stops a future
#: schema quietly reintroducing a client-declared actor.
CLIENT_IDENTITY_FIELD_NAMES = frozenset({"actor", "owner", "voter_id"})

CLIENT_IDENTITY_CARVE_OUTS: dict[tuple[str, str], CarveOut] = {
    ("POST", "/api/v1/council/motions/{motion_id}/vote"): CarveOut(
        fields=("voter_id",),
        reason=(
            "Highest severity of the set. `role` is roster-resolved and documented "
            "as untrusted-from-client, but `voter_id` is not, so the roster lookup "
            "only decorates an asserted identity, and `/close` converts the votes "
            "into a DecisionRecord. Authentication now bounds who may reach the "
            "route at all; binding voter_id to the caller additionally requires a "
            "principal->board-seat mapping, which does not exist yet and is a "
            "governance decision rather than a refactor."
        ),
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/conflicts"): CarveOut(
        fields=("owner",),
        reason=(
            "`owner` is a third-party reference — the person who will own the "
            "conflict — not a claim about the caller. Deriving it from the "
            "principal would be wrong, so it stays until intake grows a separate "
            "`reported_by` recorded from the caller."
        ),
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/conflicts/{conflict_id}/assign"): CarveOut(
        fields=("owner",),
        reason=(
            "Assignment names the assignee, who is routinely not the caller. The "
            "attributable fact this route is missing is who *performed* the "
            "assignment; that needs a schema field before the caller can fill it."
        ),
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/marketplace/items/{item_id}/install"): CarveOut(
        fields=("actor",),
        reason=(
            "`actor` is required here and is written into the install record. It "
            "is a genuine caller claim and should become the principal, but the "
            "field is non-optional, so deleting it is a breaking wire change that "
            "belongs with the other schema removals rather than in the mechanism."
        ),
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/models"): CarveOut(
        fields=("actor",),
        reason="Governance-log attribution; should become the principal. See 01b.",
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/models/{model_id}/download"): CarveOut(
        fields=("actor",),
        reason="Governance-log attribution; should become the principal. See 01b.",
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
    ("POST", "/api/v1/models/{model_id}/assign"): CarveOut(
        fields=("actor",),
        reason="Governance-log attribution; should become the principal. See 01b.",
        task="VOYN-W0-AICC-AUTH-HTTP-01b",
    ),
}


class RouteInventoryError(RuntimeError):
    """A mutating route is not covered by the routing table, or the walk is broken."""


def operation_for(method: str, path: str) -> str | None:
    return ROUTE_OPERATIONS.get((method.upper(), path))


def authenticate(request: Request) -> Principal:
    """Resolve the caller through the platform, or refuse. Never returns anonymous."""
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise HTTPException(status_code=401, detail="unauthenticated")

    try:
        principal = whoami(token)
    except PlatformUnavailable:
        # Fail closed, and not because it is tidier. Fail-open costs: during any
        # outage of the identity authority every mutating endpoint accepts
        # anonymous callers — and the attacker picks the moment, because
        # *causing* the outage is cheap (the platform's verification gate is a
        # small non-blocking semaphore). Fail-open makes the availability of
        # authentication an attacker-controlled variable. Fail-closed costs: an
        # operator cannot change policy during an outage. Reads still serve, and
        # workers are unaffected because they authenticate to PostgreSQL
        # directly, not through this path. A refused write is recoverable; an
        # accepted anonymous write is not.
        #
        # 503, not 401, on purpose: "we do not know who you are" and "you are
        # not authenticated" want different alerts and different client retries.
        raise HTTPException(
            status_code=503, detail="identity authority unavailable"
        ) from None

    if principal is None:
        # A flat refusal. The platform's own reason vocabulary is never
        # forwarded: relaying which of unknown/wrong-secret/expired/revoked
        # applies would make AICC a free oracle for an attacker probing tokens.
        raise HTTPException(status_code=401, detail="unauthenticated")
    return principal


def enforce(request: Request) -> Principal | None:
    """The single dependency mounted on every included route.

    Mutating routes are authenticated against the platform and then authorized
    locally. Everything else — reads, ``/healthz``, the SPA — is returned
    untouched, so no read pays for the round trip.

    The operation comes from the routing table via the *matched route*, never
    from the request path, so a caller cannot influence which grant is checked.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None) or ()
    verb = next(iter(sorted(set(methods) & MUTATING_VERBS)), None)
    if path is None or verb is None:
        return None

    operation = operation_for(verb, path)
    if operation is None:
        # Unreachable in a started process: validate_routing() refuses to build
        # an app whose mutating routes are not all in the table. Kept as a
        # belt-and-braces denial rather than an implicit allow, for the case
        # where a route is added to a live router after startup.
        raise HTTPException(status_code=403, detail="forbidden")

    principal = authenticate(request)
    if not authz.is_permitted(principal.principal_id, operation):
        raise HTTPException(status_code=403, detail="forbidden")
    return principal


def require_principal(principal: Principal | None = Depends(enforce)) -> Principal:
    """The caller, for a handler that needs to *record* who acted.

    FastAPI caches a parameterless dependency per request, so declaring this in
    a handler reuses the very verification the mounted :func:`enforce` already
    performed — one round trip, not two. ``None`` cannot occur on a mutating
    route (``enforce`` either returns a principal or raises); it is refused
    rather than defaulted, because the one thing a handler must never do is
    invent an actor when authentication did not produce one.
    """
    if principal is None:  # pragma: no cover - unreachable on a mutating route
        raise HTTPException(
            status_code=500, detail="unauthenticated handler invocation"
        )
    return principal


def _dependency_calls(route: object) -> list:
    """Every callable this route declares as a dependency, at any nesting."""
    calls: list = []
    dependant = getattr(route, "dependant", None)
    for dependency in getattr(dependant, "dependencies", []):
        call = getattr(dependency, "call", None)
        if call is not None:
            calls.append(call)
    for depends in getattr(route, "dependencies", []):
        call = getattr(depends, "dependency", None)
        if call is not None:
            calls.append(call)
    return calls


def _leaf_routes(router: object, inherited: tuple = ()) -> list[tuple[object, tuple]]:
    """Flatten a router tree into ``(leaf route, dependencies it inherits)``.

    Two details make this non-vacuous, and both were found by running it rather
    than by reading FastAPI:

    * ``include_router`` results are wrapped in ``_IncludedRouter``, which
      exposes neither ``.methods`` nor ``.routes`` — the endpoints hang off
      ``.original_router``. A flat pass over ``app.routes`` finds *nothing*, and
      a guard that inspects nothing reports success.
    * The dependencies passed to ``include_router`` are **not** copied onto the
      leaf routes' ``dependant``; they stay on the wrapper's
      ``include_context`` and are applied at request time. Inspecting only the
      leaf therefore also finds nothing. They have to be threaded down.

    Both are FastAPI internals, so the structural check is deliberately backed
    by a behavioural one: ``tests/http_auth`` drives an unauthenticated request
    at every route in the table and requires a 401. That test cannot be fooled
    by a future FastAPI reshaping this tree.
    """
    found: list[tuple[object, tuple]] = []
    for route in getattr(router, "routes", []):
        if getattr(route, "methods", None):
            found.append((route, inherited + tuple(_dependency_calls(route))))
            continue
        context = getattr(route, "include_context", None)
        carried = tuple(
            getattr(d, "dependency", None) for d in getattr(context, "dependencies", [])
        )
        nested = getattr(route, "original_router", None)
        found.extend(
            _leaf_routes(nested if nested is not None else route, inherited + carried)
        )
    return found


def mutating_routes(app: FastAPI) -> list[tuple[str, str, object]]:
    """``(verb, path, route)`` for every mutating leaf route of ``app``."""
    rows: list[tuple[str, str, object]] = []
    for route, _ in _leaf_routes(app):
        for verb in sorted(set(route.methods) & MUTATING_VERBS):
            rows.append((verb, route.path, route))
    return rows


def _guarded_paths(app: FastAPI) -> set[tuple[str, str]]:
    """``(verb, path)`` for every mutating route that actually carries :func:`enforce`."""
    guarded: set[tuple[str, str]] = set()
    for route, dependencies in _leaf_routes(app):
        if enforce not in dependencies:
            continue
        for verb in sorted(set(route.methods) & MUTATING_VERBS):
            guarded.add((verb, route.path))
    return guarded


def validate_routing(app: FastAPI) -> None:
    """Refuse to build an app whose mutating surface is not fully covered.

    Checks, in order:

    1. every mutating route has an entry in :data:`ROUTE_OPERATIONS`;
    2. that entry names an operation in :data:`authz.OPERATIONS`;
    3. every mutating route actually carries :func:`enforce` — a table entry
       for a route the dependency never runs on is worse than no entry, because
       it reads as coverage;
    4. the walk found a non-zero number of mutating routes. A zero-route
       inventory is not a clean bill of health, it is a broken walker, and it
       is the failure mode this check was first written to catch;
    5. the grant file, if configured, parses — so a typo in an operation name
       stops the deploy instead of silently denying an operator at 3am.
    """
    unrouted: list[str] = []
    unknown_operation: list[str] = []
    unguarded: list[str] = []
    checked = 0

    guarded = _guarded_paths(app)
    for verb, path, _route in mutating_routes(app):
        checked += 1
        operation = operation_for(verb, path)
        if operation is None:
            unrouted.append(f"{verb} {path}")
        elif operation not in authz.OPERATIONS:
            unknown_operation.append(f"{verb} {path} -> {operation}")
        if (verb, path) not in guarded:
            unguarded.append(f"{verb} {path}")

    problems: list[str] = []
    if unrouted:
        problems.append("no routing entry: " + "; ".join(sorted(unrouted)))
    if unknown_operation:
        problems.append(
            "operation outside the closed inventory: "
            + "; ".join(sorted(unknown_operation))
        )
    if unguarded:
        problems.append(
            "authentication dependency not mounted: " + "; ".join(sorted(unguarded))
        )
    if checked == 0:
        problems.append("walked 0 mutating routes — the route walker is broken")
    if problems:
        raise RouteInventoryError("; ".join(problems))

    authz.load_grants()
