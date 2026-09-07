"""Server-side redaction boundary — the last line before bytes leave AIOS.

Three complementary mechanisms, all fail-closed:

1. **Allowlist projection** — the mappers in `native_gateway.source` copy only
   named fields into pydantic DTOs with ``extra="forbid"``; anything the
   projection file carries beyond the allowlist never reaches a response.
2. **Prohibited-content scan** — every outbound string value is scanned for
   secret-shaped content and replaced with ``[REDACTED]`` if it matches; the
   fully serialized response body is then scanned once more, and any residual
   hit aborts the response with a safe 500 instead of leaking.
3. **Path-redacting log filter** (`PathRedactingFilter`) — every emitted log
   record has its ``pathname``, formatted message, and cached exception text
   scrubbed of absolute filesystem paths (e.g. the ``code_filepath``/
   ``pathname`` metadata every stdlib log record carries points at an
   absolute path on the machine that emitted it). Paths inside the repository
   are rewritten relative to the *runtime* repo root (derived from this
   module's own location, not a hardcoded list of root names), so it holds
   regardless of deployment topology (``/home/...`` on a dev box, Docker's
   ``/app`` or ``/usr/src/app``, ``/workspace`` in CI, etc.). Paths outside
   the repo — third-party library frames, temp dirs, anything — are replaced
   outright rather than passed through, because a coverage gap in this filter
   would otherwise leak the operator's local filesystem layout into every
   single log line.

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


# --------------------------------------------------------------------------
# Path-redacting log filter.
#
# Derived from the *runtime* location of this module rather than a hardcoded
# list of root directory names, so relativization holds no matter where the
# gateway is deployed (a developer's `/home/...` checkout, a Docker image
# rooted at `/app` or `/usr/src/app`, a CI runner under `/workspace`, ...).
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

PATH_REDACTED = "<path-redacted>"

# Absolute POSIX path: a leading "/" not itself preceded by a word character
# or a colon (which would mean we're mid-way through a URL such as
# "https://host/path" or a "scheme:/path" token), followed by one or more
# path segments made of ordinary filename characters. Deliberately *not*
# anchored to any fixed set of root directory names — every absolute path is
# a candidate, and each one is individually resolved and either relativized
# or fully redacted below.
_ABS_POSIX_PATH = re.compile(r"(?<![:\w])/(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+")

# Absolute Windows path: a drive letter followed by a backslash.
_ABS_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\(?:[^\s\"'()]*)")


def _relativize_or_redact(raw: str) -> str:
    """Rewrite `raw` relative to REPO_ROOT, or redact it if that's not possible.

    Fail-closed: any path that cannot be proven to live inside the repo
    (including anything that fails to parse/resolve) is fully replaced. This
    is the opposite of an allowlist-by-root-name approach, so it can't be
    silently bypassed by a deployment topology nobody enumerated up front.
    """
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            return raw
        resolved = candidate.resolve()
        rel = resolved.relative_to(REPO_ROOT)
    except (OSError, RuntimeError, ValueError):
        return PATH_REDACTED
    return rel.as_posix()


def relativize_filepaths(text: str | None) -> str | None:
    """Strip/relativize every absolute filesystem path found in `text`."""
    if not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        return _relativize_or_redact(match.group(0))

    text = _ABS_POSIX_PATH.sub(_sub, text)
    # Windows paths are never "inside REPO_ROOT" on the POSIX hosts the
    # gateway runs on (pathlib parses them as plain relative POSIX text), so
    # there's nothing to relativize them against — always fully redact.
    text = _ABS_WINDOWS_PATH.sub(PATH_REDACTED, text)
    return text


class PathRedactingFilter(logging.Filter):
    """Strips absolute local filesystem paths out of every emitted record.

    Applied to ``record.pathname`` (the absolute source-file path every
    stdlib `LogRecord` carries — this is exactly the "code_filepath"-style
    metadata that leaks a developer's or operator's local directory layout),
    the fully formatted message, and any cached exception/stack text. Message
    formatting happens *before* `record.args` is cleared, so downstream
    formatters can't re-substitute unredacted arguments back in.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.pathname = _relativize_or_redact(record.pathname) or record.pathname

        message = record.getMessage()
        record.msg = relativize_filepaths(message)
        record.args = ()

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = relativize_filepaths(record.exc_text)

        stack_info = getattr(record, "stack_info", None)
        if stack_info:
            record.stack_info = relativize_filepaths(stack_info)

        return True


def install_path_redaction(logger: logging.Logger | None = None) -> PathRedactingFilter:
    """Attach a `PathRedactingFilter` everywhere records can reach a handler.

    A filter attached only to a *logger* object is applied solely to records
    logged directly against that logger (`Logger.handle` calls
    `self.filter(record)`, not each ancestor's) — records emitted via a child
    logger (e.g. ``logging.getLogger(__name__)`` in every module) reach the
    root logger's *handlers* without ever passing back through the root
    logger's own `.filter()`. So the filter must be attached to every handler
    reachable from `logger` (root by default), not just the logger itself,
    or most real-world log calls would sail straight past it unredacted.

    Idempotent: calling this more than once does not stack duplicate filters
    on the same logger/handler.
    """
    target = logger if logger is not None else logging.getLogger()

    def _add(filterer: logging.Filterer) -> PathRedactingFilter | None:
        for existing in filterer.filters:
            if isinstance(existing, PathRedactingFilter):
                return existing
        return None

    path_filter = _add(target)
    if path_filter is None:
        path_filter = PathRedactingFilter()
        target.addFilter(path_filter)

    for handler in target.handlers:
        if _add(handler) is None:
            handler.addFilter(path_filter)

    return path_filter
