"""The full-lifecycle writer lease (VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE).

Acquired before workspace provisioning and held -- via periodic renewal
independent of the agent subprocess -- through provisioning, the agent run,
any tests/lint the agent runs, and `publish_run`.

Why this exists despite `worktree_lease.blocking_lease` and
`publish.publish_run` already existing
----------------------------------------------------------------------------
`blocking_lease` is a deliberately read-only preflight (its own docstring:
"It never acquires anything ... the worker is not the lease holder"). It
answers "is this path already leased by someone else" once, before
dispatch, and nothing more.

`publish_run` DOES acquire a real lease, but only around its own `git push`
-- seconds at the very end of a run that can hold the workspace open for up
to `request.timeout_seconds` beforehand (an agent editing files, running
tests, committing) with no writer-lease coverage at all.
`worker/handlers.py`'s own `_provision_lock` docstring named this window
explicitly as a known gap, out of scope until this task closed it.

This module closes that window without inventing a second lease mechanism:
it is the same external `voyn-lease` tool and the same identity shape
`publish.py` already uses, via the shared `lease_client` module. `voyn-lease
acquire` re-run under an identity that already holds the row is documented
(see `publish.py`'s module docstring) to extend that row's TTL rather than
fail -- the same idempotency `publish_run`'s own re-acquire-at-push already
relies on -- so periodic re-acquire here is a renewal, not a second
competing claim. `install-hooks` is re-run on every successful renewal too
(not only at acquire), which keeps the pre-push hook's on-disk identity file
fresh for the whole run, not only in the seconds around the push
`publish_run` still separately re-provisions.

`release`, unlike `acquire`/`install-hooks`, is NOT idempotent under an
already-held lease -- it is a real termination of the row. When this
module's caller already holds the lease, it passes
`PublishConfig(release_lease=False)` so `publish_run` only re-affirms
(acquire, install-hooks) and leaves the one real release to this module's
own `hold()` exiting -- after `publish_run` returns and the caller's own
post-publish work (PR bookkeeping, worktree cleanup) has finished. An
earlier revision of this module let `publish_run` release unconditionally,
which silently dropped the lease mid-function, before that cleanup; see
`VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE`'s backlog entry for the
independent-review finding that caught it.

Renewal mirrors `worker.daemon.WorkerDaemon._heartbeat_loop`'s shape: a
background thread renews at a third of the lease TTL, and any failure to
renew -- another writer took the lease over, the authority is unreachable,
the row expired -- sets the *same* `lease_lost` threading.Event the caller
already wires into `agent_runner.run_claude_code` as `cancel_event`
(VOYN-W0-AICC-FORCED-AGENT-CANCELLATION, #349). There is deliberately no new
cancellation path: a lost writer lease forcibly terminates the running agent
through the identical mechanism a lost queue-visibility lease already uses,
and `daemon._execute` already discards any outcome once `lease_lost` is set,
so a lease lost to this cause is handled exactly like one lost to the other.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from command_center.worker import lease_client

__all__ = ["WriterLeaseConfig", "WriterLeaseUnavailable", "hold"]

logger = logging.getLogger(__name__)

# A third of the TTL, matching `WorkerDaemon._heartbeat_loop`'s reasoning
# verbatim: two consecutive renewal failures (an authority restart, a
# network blip) may pass before the lease actually lapses server-side.
_RENEW_FRACTION = 3.0
_MIN_RENEW_INTERVAL_SECONDS = 1.0


class WriterLeaseUnavailable(Exception):
    """Raised by `hold` when the initial acquire fails: another writer
    holds the lease, the authority refused the request, or it could not be
    reached at all. The caller must not provision a workspace or run an
    agent without a held lease -- catch this and fail the dispatch closed,
    same as `worktree_lease.blocking_lease` already does for its preflight."""


@dataclass(frozen=True, slots=True)
class WriterLeaseConfig:
    lease_tool: str  # path to voyn-lease
    repository: str  # e.g. "ai-command-center"
    owner: str  # writer identity, e.g. "server-worker"
    session: str
    task: str  # the backlog task id
    ttl: int = 600


def _identity(cfg: WriterLeaseConfig) -> lease_client.LeaseIdentity:
    return lease_client.LeaseIdentity(
        lease_tool=cfg.lease_tool,
        repository=cfg.repository,
        owner=cfg.owner,
        session=cfg.session,
        task=cfg.task,
        ttl=cfg.ttl,
    )


def _acquire_and_provision_hooks(repo_path: Path, cfg: WriterLeaseConfig) -> str | None:
    """Acquire (or, for an identity that already holds it, renew) the writer
    lease. Returns ``None`` on success, a bounded failure reason otherwise.

    Deliberately does NOT run ``install-hooks`` (VOYN-W0-AICC-LEASE-SCOPE-
    PER-TASK). It used to, "to keep the pre-push hook's on-disk identity
    file fresh for the whole run" -- but that file lives in the clone's
    COMMON git dir, shared by every worktree, while this lease is now
    task-scoped (see `WriterLeaseConfig.repository`). Writing a task-scoped
    identity into a clone-wide file, on a renewal timer, would race
    `publish_run`'s own repository-scoped `install-hooks` and could flip the
    file mid-push, making `verify` refuse a push that was correctly
    authorised. The freshness this bought is not needed either: nothing
    pushes during the agent run, and `publish_run` re-provisions the hook
    identity immediately before its own push (#351), which is the only
    moment the file is actually read."""
    identity = _identity(cfg)
    acquire = lease_client.run_lease(
        lease_client.lease_argv(identity, "acquire", repo_path), repo_path
    )
    if acquire.returncode != 0:
        detail = (acquire.stderr or acquire.stdout).strip()[:200]
        return f"acquire_failed: {detail}"
    return None


def _release(repo_path: Path, cfg: WriterLeaseConfig) -> None:
    identity = _identity(cfg)
    # Best-effort: a release that fails (authority unreachable, lease
    # already expired server-side) leaves nothing worse than the lease's own
    # TTL expiry would already produce, and there is no caller left to
    # report the failure to once the lifecycle this lease covers has ended.
    lease_client.run_lease(lease_client.lease_argv(identity, "release", repo_path), repo_path)


class _Handle:
    """The context manager `hold` returns. Runs a background renewal thread
    for its lifetime and releases the lease on exit, whatever the reason
    for that exit was."""

    def __init__(
        self, repo_path: Path, cfg: WriterLeaseConfig, lease_lost: threading.Event
    ) -> None:
        self._repo_path = repo_path
        self._cfg = cfg
        self._lease_lost = lease_lost
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._renew_loop, name="writer-lease-renew", daemon=True
        )

    def __enter__(self) -> "_Handle":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        _release(self._repo_path, self._cfg)

    def _renew_loop(self) -> None:
        interval = max(self._cfg.ttl / _RENEW_FRACTION, _MIN_RENEW_INTERVAL_SECONDS)
        while not self._stop.wait(interval):
            if self._lease_lost.is_set():
                # Already cancelled through some other path (e.g. the
                # queue's own visibility lease) -- nothing left to renew
                # for, and calling into the lease tool now would only race
                # `__exit__`'s own release.
                return
            failure = _acquire_and_provision_hooks(self._repo_path, self._cfg)
            if failure is not None:
                logger.warning(
                    "writer lease renewal failed for task %s: %s -- "
                    "forcing agent cancellation",
                    self._cfg.task,
                    failure,
                )
                self._lease_lost.set()
                return


def hold(
    repo_path: Path, cfg: WriterLeaseConfig, lease_lost: threading.Event
) -> _Handle:
    """Acquire the full-lifecycle writer lease and return a context manager
    that renews it in the background until released.

    Raises `WriterLeaseUnavailable` if the initial acquire fails -- the
    caller must not provision a workspace or run an agent without it. Use
    as ``stack.enter_context(hold(...))`` (an `ExitStack`, so the acquire
    can be tried and its failure converted to a normal retryable outcome
    before anything is entered) or a plain ``with hold(...):``.
    """
    failure = _acquire_and_provision_hooks(repo_path, cfg)
    if failure is not None:
        raise WriterLeaseUnavailable(failure)
    return _Handle(repo_path, cfg, lease_lost)
