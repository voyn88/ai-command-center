"""One canonical parser for the stable task-workspace signing authority."""

from __future__ import annotations

import base64
import binascii
import os
import stat
from pathlib import Path

AUTHORITY_ENV = "AICC_WORKSPACE_AUTHORITY_KEY"
_AUTHORITY_DECODE_ERRORS = (ValueError, binascii.Error)
_AUTHORITY_MAX_BYTES = 1 << 16


def decode_workspace_authority_key(value: str | None) -> bytes | None:
    """Decode an explicit hex/base64 key and enforce 256-bit minimum entropy."""
    if not value:
        return None
    try:
        if value.startswith("hex:"):
            key = bytes.fromhex(value.removeprefix("hex:"))
        elif value.startswith("base64:"):
            key = base64.b64decode(value.removeprefix("base64:"), validate=True)
        else:
            return None
    except _AUTHORITY_DECODE_ERRORS:
        return None
    return key if len(key) >= 32 else None


def load_workspace_authority_environment(
    path: Path, *, require_root_owned: bool = True
) -> bytes:
    """Load a dedicated EnvironmentFile without ever evaluating it as shell.

    Opens the path exactly once with ``O_NOFOLLOW`` and validates/reads from
    that same file descriptor. Validating via ``lstat(path)`` and then
    reopening the pathname for ``read_text()`` would leave a check/use race: a
    replaceable parent directory lets an adversary swap the validated
    root-owned file for an attacker-controlled one between the two syscalls
    (review finding on 5f2f1dd).
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, flags)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("workspace authority environment must be a regular file")
        if require_root_owned and (
            info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o640
        ):
            raise ValueError(
                "workspace authority environment must be root-owned with mode 0640"
            )
        if info.st_size > _AUTHORITY_MAX_BYTES:
            raise ValueError("workspace authority environment is implausibly large")
        raw_bytes = os.read(file_descriptor, _AUTHORITY_MAX_BYTES + 1)
    finally:
        os.close(file_descriptor)
    if len(raw_bytes) > _AUTHORITY_MAX_BYTES:
        raise ValueError("workspace authority environment is implausibly large")

    assignments: list[tuple[str, str]] = []
    for raw in raw_bytes.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("workspace authority environment has malformed content")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        assignments.append((key.strip(), value))

    if len(assignments) != 1 or assignments[0][0] != AUTHORITY_ENV:
        raise ValueError(
            "workspace authority environment must contain exactly one authority key"
        )
    decoded = decode_workspace_authority_key(assignments[0][1])
    if decoded is None:
        raise ValueError(
            "workspace authority key must be explicit hex/base64 and decode to 32+ bytes"
        )
    return decoded
