"""Shared primitives for invoking the external ``voyn-lease`` CLI tool.

``orchestrator.publish`` (the lease held around a `git push`) and
``worker.writer_lease`` (the lease held across the whole provision -> agent
-> tests -> publish lifecycle, VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE) both
shell out to the same external tool with the same identity argv shape. This
module is the one place that shape is defined, so the two callers cannot
drift on how a lease row's owner is constructed -- and so a third caller
never has a reason to write a third copy.

``voyn-lease`` itself is not vendored in this repository; it is an external
binary named by ``VOYN_LEASE_TOOL`` (default ``"voyn-lease"``) and reached
through a DSN named by ``VOYN_LEASE_DSN``. This module only knows its argv
contract, inferred from every existing call site
(``acquire``/``install-hooks``/``release``/``list``), not its implementation.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["LeaseIdentity", "lease_argv", "process_start", "run_lease"]


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    lease_tool: str
    repository: str
    owner: str
    session: str
    task: str
    # VOYN-W0-AICC-LEASE-TTL-CONTRACT-BROKEN (found live 2026-08-23): every
    # caller declares its own `ttl` config field (`WriterLeaseConfig.ttl`,
    # `PublishConfig.ttl`, both defaulting to 600) but `lease_argv` never
    # forwarded it to `--ttl` -- the external `voyn-lease` CLI silently used
    # its own default (180s) instead. `writer_lease.py`'s renewal thread
    # schedules its first renewal at `ttl/3` = 200s against the CONFIGURED
    # 600s, which is 20s AFTER the row actually expired under the CLI's
    # real 180s default -- live-confirmed causing real lease cancellations
    # (VOYN_LEASE_REFUSED expired, lease process is still alive) on working
    # agent runs, discarding their output. Defaults to 600 to match the
    # value every caller already declared as its intent, not the CLI's own
    # undeclared 180s.
    ttl: int = 600


def process_start(pid: int | None = None) -> str:
    """Field 22 of ``/proc/<pid>/stat`` -- the same identity token the lease
    authority stores, immune to pid reuse (a recycled pid cannot inherit a
    dead process's lease). Falls back to ``"0"`` where unavailable; the
    lease tool still keys on pid+owner+session, which is enough for the
    single-writer guarantee even without this extra check."""
    target = os.getpid() if pid is None else pid
    try:
        with open(f"/proc/{target}/stat", encoding="ascii") as fh:
            return fh.read().split(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return "0"


def lease_argv(identity: LeaseIdentity, verb: str, repo_path: Path) -> list[str]:
    """The argv every ``voyn-lease`` invocation shares: ``--repo`` names the
    working directory the tool resolves to a repository (and, for
    ``install-hooks``, to that repository's common git dir -- shared by
    every worktree of the clone); the identity flags are what ``verify``
    checks a push's on-disk hook state against.

    ``acquire`` alone also carries ``--auto-takeover`` (VOYN-W0-AICC-LEASE-
    STUCK-EXPIRED-NO-RECLAIM, found live 2026-08-22): a holder whose process
    died without releasing -- the common case for a killed/OOM'd worker or a
    task that hit its timeout -- leaves an expired-but-present row that
    every later ``acquire`` for that repository refused forever with
    ``VOYN_LEASE_REFUSED expired requires dead-process confirmation``,
    since nothing in this pipeline ever supplied the confirmation
    ``acquire``'s own CLI exposes for exactly this case. One repository-wide
    stuck row blocks every task routed to that repository, not just the one
    that died holding it -- traced live to 173 of 216 DEFER_TO_USER
    escalations on 2026-08-22, the large majority not being genuine task
    failures or ambiguity, but this. ``--auto-takeover`` asks the lease
    authority itself to verify the recorded holder is actually dead before
    granting the takeover (the same dead-process discipline
    ``worktree_lease._held_by_our_own_line`` already applies locally, kept
    server-side here since the authority, not this process, holds the
    other host's process state) -- never an unconditional override of a
    lease a live process still holds."""
    argv = [
        identity.lease_tool, "--repo", str(repo_path), verb,
        "--repository", identity.repository, "--owner", identity.owner,
        "--session", identity.session, "--task", identity.task,
        "--pid", str(os.getpid()), "--process-start", process_start(),
    ]
    if verb == "acquire":
        argv.append("--auto-takeover")
        # See LeaseIdentity.ttl's docstring: this was missing entirely --
        # the CLI silently granted its own default TTL instead of the one
        # every caller believed it had configured.
        argv.extend(["--ttl", str(identity.ttl)])
    return argv


def run_lease(
    argv: list[str], cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
    )
