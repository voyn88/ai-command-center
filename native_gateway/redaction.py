"""Server-side redaction boundary — the last line before bytes leave AIOS.

Two complementary mechanisms, both fail-closed:

1. **Allowlist projection** — the mappers in `native_gateway.source` copy only
   named fields into pydantic DTOs with ``extra="forbid"``; anything the
   projection file carries beyond the allowlist never reaches a response.
2. **Prohibited-content scan** — every outbound string value is scanned for
   secret-shaped content and replaced with ``[REDACTED]`` if it matches; the
   fully serialized response body is then scanned once more, and any residual
   hit aborts the response with a safe 500 instead of leaking.

The pattern list is a strict superset of the native client's own
`SnapshotDecoder` guard ("authorization", "bearer ", "password", "ssh-rsa",
"postgres://", "private_key", "prompt"): if the server let such content
through, every client would hard-fail the whole snapshot, so redacting here is
also a liveness requirement, not only a security one.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Case-insensitive substring/regex patterns for content that must never leave
# the gateway: credentials, key material, DSNs, SSH data, absolute host paths,
# raw model inputs ("prompt") and anything header-shaped.
_PROHIBITED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"authorization",
        r"bearer\s",
        r"passwords?",
        r"passwd",
        r"private[_-]?key",
        r"-----BEGIN",
        r"ssh-rsa",
        r"ssh-ed25519",
        r"ssh://",
        r"postgres(?:ql)?://",
        r"\bdsn\b",
        r"api[_-]?key",
        r"secret",
        r"token",
        r"credential",
        r"ghp_[A-Za-z0-9]",
        r"github_pat_",
        r"\bsk-[A-Za-z0-9]{8,}",
        r"prompt",
        r"raw[_-]?log",
        # Absolute paths (POSIX system roots and Windows drives).
        r"(?:^|[\s\"'=(])/(?:Users|home|var|etc|opt|srv|root|private|tmp)/",
        r"[A-Za-z]:\\",
    )
)


def find_violation(text: str) -> str | None:
    """Return the name of the first prohibited pattern found, else None."""
    for pattern in _PROHIBITED:
        if pattern.search(text):
            return pattern.pattern
    return None


def sanitize_value(value: str) -> str:
    """Replace the whole value when it carries prohibited content.

    Whole-value replacement (rather than in-place masking) is deliberate: a
    value that embeds one secret cannot be trusted to be otherwise safe.
    """
    return REDACTED if find_violation(value) else value


def sanitize_tree(value: object) -> object:
    """Recursively sanitize every string in a JSON-shaped structure."""
    if isinstance(value, str):
        return sanitize_value(value)
    if isinstance(value, dict):
        return {k: sanitize_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_tree(v) for v in value]
    return value


class RedactionViolation(RuntimeError):
    """Raised when a fully serialized body still carries prohibited content.

    Reaching this means the allowlist and the per-value sanitizer were both
    bypassed (e.g. a prohibited *key* name was introduced in code review).
    The error handler converts it into an opaque 500 — fail closed.
    """

    def __init__(self, pattern: str) -> None:
        super().__init__(f"prohibited content matched pattern: {pattern}")
        self.pattern = pattern


def assert_body_safe(body: str) -> None:
    violation = find_violation(body)
    if violation is not None:
        raise RedactionViolation(violation)
