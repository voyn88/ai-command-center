#!/usr/bin/env python3
"""A stand-in for `claude` that spawns a real process tree — parent (this
process, the one the Supervisor launches directly) -> child -> grandchild —
all remaining in the *same* process group as the parent (none of the
`subprocess.Popen` calls below pass `start_new_session=True`, so `killpg`
against the parent's pgid reaches every level). Used by F4's grandchild
process-tree cancellation regression test.

Env vars:
- `FAKE_CLAUDE_TREE_PIDFILE`: base path. This script writes
  `<PIDFILE>.parent`, `<PIDFILE>.child`, `<PIDFILE>.grandchild`, each
  containing that process's pid, once the whole tree is up — the test polls
  for all three before triggering cancellation.
- `FAKE_CLAUDE_TREE_IGNORE_SIGTERM`: if "1", only the top-level parent
  installs a SIGTERM-ignoring handler (forcing the SIGKILL escalation path);
  the child/grandchild use the default SIGTERM action (immediate exit) so
  the test can also distinguish "died from SIGTERM" vs "needed SIGKILL" at
  the parent level specifically.
- `FAKE_CLAUDE_TREE_DESCENDANTS_IGNORE_SIGTERM`: if "1", child and
  grandchild ignore SIGTERM while the parent keeps the default action.
- `FAKE_CLAUDE_TREE_PARENT_EXIT_AFTER_START`: if "1", the parent exits
  naturally after reporting the tree, leaving its descendants alive unless
  the Supervisor drains the process group before reaping the leader.
- `FAKE_CLAUDE_TREE_GRANDCHILD_SETSID`: if "1", the grandchild calls
  `os.setsid()` before sleeping, making it the leader of a brand-new session
  and process group of its own. It stays a live child of `child` (its `ppid`
  is untouched), but it no longer shares the parent's pgid, so it is invisible
  to a cancellation path that only ever inspects "same pgid as the launch
  leader" — the escape this fixture exists to exercise.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

THIS_FILE = os.path.abspath(__file__)


def _write_pid(pidfile_base: str, role: str, pid: int) -> None:
    with open(f"{pidfile_base}.{role}", "w") as handle:
        handle.write(str(pid))


def _run_grandchild() -> None:
    if os.environ.get("FAKE_CLAUDE_TREE_GRANDCHILD_SETSID") == "1":
        os.setsid()
    if os.environ.get("FAKE_CLAUDE_TREE_DESCENDANTS_IGNORE_SIGTERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)


def _run_child(pidfile_base: str) -> None:
    if os.environ.get("FAKE_CLAUDE_TREE_DESCENDANTS_IGNORE_SIGTERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    grandchild = subprocess.Popen(
        [sys.executable, THIS_FILE, "--grandchild"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_pid(pidfile_base, "grandchild", grandchild.pid)
    time.sleep(60)
    grandchild.wait()


def _run_parent(pidfile_base: str) -> None:
    if os.environ.get("FAKE_CLAUDE_TREE_IGNORE_SIGTERM") == "1":
        signal.signal(signal.SIGTERM, lambda signum, frame: None)

    print(json.dumps({"type": "system", "subtype": "init"}))
    sys.stdout.flush()

    child = subprocess.Popen(
        [sys.executable, THIS_FILE, "--child", pidfile_base],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_pid(pidfile_base, "parent", os.getpid())
    _write_pid(pidfile_base, "child", child.pid)

    print(json.dumps({"type": "result", "result": "tree started"}))
    sys.stdout.flush()

    if os.environ.get("FAKE_CLAUDE_TREE_PARENT_EXIT_AFTER_START") == "1":
        # Do not exit until the child has recorded the grandchild pid. This
        # parent is the process-group leader, so the instant it exits the
        # Supervisor may observe the leader exit and drain the whole group
        # (residual SIGTERM -> SIGKILL) before the child has finished spawning
        # the grandchild and writing `<base>.grandchild`. On a loaded runner
        # that teardown wins the race, the grandchild pidfile never appears,
        # and the test's pidfile poll times out. Gating the exit on the tree
        # being fully materialised makes "whole tree up" happen-before "leader
        # exits" happen-before "group drain" — deterministic on any load.
        grandchild_pidfile = f"{pidfile_base}.grandchild"
        # 10s deliberately mirrors the test helper `_wait_for_tree_pids`'s own
        # poll deadline; keep them equal so this gate never gives up before the
        # poll does.
        deadline = time.monotonic() + 10.0
        while not os.path.exists(grandchild_pidfile) and time.monotonic() < deadline:
            time.sleep(0.01)
        return

    time.sleep(60)
    child.wait()


if __name__ == "__main__":
    if "--grandchild" in sys.argv:
        _run_grandchild()
    elif "--child" in sys.argv:
        _run_child(sys.argv[sys.argv.index("--child") + 1])
    else:
        _run_parent(os.environ["FAKE_CLAUDE_TREE_PIDFILE"])
