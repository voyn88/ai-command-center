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
through the identical mechanism a lost queue-visibility lease already uses.

THE TWO LEASES ARE NOT INTERCHANGEABLE PAST THAT POINT, and this paragraph
used to say they were: "`daemon._execute` already discards any outcome once
`lease_lost` is set, so a lease lost to this cause is handled exactly like
one lost to the other". It is no longer true and should never have been
(VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #14016). Discarding
an outcome is correct only when the QUEUE lease went, because only then is the
write refused. When THIS lease goes the queue lease is live and renewing, so
`queue_fail` is accepted -- and discarding it instead left the item `claimed`
under a lease nothing renews, which `control-01:queue` reads for the rest of
that window as an ATTENDED claim held by a lane that has already moved on.
`_execute` now keeps the two on separate events and reports a run this lease
cancelled; see its own comment for what the phantom occupancy cost the probe.
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

# A third of the TTL: a successful renewal leaves two whole intervals of
# margin before the lease lapses server-side.
#
# This used to say it was "matching `WorkerDaemon._heartbeat_loop`'s reasoning
# verbatim: two consecutive renewal failures ... may pass". Neither half of
# that survives inspection (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED): the beat
# loop's third of a window left NO margin, which is why it now derives its
# cadence from `TOLERATED_FAILED_BEATS` instead; and a failed renewal never
# "passes" HERE in the first place -- `_renew_loop` below sets `_lease_lost`
# and returns on the FIRST failure, so this fraction governs how much TTL a
# SUCCESSFUL renewal leaves in hand and nothing else. Whether this loop should
# absorb a transient authority blip the way the beat loop does is a real
# question about SRV-04a's takeover rules, and it is not this finding's to
# answer unmeasured.
#
# ONE THING THE BEAT LOOP DOES THAT THIS ONE WOULD HAVE TO COPY, when that
# question is answered: its tolerance is a DEADLINE and not a count of failed
# renewals (`daemon._heartbeat_loop`, VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
# `lease_client.run_lease` is `subprocess.run(..., timeout=120)`, so a failure
# HERE can take two minutes to arrive -- a third of the 600s TTL this fraction
# divides. A tolerance expressed as "N renewals may fail" would therefore mean
# anything between N intervals and N * (interval + 120s) of real time, and only
# the second of those is what the lease has left.
#
# AND A DEADLINE IS ONLY AS GOOD AS THE THREAD THAT READS IT, which is the
# second thing to copy (monitor_finding #41237). The beat loop's clock was
# correct and still mis-timed, because it was evaluated at the top of the loop
# and the step before that top was the call that had stopped answering; the
# lapse is now watched by a thread holding no connection at all. This loop's
# block is at least BOUNDED where the beat's was not -- `subprocess.run`'s own
# `timeout=120` is a client-side deadline psycopg has no equivalent of -- so
# the shape is survivable here and was not there. A tolerance added to this
# loop would have to say which thread enforces it.
#
# AND IT WOULD HAVE TO SAY WHAT THE DEADLINE PROVES, which is the third thing
# (monitor_finding #41318). A client-side clock measures what the CALLER SAW,
# not what the authority did: the beat loop's deadline was dated from the last
# beat the lane saw accepted, and `daemon._execute` read it as "the database
# has taken this attempt away" -- which an unanswered call cannot establish.
# This loop is already on the right side of that distinction and for a reason
# worth keeping: `voyn-lease acquire` is idempotent under an identity that
# already holds the row, so a renewal whose answer is lost leaves nothing to
# infer -- the next renewal re-asserts the same lease. A tolerance here would
# have to name which of the two facts it is measuring before it could decide
# anything on them.
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
    #
    # AND IT HAS TO BE BEST-EFFORT FOR A RAISE TOO, NOT ONLY FOR A NON-ZERO
    # EXIT (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED). Ignoring the
    # `CompletedProcess` made this best-effort against a REFUSAL; the first
    # failure the comment above names -- "authority unreachable" -- does not
    # arrive that way. `lease_client.run_lease` is
    # `subprocess.run(..., timeout=120)` and does not catch its own
    # `TimeoutExpired`, so a hung lease tool raised out of `_Handle.__exit__`,
    # which is the ExitStack `worker.handlers` closes IN ITS `finally`.
    #
    # So the exception replaced whatever that handler had already decided --
    # including a run that had finished and PUBLISHED. `_dispatch` catches it
    # and turns it into a retryable failure, so `queue_fail` requeues finished
    # work, the next delivery re-runs the whole attempt (`git push` and PR
    # creation included) and `queue_claim` charges another attempt for it. A
    # release is a cleanup of a lease whose lifecycle has already ended; it
    # cannot be allowed to decide the outcome of the work it was protecting.
    try:
        lease_client.run_lease(
            lease_client.lease_argv(identity, "release", repo_path), repo_path
        )
    except Exception:  # noqa: BLE001 - the release is cleanup, never a verdict
        logger.warning(
            "writer lease release for task %s did not complete; the row is "
            "left to its own TTL expiry",
            cfg.task,
            exc_info=True,
        )


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
            try:
                failure = _acquire_and_provision_hooks(self._repo_path, self._cfg)
            except Exception as error:  # noqa: BLE001 - see below
                # A RENEWAL THAT RAISES MUST NOT KILL THIS THREAD SILENTLY
                # (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
                #
                # `_acquire_and_provision_hooks` reads a RETURN CODE, so
                # every branch below it assumes `voyn-lease` ran and
                # answered. `lease_client.run_lease` is
                # `subprocess.run(..., timeout=120)` and does not catch its
                # own `TimeoutExpired`, so a lease tool that HANGS raises
                # straight through here -- and so does a missing or
                # unexecutable binary (`OSError`). This loop is a thread
                # target, so the exception ended the thread, and nothing
                # anywhere observed it:
                #
                #   * `_lease_lost` was never set, so the agent kept running;
                #   * nothing renewed the lease again, so the row expired
                #     server-side at `ttl` while this process still held it
                #     -- `lease_argv`'s own comment records what that does to
                #     the next claimant ("every later acquire for that
                #     repository refused forever with `VOYN_LEASE_REFUSED
                #     expired requires dead-process confirmation`", and
                #     `--auto-takeover` cannot help while the holder is
                #     alive);
                #   * and the module's whole promise -- a writer lease held
                #     "through provisioning, the agent run ... and
                #     `publish_run`" -- quietly stopped being true for the
                #     rest of a run that may last `request.timeout_seconds`.
                #
                # IT IS REACHED BY THE SAME BLIP THE QUEUE BEAT IS SIZED FOR.
                # `voyn-lease` talks to its authority over a DSN
                # (`/etc/aicc/lease.env`) and the credential rotation restarts
                # that tunnel on its own timer; a hung connect is exactly what
                # a restarting tunnel produces, and 120s of it is this
                # function's own default.
                #
                # Answered as a FAILED RENEWAL, which is what it is, rather
                # than with a new tolerance: `WorkerDaemon._heartbeat_loop`
                # absorbs `TOLERATED_FAILED_BEATS` of these because it can
                # prove how much lease it has left, and whether this loop
                # should do the same is the open question stated above
                # `_RENEW_FRACTION`. Failing closed is the answer that needs
                # no new measurement -- and it costs an attempt far less than
                # it used to, because `daemon._execute` now REPORTS a run
                # cancelled by this lease instead of discarding it.
                logger.exception(
                    "writer lease renewal raised for task %s -- forcing agent "
                    "cancellation",
                    self._cfg.task,
                )
                failure = f"renewal_raised: {error!r}"[:200]
            if failure is not None:
                if self._stop.is_set():
                    # THE HOLD IS ALREADY OVER, SO THERE IS NOTHING LEFT TO
                    # CANCEL (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED). The
                    # `_stop` test at the top of the loop is not enough on its
                    # own: `_acquire_and_provision_hooks` runs
                    # `subprocess.run(..., timeout=120)`, and `__exit__` joins
                    # this thread for only 10 seconds before releasing the
                    # lease and returning. So a renewal already in flight when
                    # the hold exits outlives it -- and then fails, BECAUSE
                    # `__exit__` released the row it was renewing.
                    #
                    # Setting `_lease_lost` there reaches a run that has
                    # already finished: this is `worker.handlers`' own
                    # cancellation event, and `daemon._execute` now REPORTS a
                    # run this lease cancelled rather than discarding it, so a
                    # late renewal failure turns a completed, PUBLISHED attempt
                    # into `queue_fail(retryable => true)`. The item is
                    # requeued, the next delivery re-runs the whole handler --
                    # `git push` and PR creation included -- and `queue_claim`
                    # charges another attempt for it, three of which
                    # dead-letter healthy work.
                    #
                    # Same lesson as `_release` above, which may not decide the
                    # outcome of the work it was protecting: a lease whose
                    # lifecycle has ended has no verdict left to give.
                    logger.warning(
                        "writer lease renewal for task %s failed after the "
                        "hold was released (%s); not cancelling -- the run it "
                        "protected has already finished",
                        self._cfg.task,
                        failure,
                    )
                    return
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
