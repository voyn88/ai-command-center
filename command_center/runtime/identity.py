"""Process existence and identity, for launch-time recording and restart
reconciliation.

`kill(pid, 0)` alone only proves *some* process currently holds that pid — on
any long-running machine PIDs get reused, so a bare `kill(pid, 0) == 0` check
after a restart can easily be a completely unrelated process that happens to
have been assigned the same number. This module captures more than that: the
process's state, start time (`lstart`), and full command line, all from a single
`ps` call, so a later check can compare *identity*, not just "a pid exists".
POSIX zombies are deliberately treated as absent: they retain a PID until
their parent reaps them, but can no longer execute or own live work.

`ps` (not `/proc`) is used because this project targets macOS/BSD-likes as a
local development tool; `/proc` does not exist on macOS.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: str
    command: str

    def as_string(self) -> str:
        return f"{self.start_time}|{self.command}"


class ProcessQueryStatus(str, Enum):
    LIVE = "LIVE"
    ABSENT = "ABSENT"
    ZOMBIE = "ZOMBIE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProcessQuery:
    status: ProcessQueryStatus
    identity: ProcessIdentity | None = None


def _status_after_failed_ps(pid: int) -> ProcessQueryStatus:
    """Distinguish a confirmed absent pid from an unreadable live/unknown one."""
    if pid <= 0:
        return ProcessQueryStatus.ABSENT
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessQueryStatus.ABSENT
    except (PermissionError, OSError):
        return ProcessQueryStatus.UNKNOWN
    # The pid exists, but the identity query failed. It must not be treated as
    # gone: doing so could free a lease or terminalize work still in progress.
    return ProcessQueryStatus.UNKNOWN


def query_identity(pid: int, *, timeout: float = 5.0) -> ProcessQuery:
    """Return a tri-state-safe process query.

    ``UNKNOWN`` is intentionally distinct from ``ABSENT``/``ZOMBIE`` so a
    failing or incompatible ``ps`` cannot make live work appear gone.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "state=,lstart=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ProcessQuery(_status_after_failed_ps(pid))
    if result.returncode != 0:
        return ProcessQuery(_status_after_failed_ps(pid))
    line = result.stdout.strip("\n")
    if not line.strip():
        return ProcessQuery(_status_after_failed_ps(pid))
    # `state` is the first token; its first character is the portable primary
    # process state on both Linux procps and BSD/macOS ps. A zombie has exited
    # and cannot execute, even though it keeps a PID until its parent reaps it.
    # `lstart` is a fixed-width field (`ddd mmm dd hh:mm:ss yyyy`-shaped, though
    # locale can change the exact text); `command` is everything after it. We
    # don't need to parse the timestamp, only compare it verbatim, so no
    # locale-specific date parsing is required here.
    parts = line.split(None, 6)
    if parts and parts[0].startswith("Z"):
        return ProcessQuery(ProcessQueryStatus.ZOMBIE)
    if len(parts) < 7:
        return ProcessQuery(ProcessQueryStatus.UNKNOWN)
    start_time = " ".join(parts[1:6])
    command = parts[6]
    process_identity = ProcessIdentity(
        pid=pid, start_time=start_time, command=command
    )
    return ProcessQuery(ProcessQueryStatus.LIVE, process_identity)


def capture_identity(pid: int, *, timeout: float = 5.0) -> ProcessIdentity | None:
    """Return a confirmed live identity, else ``None``.

    Callers making a liveness decision must use :func:`query_identity` or
    :func:`process_exists`; ``None`` alone intentionally does not distinguish
    absent/zombie from an unqueryable process.
    """
    return query_identity(pid, timeout=timeout).identity


def process_exists(pid: int) -> bool:
    status = query_identity(pid).status
    return status not in {ProcessQueryStatus.ABSENT, ProcessQueryStatus.ZOMBIE}


def identity_matches(pid: int, recorded_identity: str | None) -> bool:
    """True only if `pid` currently exists *and* its captured identity string
    matches `recorded_identity` exactly. False (never an exception) for a
    dead pid, a reused pid running a different command, or a missing/blank
    recorded identity (nothing to compare against)."""
    if not recorded_identity:
        return False
    query = query_identity(pid)
    if query.status is not ProcessQueryStatus.LIVE or query.identity is None:
        return False
    return query.identity.as_string() == recorded_identity
