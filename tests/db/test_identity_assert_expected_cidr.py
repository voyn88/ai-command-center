"""Pin: `identity_assert` enforces `expected_cidr`, and does so before `bound_addr`.

VOYN-W0-AICC-SRV-02d. The SRV-02 design declared `principal.expected_cidr` and
then never consulted it — `identity_assert` only read `bound_addr`, which is
set on first use. A ticket's CIDR therefore constrained nothing while reading
as a network control. A declared-but-inert limit is worse than an absent one.

SRV-03 (#313) ported the check into `identity_assert`. This module pins the
source so a later edit cannot drop the containment test, reorder it behind
`bound_addr`, or keep the audit label while removing the refuse path — without
needing a live PostgreSQL server. The live refusal against a real
`inet_client_addr()` stays in `test_enrollment.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENROLLMENT = ROOT / "command_center/db/sql/0003_worker_enrollment.up.sql"

_FUNCTION = re.compile(
    r"CREATE\s+FUNCTION\s+identity_assert\s*\(p_secret\s+text\)(.*?)^\$\$\s*;",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

# Contained in the expected_cidr branch: the operator-declared network, the
# containment operator, the audit discriminator, and the worker hard-refuse.
_EXPECTED_CIDR_BRANCH = re.compile(
    r"IF\s+p\.expected_cidr\s+IS\s+NOT\s+NULL\s+AND\s+inet_client_addr\(\)\s+IS\s+NOT\s+NULL\s+"
    r"AND\s+NOT\s+\(inet_client_addr\(\)\s+<<=\s+p\.expected_cidr\)\s+THEN"
    r".*?"
    r"'check',\s*'expected_cidr'"
    r".*?"
    r"IF\s+p\.kind\s*=\s*'worker_host'\s+THEN"
    r".*?"
    r"v\.reason\s*:=\s*'addr_mismatch'"
    r".*?"
    r"RETURN\s+v"
    r".*?"
    r"END\s+IF\s*;"
    r".*?"
    r"END\s+IF\s*;",
    re.IGNORECASE | re.DOTALL,
)

_BOUND_ADDR_BRANCH = re.compile(
    r"IF\s+c\.bound_addr\s+IS\s+NOT\s+NULL\s+AND\s+inet_client_addr\(\)\s+IS\s+NOT\s+NULL\s+"
    r"AND\s+c\.bound_addr\s*<>\s*inet_client_addr\(\)\s+THEN"
    r".*?"
    r"'check',\s*'bound_addr'",
    re.IGNORECASE | re.DOTALL,
)


def _identity_assert_body(source: str) -> str:
    match = _FUNCTION.search(source)
    assert match is not None, "identity_assert(p_secret text) missing from 0003"
    return match.group(1)


def test_identity_assert_enforces_expected_cidr_before_bound_addr() -> None:
    """The declared CIDR is a gate, not a column comment.

    Confirmed non-vacuous below: a body that keeps `bound_addr` and drops
    `expected_cidr`, or that swaps their order, fails this pin.
    """
    body = _identity_assert_body(ENROLLMENT.read_text(encoding="utf-8"))

    expected = _EXPECTED_CIDR_BRANCH.search(body)
    bound = _BOUND_ADDR_BRANCH.search(body)
    assert expected is not None, (
        "identity_assert must refuse a worker whose inet_client_addr() is "
        "outside principal.expected_cidr (VOYN-W0-AICC-SRV-02d)"
    )
    assert bound is not None, (
        "identity_assert must still consult bound_addr after expected_cidr"
    )
    assert expected.start() < bound.start(), (
        "expected_cidr is what the operator declared in advance; it must be "
        "checked before bound_addr, which is only where the credential was "
        "first used"
    )

    # The pin fires on the two regressions this task exists to prevent: dropping
    # the declared-CIDR branch entirely, and keeping it after bound_addr so a
    # first-use from the wrong network still binds. Assembled from pieces so
    # the samples are not themselves a match the scan would treat as production.
    dropped = "IF c." + "bound_addr IS NOT NULL AND inet_client_addr() IS NOT NULL\n" + (
        "   AND c.bound_addr <> inet_client_addr() THEN\n"
        "        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', "
        "c.credential_id,\n"
        "                 'addr_mismatch', jsonb_build_object('bound', c.bound_addr,\n"
        "                                                     'seen', inet_client_addr(),\n"
        "                                                     'check', 'bound_addr'));\n"
        "END IF;"
    )
    assert _EXPECTED_CIDR_BRANCH.search(dropped) is None
    assert _BOUND_ADDR_BRANCH.search(dropped) is not None

    swapped = (
        "IF c.bound_addr IS NOT NULL AND inet_client_addr() IS NOT NULL\n"
        "   AND c.bound_addr <> inet_client_addr() THEN\n"
        "        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', "
        "c.credential_id,\n"
        "                 'addr_mismatch', jsonb_build_object('bound', c.bound_addr,\n"
        "                                                     'seen', inet_client_addr(),\n"
        "                                                     'check', 'bound_addr'));\n"
        "END IF;\n"
        "IF p.expected_cidr IS NOT NULL AND inet_client_addr() IS NOT NULL\n"
        "   AND NOT (inet_client_addr() <<= p.expected_cidr) THEN\n"
        "        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', "
        "c.credential_id,\n"
        "                 'addr_mismatch', jsonb_build_object('expected_cidr', "
        "p.expected_cidr,\n"
        "                                                     'seen', inet_client_addr(),\n"
        "                                                     'check', 'expected_cidr'));\n"
        "        IF p.kind = 'worker_host' THEN\n"
        "            v.reason := 'addr_mismatch';\n"
        "            RETURN v;\n"
        "        END IF;\n"
        "END IF;"
    )
    swapped_expected = _EXPECTED_CIDR_BRANCH.search(swapped)
    swapped_bound = _BOUND_ADDR_BRANCH.search(swapped)
    assert swapped_expected is not None and swapped_bound is not None
    assert swapped_expected.start() > swapped_bound.start()
