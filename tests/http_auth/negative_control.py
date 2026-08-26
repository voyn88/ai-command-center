#!/usr/bin/env python3
"""Negative control for the HTTP authentication boundary (VOYN-W0-AICC-AUTH-HTTP-01).

A passing test suite proves that the code passes its tests. It does not prove
that the tests would notice if the protection were removed — and for a security
control that is the only question worth asking. So this script removes each
control in turn, on a throwaway copy of the tree, and requires the suite to go
red. A mutant that survives names a control nothing is actually checking.

Each mutant is an exact textual substitution. If the text it expects is not
found, that is an error and not a skip: a mutation that silently fails to apply
would produce a "killed" verdict from an unmutated tree, which is precisely the
false negative this script exists to avoid.

Usage::

    python -m tests.http_auth.negative_control            # all mutants
    python -m tests.http_auth.negative_control fail_open  # one, by name

It lives under ``tests/`` rather than ``scripts/`` because it is test tooling
(the same place `tests.architecture.aios_boundary` lives), and because a module
named for authentication under ``scripts/`` reads to the AIOS boundary scanner
as new authz capability in the product — which it is not.

Exit status is 0 only when every mutant was killed *and* the unmutated control
copy passed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE = ["tests/http_auth"]

ROUTING = "command_center/http_auth/routing.py"
IDENTITY = "command_center/http_auth/identity.py"
AUTHZ = "command_center/http_auth/authz.py"
DISPATCH_API = "command_center/dispatch/api.py"
API_APP = "command_center/api/app.py"
WEBAPI_APP = "command_center/webapi/app.py"


@dataclass(frozen=True)
class Mutant:
    """One removed control, and the property it removes."""

    name: str
    control: str
    edits: tuple[tuple[str, str, str], ...] = field(default=())


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        name="fail_open",
        control="fail closed when the identity authority is unreachable",
        edits=(
            (
                ROUTING,
                'raise HTTPException(status_code=503, detail="identity authority unavailable") from None',
                'return Principal(principal_id="anonymous", tenant_id="unknown")',
            ),
        ),
    ),
    Mutant(
        name="any_status_is_permission",
        control="only a 200 from whoami is an answer",
        edits=(
            (
                IDENTITY,
                '        raise PlatformUnavailable(f"whoami returned {status}")',
                '        return Principal(principal_id="anonymous", tenant_id="unknown")',
            ),
        ),
    ),
    Mutant(
        name="unparseable_answer_accepted",
        control="an answer AICC cannot parse is not an identity",
        edits=(
            (
                IDENTITY,
                '        raise PlatformUnavailable(f"unparseable whoami response: {exc}") from exc',
                '        return Principal(principal_id="anonymous", tenant_id="unknown")',
            ),
        ),
    ),
    Mutant(
        name="lenient_bearer_parsing",
        control="a header AICC cannot read unambiguously is not a credential",
        edits=(
            (
                IDENTITY,
                """    header = authorization_header or ""
    scheme, separator, token = header.partition(" ")
    if scheme.lower() != "bearer" or separator != " ":
        return None
    if not token or token.strip() != token:
        return None
    return token""",
                """    header = authorization_header or ""
    return header.split(" ")[-1].strip() or None""",
            ),
        ),
    ),
    Mutant(
        name="skip_authorization",
        control="AICC-local authorization (a 200 from whoami is not permission)",
        edits=(
            (
                ROUTING,
                "    if not authz.is_permitted(principal.principal_id, operation):",
                "    if False and not authz.is_permitted(principal.principal_id, operation):",
            ),
        ),
    ),
    Mutant(
        name="cache_verdicts",
        control="no cache on the write path (revocation takes effect immediately)",
        edits=(
            (
                ROUTING,
                """def authenticate(request: Request) -> Principal:""",
                """_VERDICT_CACHE: dict = {}


