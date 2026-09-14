"""Server-side redaction boundary — the last line before bytes leave AIOS.

Three complementary mechanisms, all fail-closed:

1. **Allowlist projection** — the mappers in `native_gateway.source` copy only
   named fields into pydantic DTOs with ``extra="forbid"``; anything the
   projection file carries beyond the allowlist never reaches a response.
2. **Prohibited-content scan** — every outbound string value is scanned for
   secret-shaped content and replaced with ``[REDACTED]`` if it matches; the
   fully serialized response body is then scanned once more, and any residual
   hit aborts the response with a safe 500 instead of leaking.
3. **Log path redaction** — `PathRedactingFilter` rewrites the absolute
   local-filesystem paths that `logging`'s own machinery injects (the call
   site's `pathname`, and `exc_info=True` tracebacks, which print each
   frame's absolute source file) to repo-relative form before a handler can
   emit them. This is the log-side counterpart to (2): the HTTP scan never
   sees log output, so it cannot cover this leak on its own.

The pattern list is a strict superset of the native client's own
`SnapshotDecoder` guard ("authorization", "bearer ", "password", "ssh-rsa",
"postgres://", "private_key", "prompt"): if the server let such content
through, every client would hard-fail the whole snapshot, so redacting here is
also a liveness requirement, not only a security one.
"""

from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path

REDACTED = "[REDACTED]"
REPO_ROOT = Path(__file__).resolve().parent.parent

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


# Absolute path spans (POSIX system roots and Windows drives) — the same
# roots `_PROHIBITED` flags above, but captured whole so the match can be
# rewritten in place rather than nuking the entire log line.
_ABS_PATH = re.compile(
    r"/(?:Users|home|var|etc|opt|srv|root|private|tmp)(?:/[^\s\"'()]*)?"
    r"|[A-Za-z]:\\[^\s\"'()]*"
)


def relativize_filepaths(text: str) -> str:
    """Rewrite absolute local filesystem paths to repo-relative form.

    A path under this checkout becomes a relative POSIX path — it names a
    file in version control, not a developer machine's or host's directory
    layout, so it is safe to keep for debugging. A path outside the checkout
    (interpreter, virtualenv, OS temp dir, another user's home) carries no
    diagnostic value worth the leak and is redacted outright.
    """

    def _sub(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            rel = Path(raw).resolve().relative_to(REPO_ROOT)
        except (OSError, ValueError):
            return REDACTED
        return rel.as_posix()

    return _ABS_PATH.sub(_sub, text)


class PathRedactingFilter(logging.Filter):
    """Strips absolute local filesystem paths out of every emitted record.

    Covers the three places `logging` can leak a machine's directory layout
    on its own, independent of what the caller passed as the log message:
    the call site's `record.pathname`, any absolute path interpolated into
    the message text, and — the sharpest edge, because it fires even when
    the caller never mentions a path — every frame of an `exc_info=True`
    traceback, which `logging` renders as ``File "<absolute path>", ...``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.pathname = relativize_filepaths(record.pathname)
        record.msg = relativize_filepaths(record.getMessage())
        record.args = ()
        if record.exc_info:
            exc_text = record.exc_text or "".join(
                traceback.format_exception(*record.exc_info)
            )
            record.exc_text = relativize_filepaths(exc_text)
        return True
