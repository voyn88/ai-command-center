"""Every endpoint test in this package acts as an authenticated, granted caller.

See `tests/http_auth_fixture.py` for what this replaces (only the network call
to the identity authority) and what still runs (the routing table, the
operation lookup and the local grant check). Coverage of the guard itself is
proven in `tests/http_auth/`, not here.
"""

from __future__ import annotations

import pytest

from tests.http_auth_fixture import authenticated_caller as _authenticated_caller

authenticated_caller = _authenticated_caller


@pytest.fixture(autouse=True)
def _authenticated(authenticated_caller):
    return authenticated_caller
