"""Opaque cursor pagination.

A cursor encodes ``(collection, offset, revision)``.  It is deliberately
opaque to clients (base64url) and deliberately transparent to the server —
no signing is needed because a tampered cursor can at worst read the same
allowlisted, already-redacted collections.

Revision binding: ``/v1/events`` cursors are bound to the projection revision
they were minted against.  When the projection advances, positions are no
longer stable (the producer may compact), so a stale events cursor yields a
409 ``resync_required`` instead of silently skipping or repeating events.
Task/dialog/decision cursors are offset-tolerant and not revision-bound.
"""

from __future__ import annotations

import base64
import binascii


def encode_cursor(collection: str, offset: int, revision: str = "") -> str:
    raw = f"1|{collection}|{offset}|{revision}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, collection: str) -> tuple[int, str] | None:
    """Return (offset, revision) or None for a malformed/foreign cursor."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[0] != "1" or parts[1] != collection:
        return None
    if not parts[2].isdigit():
        return None
    return int(parts[2]), parts[3]
