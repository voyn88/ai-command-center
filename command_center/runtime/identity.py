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

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: str
    command: str

    def as_string(self) -> str:
        return f"{self.start_time}|{self.command}"


def capture_identity(pid: int, *, timeout: float = 5.0) -> ProcessIdentity | None:
    """Return the current identity of `pid`, or None if no such process exists
    (or it could not be queried). Never raises for an absent/invalid pid."""
    try:
        result = subprocess.run(
            ["ps", "-o", "state=,lstart=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip("\n")
    if not line.strip():
        return None
    # `state` is the first token; its first character is the portable primary
    # process state on both Linux procps and BSD/macOS ps. A zombie has exited
    # and cannot execute, even though it keeps a PID until its parent reaps it.
    # `lstart` is a fixed-width field (`ddd mmm dd hh:mm:ss yyyy`-shaped, though
    # locale can change the exact text); `command` is everything after it. We
    # don't need to parse the timestamp, only compare it verbatim, so no
    # locale-specific date parsing is required here.
    parts = line.split(None, 6)
    if parts and parts[0].startswith("Z"):
        return None
    if len(parts) < 7:
        # Unexpected `ps` output shape — still return *something* stable
        # rather than silently treating this as "no such process".
        return ProcessIdentity(pid=pid, start_time=line, command="")
    start_time = " ".join(parts[1:6])
    command = parts[6]
    return ProcessIdentity(pid=pid, start_time=start_time, command=command)


def process_exists(pid: int) -> bool:
    return capture_identity(pid) is not None


def identity_matches(pid: int, recorded_identity: str | None) -> bool:
    """True only if `pid` currently exists *and* its captured identity string
    matches `recorded_identity` exactly. False (never an exception) for a
    dead pid, a reused pid running a different command, or a missing/blank
    recorded identity (nothing to compare against)."""
    if not recorded_identity:
        return False
    current = capture_identity(pid)
    if current is None:
        return False
    return current.as_string() == recorded_identity
