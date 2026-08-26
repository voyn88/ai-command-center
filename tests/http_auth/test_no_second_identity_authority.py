"""AICC must not become an identity authority.

`docs/AIOS_BOUNDARY.md` argues that `command_center/http_auth/` is a *consumer*
of the platform's authentication contract rather than new engine capability in
a frozen category. That argument is only worth the paragraph it is written in
if something checks it, so these assertions are the mechanical half: the day
this package grows a credential store, a token format or a second principal
registry, the argument stops being true and this file goes red.

The checks are on the source rather than on behaviour on purpose. A credential
store that is present but unused would still be the architectural change the
boundary doctrine forbids, and no request-level test would see it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "command_center" / "http_auth"

#: Libraries you reach for to *be* an authentication authority: to hash or
#: compare a secret, to mint or verify a token, or to generate one.
AUTHORITY_LIBRARIES = frozenset(
    {
        "hashlib",
        "hmac",
        "secrets",
        "crypt",
        "jwt",
        "jose",
        "joserfc",
        "authlib",
        "passlib",
        "bcrypt",
        "argon2",
        "nacl",
        "cryptography",
        "itsdangerous",
    }
)

#: Anything that would make this package a *store* of identities.
PERSISTENCE_LIBRARIES = frozenset({"psycopg", "psycopg2", "sqlite3", "sqlalchemy", "aios_db"})


def _modules() -> list[Path]:
    found = sorted(PACKAGE.glob("*.py"))
    assert found, "the package moved — this test would otherwise pass vacuously"
    return found


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_here_can_verify_a_secret_itself(module):
    """Verification stays with the platform.

    Importing a hashing or token library here would mean AICC had started
    deciding for itself whether a credential is genuine — which is the change
    that turns an AICC compromise into a platform compromise.
    """
    offending = _imported_roots(module) & AUTHORITY_LIBRARIES
    assert offending == set(), f"{module.name} imports {sorted(offending)}"


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_here_stores_identities(module):
    """A grant map read from configuration is an ACL. A table of principals
    would be a second registry for the same fleet, which the delivery rules
    prohibit outright."""
    offending = _imported_roots(module) & PERSISTENCE_LIBRARIES
    assert offending == set(), f"{module.name} imports {sorted(offending)}"


def test_the_platform_is_reached_only_through_whoami():
    """One call, one contract. A second endpoint would be a second dependency
    on the authority's internals rather than on its published surface."""
    from command_center.http_auth import identity

    assert identity.WHOAMI_PATH == "/api/v1/whoami"

    tree = ast.parse((PACKAGE / "identity.py").read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
    }
    platform_paths = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "/api/" in node.value
        and node.value not in docstrings
    }
    assert platform_paths == {identity.WHOAMI_PATH}


def test_the_package_does_not_import_aios_core():
    """Restated locally from `tests/architecture/test_aios_boundary_fitness.py`
    so a failure here names the reason rather than a global inventory diff."""
    for module in _modules():
        roots = _imported_roots(module)
        assert "aios" not in roots, module.name
