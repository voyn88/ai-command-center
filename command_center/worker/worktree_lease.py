"""Does another writer already hold the worktree we are about to write into?
(VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE, part B.)

The writer lease exists so that exactly one process writes to a checkout at a
time, and the git hooks enforce it *for commits made through them*. The worker
never went through that door: it dispatched ``run_claude_code`` straight into
the routed repository path, so an agent could edit files in a tree another
writer holds. The hooks would stop the resulting commit, which is the good
case; the bad case is the working tree already carrying somebody else's edits
by then.

This module answers one question -- "is the path we are about to write into
covered by a live lease?" -- and answers it fail-closed. It never acquires
anything: the worker is not the lease holder and must not become one here.
``publish_run`` takes the lease for the push and releases it in a ``finally``,
so a lease taken around the run would be dropped early by that release.

Scope, stated rather than assumed:

- Only *mutating* dispatches are the caller's concern; a read-only run gets
  the read-only sandbox profile and cannot write into the tree at all.
- The gate is live when this process has a lease authority to ask --
  ``VOYN_LEASE_DSN``, the tool's only DSN source. Without it ``list`` cannot
  answer, and a host with no lease authority has no lease to violate.
- With an authority configured, every failure to ask it (missing tool,
  non-zero exit, unparseable output, timeout) blocks the dispatch. A guard
  that opens when it cannot see is not a guard.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["blocking_lease"]

_TIMEOUT_SECONDS = 30


def _this_host() -> set[str]:
    return {socket.gethostname(), socket.getfqdn()}


def _expiry(row: dict) -> datetime | None:
    raw = row.get("expires_at")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _covers(leased: str | None, repository: Path) -> bool:
    """A lease on a worktree covers that directory and everything under it.

    Equality alone would miss a dispatch routed at a subdirectory of a leased
    checkout, which is the same collision one level down.
    """
    if not isinstance(leased, str) or not leased:
        return False
    try:
        path = Path(leased).resolve()
    except OSError:
        return False
    return repository == path or repository.is_relative_to(path)


def _process_start(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` -- the same identity token the lease
    authority stores. Parsed past the last ``)`` because ``comm`` is
    parenthesised and may itself contain spaces, which a naive split would
    mis-column."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2 :].split()
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _ancestry() -> dict[int, str | None]:
    """This process and every ancestor, mapped to its start token.

    A lease held by one of our own ancestors is not a foreign writer: the
    supervisor that owns the tree is precisely who launched us, and refusing
    its own dispatch would deadlock the loop it runs. ``process_start`` is
    checked alongside the pid because pids are reused, and a recycled pid that
    happens to match an ancestor would otherwise open the gate.
    """
    seen: dict[int, str | None] = {}
    pid = os.getpid()
    while pid > 0 and pid not in seen:
        seen[pid] = _process_start(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return seen


def _held_by_our_own_line(row: dict, ancestry: dict[int, str | None]) -> bool:
    """True when this lease belongs to us or to a process that launched us."""
    pid = row.get("process_pid")
    if not isinstance(pid, int) or pid not in ancestry:
        return False
    claimed = row.get("process_start")
    ours = ancestry[pid]
    if claimed is None or ours is None:
        # The row names one of our pids but identity cannot be confirmed on
        # both sides. Fail closed: an unverifiable match is not a match.
        return False
    return str(claimed) == str(ours)


def blocking_lease(repository: Path) -> str | None:
    """Reason this dispatch must not run, or None when the path is clear."""
    if not os.environ.get("VOYN_LEASE_DSN"):
        return None
    tool = os.environ.get("VOYN_LEASE_TOOL", "voyn-lease")
    try:
        listed = subprocess.run(
            [tool, "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"writer-lease authority unreachable: {exc}"
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout).strip()[-400:]
        return f"writer-lease authority refused to list leases: {detail}"
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as exc:
        return f"writer-lease authority returned unreadable output: {exc}"
    if not isinstance(rows, list):
        return "writer-lease authority returned unreadable output: not a list"

    try:
        target = repository.resolve()
    except OSError:
        target = repository
    hosts = _this_host()
    now = datetime.now(timezone.utc)
    ancestry = _ancestry()
    for row in rows:
        if not isinstance(row, dict) or row.get("host") not in hosts:
            # A worktree path on another host is a different directory that
            # happens to share a name.
            continue
        if not _covers(row.get("worktree"), target):
            continue
        expires_at = _expiry(row)
        if expires_at is not None and expires_at <= now:
            # An expired lease holds nothing; takeover is the lease tool's
            # own decision, not ours to pre-empt.
            continue
        if _held_by_our_own_line(row, ancestry):
            continue
        return (
            f"worktree {target} is held by writer {row.get('owner')!r} "
            f"(session {row.get('session_id')!r}, pid {row.get('process_pid')}, "
            f"expires {row.get('expires_at')}); refusing to dispatch a "
            f"mutating run into another writer's checkout"
        )
    return None
