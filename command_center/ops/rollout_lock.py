"""The staged-rollout lock: one definition of the path and of "held".

`ops/aicc_staged_worker_rollout.py` stops the lane-mutating timers for a
rollout's duration and, as a second and independent defense, holds this
marker file so that a tick which fires anyway -- one already queued when its
timer was stopped, or one started by hand -- can notice a rollout is in
flight and defer instead of racing its lane mutations (live worker-01
2026-09-08: a self-deploy restart collided with the rollout's own `stop` of
the canary and systemd canceled one of the two jobs).

Both consumers that run inside the `command_center` package -- the rotation
controller and the self-deploy tick -- import the path and the
held/not-held decision from HERE rather than each spelling it out, because
a marker whose consumers disagree about its path or its meaning is not a
lock at all: every consumer would simply find "no lock" and proceed. The
rollout script itself cannot import this module (it runs under the system
interpreter from /usr/bin/python3 with only ops/ on its path, long before
any venv exists), and the systemd units and installer shell cannot import
Python at all, so those copies stay literal -- and
tests/ops/test_rollout_lock_consistency.py fails the build if any of them
drifts from this one, which is the enforcement the hand-sync comment used
to ask for politely.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ROLLOUT_LOCK_PATH",
    "rollout_lock_is_held",
    "rollout_lock_holder_is_alive",
]

#: The marker every consumer agrees on. `/run` is a tmpfs: a host that
#: reboots out of a crashed rollout starts with no lock at all, which is the
#: backstop behind the staleness check below.
ROLLOUT_LOCK_PATH = Path("/run/aicc-staged-rollout.lock")


def rollout_lock_holder_is_alive(pid: int) -> bool:
    """Is the process that wrote the lock still running?

    `/proc/<pid>` rather than `os.kill(pid, 0)`: the rotator runs as
    `aicc-rotator` and self-deploy as `voynadmin`, while the rollout that
    holds the lock is root -- `kill(2)` from an unprivileged reader against
    a root process raises EPERM, which is "alive" but reads identically to
    "no such process" unless every caller remembers to tell them apart.
    Reading /proc has no such trap and needs no privilege.

    Fail-closed where the question cannot be answered: a host with no
    /proc mounted (a container running the tests, most obviously) reports
    the holder alive, so an unreadable liveness signal never downgrades a
    real lock to a stale one.
    """
    if pid <= 0:
        return True
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    return (proc / str(pid)).exists()


def rollout_lock_is_held(path: str | os.PathLike[str] | None) -> bool:
    """True while a LIVE staged rollout holds `path`.

    Existence alone is deliberately not the answer. The rollout removes the
    lock from a `finally`, but a `finally` does not run for SIGKILL, a power
    cut, or an OOM kill -- and an existence-only check turns that crash into
    a permanent wedge: rotation and self-deploy both refuse forever (until
    the host reboots and the tmpfs clears) with nothing in the journal
    naming the dead holder. So the lock carries its writer's pid and a
    holder that is gone makes the lock stale, i.e. not held.

    Every ambiguous case resolves to "held", because the cost of deferring a
    tick that could have run is one timer interval, while the cost of
    running one that should have deferred is the canceled-job collision this
    whole mechanism exists to prevent:

    - unreadable, or content that is not a plain positive integer without
      a leading zero (the exact set every reader can agree on -- see the
      `case` in the units' ExecCondition): held;
    - a pid that is still running: held;
    - no /proc to ask: held (see `rollout_lock_holder_is_alive`).

    Pid reuse can keep a stale lock looking held; /run being a tmpfs bounds
    that to the current boot, and the rollout itself takes over a stale lock
    rather than waiting for it.
    """
    if not path:
        return False
    lock = Path(path).expanduser()
    try:
        raw = lock.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        # The file is there and cannot be read -- that is not evidence of
        # absence.
        return True
    holder = raw.strip()
    if not holder.isdigit() or holder.startswith("0"):
        return True
    return rollout_lock_holder_is_alive(int(holder))
