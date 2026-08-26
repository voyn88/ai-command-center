"""Who is calling: the authentication half of the HTTP boundary.

Mechanism. The caller presents its own platform bearer credential; AICC
forwards it to ``GET /api/v1/whoami`` and takes the reflected ``principal_id``
as the caller's identity. AICC stores no credential, hashes nothing, and holds
no signing key.

Why not a locally verifiable signed token (JWT/PASETO). It was the preferred
option on latency grounds until the platform was read: there is no signed-token
format upstream at all, and the credential is an opaque ``<id>.<secret>``
verified by PBKDF2 against a live row. Introducing a signed token would mean
building a *second* credential format inside the identity authority — and,
decisively, the acceptance criterion is that a revoked principal is refused
**immediately**. A self-contained token is by construction valid until it
expires; making revocation immediate requires a revocation lookup per request,
which is exactly the round-trip the signed token existed to avoid. The option
collapses into this one.

Why not verify in-process. Reusing the platform's own verifier would require
AICC to hold the platform's credential-store database role, i.e. read access to
every credential hash in the fleet — turning an AICC compromise into a platform
compromise. AICC is the *less* trusted service here and must not hold the more
sensitive credential. The repository's own SDK-boundary fitness test
(``tests/architecture/test_aios_boundary_fitness.py``) forbids the import
independently.

Why no cache on this path. The platform has none: revocation currently takes
effect on the next request. Any cache AICC adds is staleness AICC *introduces*
into a contract that does not have it, and the window it opens is exactly the
window in which a revoked operator can still mutate. Measured cost of paying
the round trip instead: 47–76 ms median (PBKDF2, 600k iterations, n=20, two
runs on an Apple Silicon core), a ceiling of roughly 105–169 authentications
per second platform-wide. That is affordable on a write path whose traffic is
operator actions. It does **not** rule out authenticating the read paths;
reads are out of scope here by acceptance criteria, not because cost forbids
them (``VOYN-W0-AICC-AUTH-HTTP-02``).

Why the transport is ``urllib`` and not ``httpx``. ``httpx`` is a *development*
dependency here (Starlette's TestClient needs it); it is absent from
``requirements-web.txt`` and therefore from the served environment. Reaching
for it would add a production dependency and force every pinned lock to be
recompiled. The standard library covers a single authenticated GET with a
timeout, so the authentication path adds no new supply-chain surface.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Environment variable naming the platform's base URL, e.g.
#: ``https://platform.internal``. Absent means "this deployment cannot
#: authenticate anyone", which is a 503, never an implicit allow.
PLATFORM_URL_ENV = "AICC_PLATFORM_URL"

WHOAMI_PATH = "/api/v1/whoami"

#: Connect + read budget for one verification. Short enough that a hung
#: authority degrades to a fast, loud 503 rather than a queue of stuck workers.
TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    Constructed only by :func:`_whoami` from a platform response. Handlers take
    a ``Principal`` rather than an actor string precisely so that an identity
    cannot be spelled out by a client: there is no constructor a request body
    can reach.
    """

    principal_id: str
    tenant_id: str
    capabilities: tuple[str, ...] = ()


class PlatformUnavailable(RuntimeError):
    """The identity authority could not be reached, or did not answer usably.

    Deliberately distinct from "the caller is not authenticated". "We do not
    know who you are" and "you are not who you claim" want different HTTP
    statuses (503 vs 401), different client retry behaviour, and different
    alerts.
    """


def platform_base_url() -> str | None:
    value = os.environ.get(PLATFORM_URL_ENV, "").strip()
    return value.rstrip("/") or None


def _http_get_json(url: str, token: str, timeout: float) -> tuple[int, bytes]:
    """One authenticated GET. The seam the test suite replaces.

    Returns ``(status, body)``. Raises :class:`PlatformUnavailable` when there
    is no usable answer at all — a socket error, a timeout, a DNS failure.
    ``HTTPError`` is *not* an outage: it carries a real status line, so it is
    returned as a status for the caller to interpret.
    """
    request = urllib.request.Request(  # noqa: S310 - scheme is operator-configured
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PlatformUnavailable(f"whoami transport failure: {exc}") from exc


def _whoami(token: str) -> Principal | None:
    """Ask the platform who this credential belongs to.

    ``None`` means the platform gave a definite "not authenticated". Anything
    that is not a definite answer raises :class:`PlatformUnavailable`; the two
    must never be collapsed, because collapsing "unknown" into "denied" is how
    an outage becomes indistinguishable from an attack, and collapsing it the
    other way is how an outage becomes an open door.
    """
    base = platform_base_url()
    if base is None:
        raise PlatformUnavailable(f"{PLATFORM_URL_ENV} is not configured")

    status, body = _http_get_json(base + WHOAMI_PATH, token, TIMEOUT_SECONDS)

    if status == 401:
        return None
    if status != 200:
        # 429 from the platform's verification gate, 503 from an outage, or
        # anything unexpected. Not a denial — we simply do not know.
        raise PlatformUnavailable(f"whoami returned {status}")

    try:
        data = json.loads(body)["data"]
        return Principal(
            principal_id=str(data["principal_id"]),
            tenant_id=str(data["tenant_id"]),
            capabilities=tuple(str(c) for c in data.get("capabilities", ())),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise PlatformUnavailable(f"unparseable whoami response: {exc}") from exc


def bearer_token(authorization_header: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header, or ``None``.

    Strict on purpose: a header AICC cannot parse unambiguously is not a
    credential. Surrounding whitespace inside the token is rejected rather than
    stripped, so AICC and the platform never disagree about which bytes were
    presented.
    """
    header = authorization_header or ""
    scheme, separator, token = header.partition(" ")
    if scheme.lower() != "bearer" or separator != " ":
        return None
    if not token or token.strip() != token:
        return None
    return token
