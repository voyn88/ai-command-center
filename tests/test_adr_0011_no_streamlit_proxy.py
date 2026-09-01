"""ADR-0011 recorded a decision, not just a document: no authenticating proxy
is planned in front of the Streamlit console (VOYN-W0-AICC-CONSOLE-NO-AUTH and
its remediation chain). Four prior attempts to design exactly that proxy were
independently rejected by review, each for a distinct credential- or
session-handling hazard; the previous attempt in this chain shipped only the
ADR text with no enforcement, so a later edit could silently reintroduce the
promise the ADR retired. This module is that enforcement: it fails if the
retired promise reappears anywhere it previously lived, and fails if the ADR
itself is missing or has been stripped of its actual decision.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR = ROOT / "docs" / "adr" / "0011-streamlit-console-no-remote-reachability.md"

# The retired claim, in both languages it was made in across this repository's
# history: widen the bind address *and put an authenticating proxy in front of
# it*, as a condition of doing so safely. ADR-0011 exists specifically because
# that proxy was never safely implementable for Streamlit's own WebSocket
# protocol. This matches the specific instructive phrasing ("must/should be
# used only behind such a proxy") rather than every mention of the word
# "proxy" — ADR-0011 and ARCHITECTURE.md legitimately *discuss* the rejected
# proxy design, and discussing it is not promising it.
_RETIRED_PROMISE = re.compile(
    r"(front of a reviewed authenticat\w*\s+proxy"
    r"|использоваться\s+только\s+за\s+\S*\s*аутентифицирующ\w*\s+прокси)",
    re.IGNORECASE,
)

# Files that made the retired promise before this task and must not make it
# again. Not every file that merely mentions "proxy" — files that promised an
# *authenticating* one specifically for this console.
_MUST_NOT_PROMISE = (
    ROOT / "docker-compose.aml.yml",
    ROOT / "docs" / "aml" / "ACCEPTANCE_PACKAGE.md",
)


def test_adr_0011_exists_and_records_the_decision() -> None:
    assert ADR.is_file(), (
        "docs/adr/0011-streamlit-console-no-remote-reachability.md is missing; "
        "the decision it records (no authenticating proxy for the Streamlit "
        "console) must stay written down, not implicit"
    )
    text = ADR.read_text(encoding="utf-8")
    assert "no reverse proxy" in text, (
        "ADR-0011 must state the decision plainly: no reverse proxy is built "
        "for the Streamlit console"
    )
    assert "command_center.webapi" in text, (
        "ADR-0011 must name the actual remote-access path (command_center.webapi "
        "/ the web client), not just what is rejected"
    )


def test_no_document_promises_an_authenticating_proxy_for_streamlit() -> None:
    for path in _MUST_NOT_PROMISE:
        assert path.is_file(), f"expected file missing: {path}"
        text = path.read_text(encoding="utf-8")
        match = _RETIRED_PROMISE.search(text)
        assert match is None, (
            f"{path} promises an authenticating proxy in front of the Streamlit "
            f"console ({match.group(0)!r}); ADR-0011 retired that promise because "
            "four independent designs for it were rejected by review — see "
            "docs/adr/0011-streamlit-console-no-remote-reachability.md"
        )