def authenticate(request: Request) -> Principal:""",
            ),
            (
                ROUTING,
                """    try:
        principal = whoami(token)""",
                """    if token in _VERDICT_CACHE:
        return _VERDICT_CACHE[token]
    try:
        principal = whoami(token)""",
            ),
            (
                ROUTING,
                """        raise HTTPException(status_code=401, detail="unauthenticated")
    return principal""",
                """        raise HTTPException(status_code=401, detail="unauthenticated")
    _VERDICT_CACHE[token] = principal
    return principal""",
            ),
        ),
    ),
    Mutant(
        name="grants_default_allow",
        control="deny by default for a principal with no grants",
        edits=(
            (
                AUTHZ,
                "    return operation in load_grants().get(principal_id, frozenset())",
                """    grants = load_grants()
    if principal_id not in grants:
        return True
    return operation in grants[principal_id]""",
            ),
        ),
    ),
    Mutant(
        name="unknown_operation_denies_quietly",
        control="a misspelled operation is a programming error, not a denial",
        edits=(
            (
                AUTHZ,
                "        raise UnknownOperationError(operation)",
                "        return False",
            ),
        ),
    ),
    Mutant(
        name="grant_file_typo_accepted",
        control="a grant file naming an unknown operation stops the deploy",
        edits=(
            (
                AUTHZ,
                """        unknown = sorted(set(operations) - OPERATIONS)""",
                """        unknown = []""",
            ),
        ),
    ),
    Mutant(
        name="unrouted_route_is_allowed",
        control="a mutating route with no routing entry is denied, not waved through",
        edits=(
            (
                ROUTING,
                """        raise HTTPException(status_code=403, detail="forbidden")

    principal = authenticate(request)""",
                """        return None

    principal = authenticate(request)""",
            ),
        ),
    ),
    Mutant(
        name="vacuous_route_walker",
        control="the boot check recurses into included routers",
        edits=(
            (
                ROUTING,
                """        context = getattr(route, "include_context", None)
        carried = tuple(
            getattr(d, "dependency", None) for d in getattr(context, "dependencies", [])
        )
        nested = getattr(route, "original_router", None)
        found.extend(
            _leaf_routes(nested if nested is not None else route, inherited + carried)
        )""",
                """        continue""",
            ),
            (
                ROUTING,
                """    if checked == 0:
        problems.append("walked 0 mutating routes — the route walker is broken")""",
                """    if False:
        problems.append("walked 0 mutating routes — the route walker is broken")""",
            ),
        ),
    ),
    Mutant(
        name="no_boot_check",
        control="the boot check's call sites in both app factories",
        edits=(
            (API_APP, "    validate_routing(app)", "    pass  # validate_routing(app)"),
            (WEBAPI_APP, "    validate_routing(app)", "    pass  # validate_routing(app)"),
        ),
    ),
    Mutant(
        name="dependency_not_mounted",
        control="the authentication dependency on the dispatch write routes",
        edits=(
            (
                WEBAPI_APP,
                "    app.include_router(create_dispatch_router(), dependencies=[Depends(enforce)])",
                "    app.include_router(create_dispatch_router())",
            ),
            (API_APP, "    validate_routing(app)", "    pass  # validate_routing(app)"),
            (WEBAPI_APP, "    validate_routing(app)", "    pass  # validate_routing(app)"),
        ),
    ),
    Mutant(
        name="extra_ignore",
        control='extra="forbid" — a forged actor is refused, not silently ignored',
        edits=(
            (
                DISPATCH_API,
                """    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False""",
                """    model_config = ConfigDict(extra="ignore")

    confirmed: bool = False""",
            ),
            (
                DISPATCH_API,
                """    model_config = ConfigDict(extra="forbid")

    changes: dict = Field(default_factory=dict)""",
                """    model_config = ConfigDict(extra="ignore")

    changes: dict = Field(default_factory=dict)""",
            ),
        ),
    ),
    Mutant(
        name="actor_from_body",
        control="the actor is derived from the caller, never declared by them",
        edits=(
            (
                DISPATCH_API,
                """    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False""",
                """    model_config = ConfigDict(extra="ignore")

    confirmed: bool = False
    actor: str | None = None""",
            ),
            (
                DISPATCH_API,
                """        body = payload or AssignRequest()
        return _service.assign(_root(), principal, confirmed=body.confirmed)""",
                """        body = payload or AssignRequest()
        if body.actor:
            principal = Principal(
                principal_id=body.actor, tenant_id=principal.tenant_id
            )
        return _service.assign(_root(), principal, confirmed=body.confirmed)""",
            ),
        ),
    ),
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def _materialise(destination: Path) -> None:
    for relative in _tracked_files():
        source = REPO_ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _apply(mutant: Mutant, tree: Path) -> None:
    for relative, old, new in mutant.edits:
        path = tree / relative
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise SystemExit(
                f"mutant {mutant.name!r}: the text it rewrites is not in {relative}.\n"
                "A mutation that does not apply would report a kill from an "
                "unmutated tree. Update the mutant, do not skip it."
            )
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _run_suite(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=tree,
        capture_output=True,
        text=True,
    )


def _killers(output: str) -> list[str]:
    return sorted(
        {
            line.split(" ")[1].split("[")[0]
            for line in output.splitlines()
            if line.startswith("FAILED ") or line.startswith("ERROR ")
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("only", nargs="*", help="mutant names to run (default: all)")
    args = parser.parse_args()

    selected = [m for m in MUTANTS if not args.only or m.name in args.only]
    if args.only and len(selected) != len(args.only):
        raise SystemExit(f"unknown mutant(s): {set(args.only) - {m.name for m in selected}}")

    survivors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="auth-http-negative-control-") as workspace:
        root = Path(workspace)

        control = root / "unmutated"
        _materialise(control)
        result = _run_suite(control)
        print("=" * 72)
        print("CONTROL (unmutated copy) — must PASS")
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "")
        if result.returncode != 0:
            print(result.stdout[-4000:])
            print("the unmutated copy fails: every 'kill' below would be meaningless")
            return 2

        for mutant in selected:
            tree = root / mutant.name
            _materialise(tree)
            _apply(mutant, tree)
            result = _run_suite(tree)
            killed = result.returncode != 0
            print("=" * 72)
            print(f"MUTANT {mutant.name}")
            print(f"  removes: {mutant.control}")
            if killed:
                print(f"  KILLED by: {', '.join(_killers(result.stdout)) or 'collection error'}")
            else:
                print("  *** SURVIVED — no test observes this control ***")
                survivors.append(mutant.name)

    print("=" * 72)
    print(f"{len(selected) - len(survivors)}/{len(selected)} mutants killed, {len(survivors)} survived")
    if survivors:
        print("survivors: " + ", ".join(survivors))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
