"""systemd readiness/watchdog notifications, stdlib only (SRV-06).

Why not the ``sdnotify``/``systemd-python`` packages: the whole protocol is
one datagram to the socket named by ``NOTIFY_SOCKET``, and a dependency for
ten lines would be load-bearing supply chain for no capability. The protocol
reference is sd_notify(3).

Design constraints, each visible in the code:

* **Absent socket means not under systemd** — every call is a silent no-op,
  so the daemon runs identically in tests, in a terminal, and under the unit.
* **Notification failure must never fail the work.** A watchdog ping that
  cannot be sent is systemd's cue to restart us — exactly the supervision the
  ping exists to enable — so errors are swallowed by design, not by accident.
* **The env is read per call, not cached at import**, because tests and
  re-exec both change it, and a cached miss would silently disable the
  watchdog for the process's whole life.
"""

from __future__ import annotations

import os
import socket

__all__ = ["sd_notify", "watchdog_interval_seconds"]


def sd_notify(state: str) -> bool:
    """Send one state line (``READY=1``, ``WATCHDOG=1``, ``STOPPING=1``).

    Returns whether a datagram was actually sent — callers never need this
    for correctness, but tests do for proof.
    """
    target = os.environ.get("NOTIFY_SOCKET", "")
    if not target:
        return False
    if target.startswith("@"):  # abstract namespace (Linux): leading NUL
        target = "\0" + target[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(target)
            sock.send(state.encode("utf-8"))
        return True
    except OSError:
        return False


def watchdog_interval_seconds() -> float | None:
    """Half of systemd's ``WATCHDOG_USEC`` budget, or ``None`` when no
    watchdog is armed for this process.

    Half is sd_watchdog_enabled(3)'s own recommendation: one lost or late
    ping survives, two consecutive ones mean the process is genuinely wedged.
    ``WATCHDOG_PID`` gating matters because systemd forwards the env to
    children that must not inherit the obligation to ping.
    """
    usec = os.environ.get("WATCHDOG_USEC", "")
    if not usec.isdigit() or int(usec) <= 0:
        return None
    pid = os.environ.get("WATCHDOG_PID", "")
    if pid and pid != str(os.getpid()):
        return None
    return int(usec) / 2_000_000.0
