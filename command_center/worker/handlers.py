"""The payload -> execution bridge (VOYN-W0-AICC-SRV-05, slice 2).

``agent_run`` is executed through the *existing* runner
(``command_center.agent_runner.run_claude_code``) rather than a new one:
that module already owns the sandbox-profile decision (audit D7 --
provenance-aware downgrade to read-only), VCS-credential scrubbing, timeout
handling and the never-raises result shape. This bridge adds only what the
queue context requires -- payload validation, repository validation, and
folding the run into a ``HandlerOutcome`` -- and deliberately owns nothing
the runner already decides.

Outcome discipline:

- a payload defect (bad version, missing fields, unknown repository) is
  **non-retryable**: redelivery cannot repair data;
- a run that *executed* is ``ok`` regardless of the agent's own exit --
  "the agent failed the task" is a result for the control plane to read,
  not a queue-level failure to redeliver, and retrying a completed mutating
  run would re-apply its side effects;
- only the case where execution could not start stays retryable: another
  host or a later moment can genuinely cure it. That covers both the runner
  never launching the process (OS error surfaced as status ``failed`` with
  no exit code and empty stdout) *and* the CLI process launching but the
  Claude-CLI account itself failing before any task work happened (rate
  limit / auth / overload -- the CLI's own structured ``is_error`` +
  ``api_error_status``/``terminal_reason`` fields, see
  ``agent_runner.RunResult.is_executor_api_error``). Both are executor
  infrastructure failures, not task outcomes, however different their raw
  ``exit_code``/``stdout`` shape looks.

Result rows are bounded: stdout/stderr travel as tails, because a jsonb
column is a coordination record, not a log store -- the full transcript
stays on the worker host's journal.
"""

from __future__ import annotations

import os
import re
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from command_center import agent_runner, project_config, workspace_provisioning
from command_center.orchestrator.publish import PublishConfig, publish_run
from command_center.worker import writer_lease
from command_center.worker.daemon import Handler, HandlerOutcome
from command_center.worker.payloads import PayloadError, parse_agent_run
from command_center.worker.worktree_lease import blocking_lease

__all__ = ["build_handlers"]

_TAIL_CHARS = 4000

# Sibling worktree directory, one per branch — the SAME convention
# `task_pipeline.derive_worktree_path` uses for the desktop launch path
# (`<repo>-worktrees/<branch-slug>`, outside the repository so a worktree
# never appears as untracked noise inside it). Kept as a private four-line
# duplicate here rather than an import: task_pipeline.py is the ~2400-line
# desktop pipeline module, and pulling it into the worker's dispatch path for
# one pure path-formatting function would wire an unrelated UI-facing module
# into a security-sensitive hot path. Matching the string convention (not
# importing the code) is what matters — it means a task's worker-provisioned
# worktree and its desktop-pipeline worktree for the same branch resolve to
# the identical path, so `blocking_lease` still detects a collision between
# the two launch paths. If a third caller needs this convention, extract it
# into `workspace_provisioning` then — not before.
# VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT naming decision: keyed by
# BRANCH (`backlog/<task>`), not by attempt. The payload contract
# (`worker.payloads.AgentRunRequest`) carries no `attempt_id`/`head_sha` --
# the only per-delivery identifier available at this call site is the
# queue's own `attempt_no` (a Handler parameter, not payload data). Keying by
# attempt_no instead of branch was rejected: git allows exactly one worktree
# per branch (`provision_workspace`'s `no_conflicting_worktree` gate), and
# `publish_run` always pushes to `backlog/<task>` regardless of which attempt
# produced the commit -- a worktree per attempt_no would either collide with
# that gate the moment two attempts of the same task existed, or require a
# second, attempt-scoped branch that `publish_run` does not know about. A
# worktree per TASK is the isolation the audit actually needs: the defect was
# different tasks/branches sharing ONE checkout, not one task's own retries
# sharing it with each other -- those retries sharing a worktree is
# correct, since they are building toward the same branch and the same PR.
_provision_locks: dict[str, threading.Lock] = {}
_provision_locks_guard = threading.Lock()


def _isolated_workspace_path(repository: Path, branch: str) -> Path:
    return workspace_provisioning.task_workspace_path(repository, branch)


def _task_lease_scope(request: Any) -> str:
    """The full-lifecycle writer lease's scope key (VOYN-W0-AICC-LEASE-SCOPE-
    PER-TASK): one lease per TASK, not per repository.

    Namespaced by project so two repositories cannot collide on a shared
    task id, and falling back to `project_id` when a payload carries no
    backlog task id keeps the pre-BO-S2a shape working -- such a payload has
    no task identity to scope by, so repository scope is the honest (and
    historical) answer for it.

    `VOYN_LEASE_REPOSITORY`, when set, is still honoured as the project half:
    it is how an operator points a host at a differently-named lease
    namespace, and silently ignoring it here would break that.
    """
    project = os.environ.get("VOYN_LEASE_REPOSITORY") or request.project_id
    task = getattr(request, "backlog_task_id", None)
    return f"{project}:{task}" if task else str(project)


def _provision_lock(key: str) -> threading.Lock:
    """One lock per resolved workspace path, so two in-process deliveries
    that both compute the same new path cannot both pass
    `provision_workspace`'s `workspace.exists()` check before either has
    created it (`provision_workspace` is check-then-act, not itself atomic
    against a concurrent identical call).

    This closes the race for THIS process only. The daemon's own claim loop
    (`worker.daemon.WorkerDaemon.run_forever`) dispatches one item at a time,
    so within a single worker process two deliveries never overlap in the
    first place; this lock is defense-in-depth for a future multi-threaded
    dispatcher, and for tests that call the handler directly from several
    threads. A concurrent SECOND PROCESS (this host or another) redelivering
    the SAME task while a first attempt is still running now finds the
    writer lease already held (VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE,
    below in `_run_agent`) and is refused by `voyn-lease acquire` itself --
    the gap this docstring used to name as explicitly out of scope is
    closed there, not here; this lock remains defense-in-depth for the
    in-process race described above only."""
    with _provision_locks_guard:
        lock = _provision_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _provision_locks[key] = lock
        return lock

#: BO-S3 result enrichment: the delivery contract for machine-readable
#: outcomes. A PR reference is extracted by its exact URL shape — nothing
#: else on GitHub looks like it — and the head SHA ONLY from an explicit
#: labelled trailer line (`HEAD_SHA: <hex>`), because a bare 40-hex string in
#: a transcript is any object id at all and guessing is what the
#: no-substring rule forbids. The planner's prompt asks the executor for the
#: trailer; an executor that omits it simply yields a result without a sha,
#: and the backlog's DONE gate holds until the fact arrives another way.
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_HEAD_SHA_TRAILER = re.compile(r"^HEAD_SHA:\s*([0-9a-f]{7,40})\s*$", re.MULTILINE)


def _machine_outcome(result_text: str) -> dict[str, str | None]:
    pr_match = _PR_URL.search(result_text)
    sha_match = _HEAD_SHA_TRAILER.search(result_text)
    return {
        "pr_url": pr_match.group(0) if pr_match else None,
        "head_sha": sha_match.group(1) if sha_match else None,
    }


def _tail(text: str) -> str:
    return text[-_TAIL_CHARS:] if len(text) > _TAIL_CHARS else text


def _cascade_link(request, attempt_no: int) -> dict[str, Any] | None:
    """BO-S2a: the cascade link for this delivery, selected by the queue's own
    attempt number — no new state, so failover rides the existing retry/reap
    machinery. Clamped at the tail: once the cascade is exhausted the last
    link keeps serving until the attempt budget (its length) dead-letters."""
    if not request.cascade:
        return None
    return request.cascade[min(attempt_no, len(request.cascade)) - 1]


def _executor_preflight(executor: str, task_type: str) -> tuple[bool, str, str]:
    if executor == "codex" and task_type in agent_runner.MUTATING_TASK_TYPES:
        available, detail = agent_runner.codex_workspace_write_preflight()
        return available, detail, "codex workspace-write sandbox unavailable"
    if executor == "codex":
        available, detail = agent_runner.claude_cli_preflight(agent_runner.CODEX_BINARY)
        return available, detail, "codex cli unavailable"
    if executor == "copilot":
        available, detail = agent_runner.claude_cli_preflight(
            agent_runner.COPILOT_BINARY
        )
        return available, detail, "copilot cli unavailable"
    available, detail = agent_runner.claude_cli_preflight()
    return available, detail, "claude cli unavailable"


def _run_agent(
    payload: dict[str, Any], lease_lost: threading.Event, attempt_no: int = 1
) -> HandlerOutcome:
    request = parse_agent_run(payload)
    if isinstance(request, PayloadError):
        if request.retryable:
            return HandlerOutcome.executor_infra_failure(request.reason)
        return HandlerOutcome.request_rejected(request.reason)

    link = _cascade_link(request, attempt_no)
    cascade_step = attempt_no if link is not None else None
    task_type = request.task_type
    model = request.model
    # A payload with no cascade at all (pre-BO-S2a shape, or a direct
    # enqueue) keeps the historical behaviour: Claude, unchanged.
    executor = "claude"
    if link is not None:
        executor = str(link.get("executor"))
        if executor not in agent_runner.COMMAND_BUILDERS:
            # Executor unavailability is a ROUTING signal, not a task error
            # (approved BO-S2a decision): a retryable refusal returns the
            # attempt to the pool and the next delivery selects the next
            # link. An unknown name on the LAST link exhausts the budget
            # into the dead letter, where the reason names the route.
            return HandlerOutcome.executor_infra_failure(
                f"executor_unavailable: {executor!r} (cascade step {attempt_no})"
            )
        task_type = str(link.get("task_type", task_type))
        model_override = link.get("model")
        if isinstance(model_override, str) and model_override.strip():
            model = model_override

    try:
        repository = agent_runner.validate_repository(
            request.project_id, request.repository_path
        )
    except agent_runner.RunnerError as exc:
        # A repository this host cannot see may exist on another: the row is
        # host-local state, so let redelivery try elsewhere -- bounded by the
        # item's own max_attempts.
        return HandlerOutcome.executor_infra_failure(str(exc))

    available, detail, unavailable_reason = _executor_preflight(executor, task_type)
    if not available and executor == "codex" and link is not None:
        # An open Codex circuit is a routing fact, not a consumed model
        # attempt. Select the next healthy cascade link inside this already
        # claimed delivery instead of returning it just to increment the
        # queue attempt counter.
        for candidate_step in range(attempt_no + 1, len(request.cascade) + 1):
            candidate = request.cascade[candidate_step - 1]
            candidate_executor = str(candidate.get("executor"))
            if candidate_executor not in agent_runner.COMMAND_BUILDERS:
                continue
            candidate_task_type = str(candidate.get("task_type", request.task_type))
            candidate_available, candidate_detail, candidate_reason = _executor_preflight(
                candidate_executor, candidate_task_type
            )
            if not candidate_available:
                detail, unavailable_reason = candidate_detail, candidate_reason
                continue
            link = candidate
            cascade_step = candidate_step
            executor = candidate_executor
            task_type = candidate_task_type
            model = request.model
            model_override = candidate.get("model")
            if isinstance(model_override, str) and model_override.strip():
                model = model_override
            available, detail, unavailable_reason = (
                candidate_available,
                candidate_detail,
                candidate_reason,
            )
            break
    if not available:
        return HandlerOutcome.executor_infra_failure(
            f"{unavailable_reason}: {detail}"
        )

    if lease_lost.is_set():
        # The lease died while we validated; starting a mutating run now
        # would produce effects no attempt row accounts for.
        return HandlerOutcome.executor_infra_failure(
            "lease lost before execution started"
        )

    # Provenance gate (audit D7, applied at the queue boundary): an untrusted
    # payload asking for a *mutating* task type is refused outright rather
    # than silently downgraded. profile_for_task would downgrade it to the
    # read-only profile -- but running a mutating prompt read-only produces a
    # half-executed task that looks completed, and masquerading the task type
    # to force the downgrade would lie to the audit trail. Non-retryable:
    # redelivery cannot add the operator elevation; the control plane
    # re-enqueues with untrusted=false after review.
    if request.untrusted and task_type in agent_runner.MUTATING_TASK_TYPES:
        return HandlerOutcome.request_rejected(
            f"untrusted payload requests mutating task_type "
            f"{task_type!r}; operator elevation required"
        )

    # The publish branch is `backlog/<task>` (publish.py) -- per-task, or
    # every dispatch for this project collides on one shared branch and a
    # later force-push silently erases an earlier task's still-unmerged work
    # (VOYN-W0-AICC-PUBLISH-BRANCH-COLLISION). Falls back to project_id only
    # for a payload enqueued before this field existed. Computed once and
    # reused for both the isolation branch below and PublishConfig.task, so
    # the worktree a run executes in and the branch it publishes to can never
    # drift apart.
    backlog_task = request.backlog_task_id or request.project_id

    # Isolated workspace per mutating task (VOYN-W0-AICC-ISOLATED-WORKTREE-
    # PER-ATTEMPT, a P0 audit finding: every mutating run for a project
    # shared ONE checkout -- `repository` -- with no isolation at all, so two
    # concurrently-dispatched tasks for the same project could corrupt each
    # other's working tree. `run_repository` is what actually gets handed to
    # the CLI and to `publish_run` below; for a read-only task it stays
    # `repository` unchanged (no isolation requirement, and no worktree churn
    # for the common read-only case). See `_isolated_workspace_path`'s
    # comment for the branch-not-attempt naming decision.
    run_repository = repository
    isolated_workspace: Path | None = None

    # VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE: everything from here to the
    # function's return travels inside one `ExitStack`, closed in the
    # `finally` below, so the full-lifecycle writer lease entered into it a
    # few lines down (mutating tasks only, see the comment there) is
    # released on every exit path -- workspace verification failure,
    # mid-run cancellation, a failed/timed-out run, or the ordinary
    # successful return after `publish_run`. For a non-mutating task
    # nothing is ever entered, so `stack.close()` is a no-op.
    stack = ExitStack()
    try:
        if task_type in agent_runner.MUTATING_TASK_TYPES:
            expected_branch = f"backlog/{backlog_task}"
            isolated_workspace = _isolated_workspace_path(repository, expected_branch)

            # Single-writer gate at the dispatch boundary (part B of
            # VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE), now checked
            # against the ISOLATED workspace -- the directory this dispatch
            # is actually about to write into -- rather than the shared
            # `repository`. Checked before provisioning: the path is
            # computed deterministically above with no filesystem access, so
            # this can run whether or not the worktree exists yet.
            # Retryable: a lease is a temporary claim, so redelivery lands
            # once it is released, bounded by the item's own max_attempts.
            held = blocking_lease(isolated_workspace)
            if held is not None:
                return HandlerOutcome.executor_infra_failure(held)

            # Full-lifecycle writer lease. `blocking_lease` above is a
            # deliberately read-only preflight (its own docstring: "it never
            # acquires anything") and `publish_run` below only acquires
            # around its own `git push` -- neither covers the window this
            # closes: provisioning, the agent run itself (which can hold the
            # workspace open for up to `request.timeout_seconds`), and any
            # local test/lint step the agent runs before committing.
            # Acquired here, BEFORE `provision_and_verify`, and held by
            # `writer_lease.hold`'s background renewal thread (mirroring
            # `worker.daemon.WorkerDaemon._heartbeat_loop`'s shape) all the
            # way through `publish_run` -- released only when the `finally`
            # below closes `stack`. A renewal failure sets the SAME
            # `lease_lost` event this function already wires into
            # `run_claude_code` as `cancel_event`
            # (VOYN-W0-AICC-FORCED-AGENT-CANCELLATION, #349): losing this
            # lease forcibly cancels the running agent through the identical
            # path a lost queue-visibility lease already uses, rather than a
            # second, parallel cancellation mechanism. `publish_run`'s own
            # acquire/install-hooks/release around the push stay unchanged
            # and become a harmless re-affirmation under an already-held
            # lease (its module docstring already documents that re-acquire
            # under the same identity is idempotent).
            #
            # Gated on `VOYN_LEASE_DSN` exactly like `blocking_lease` above:
            # a host with no configured lease authority has no lease to
            # hold, and must not be blocked by requiring a tool that was
            # never wired up for it.
            full_lifecycle_lease_held = False
            if os.environ.get("VOYN_LEASE_DSN"):
                lease_cfg = writer_lease.WriterLeaseConfig(
                    lease_tool=os.environ.get("VOYN_LEASE_TOOL", "voyn-lease"),
                    # TASK-scoped, not repository-scoped
                    # (VOYN-W0-AICC-LEASE-SCOPE-PER-TASK). This lease's stated
                    # purpose -- see `_provision_lock`'s docstring, which
                    # names it as the thing that closes the gap -- is to stop
                    # a SECOND PROCESS redelivering THE SAME TASK while a
                    # first attempt is still running. Keying it on the
                    # repository made it also block every OTHER task in that
                    # repository, which since #346 gave each task its own
                    # worktree is contention with nothing: two tasks in two
                    # worktrees share no mutable state during the agent run.
                    #
                    # Measured 2026-08-23, three hours after the executor
                    # cascade fix removed the quota bottleneck: 96 of 115
                    # returns-to-pool were `VOYN_LEASE_REFUSED active` -- 83%
                    # of all failures were tasks refusing each other for no
                    # physical reason. Two attempts of the SAME task still
                    # collide, which is correct: they share one worktree by
                    # design.
                    #
                    # Push serialization is unaffected: `publish_run` keeps
                    # its own repository-scoped lease around the push itself,
                    # which is the operation that genuinely needs one clone-
                    # wide writer at a time.
                    repository=_task_lease_scope(request),
                    owner=os.environ.get("AICC_PUBLISH_OWNER", "server-worker"),
                    session=os.environ.get("VOYN_LEASE_SESSION", "server-worker"),
                    task=backlog_task,
                )
                try:
                    stack.enter_context(
                        writer_lease.hold(repository, lease_cfg, lease_lost)
                    )
                    # `publish_run` below must not drop this lease itself --
                    # see `PublishConfig.release_lease` -- release happens
                    # only once, when `stack.close()` runs in this function's
                    # own `finally`.
                    full_lifecycle_lease_held = True
                except writer_lease.WriterLeaseUnavailable as exc:
                    # Another writer already holds the repository's lease,
                    # or the authority refused/could not be reached: a data
                    # refusal, not a defect -- the attempt returns to the
                    # pool and a later delivery retries once the lease
                    # clears, bounded by max_attempts.
                    return HandlerOutcome.executor_infra_failure(
                        f"writer lease unavailable: {exc}"
                    )

            base_branch = (
                project_config.get_project_config(request.project_id).get("default_branch")
                or "main"
            )
            spec = workspace_provisioning.WorkspaceSpec(
                workspace_path=str(isolated_workspace),
                expected_branch=expected_branch,
                base_branch=base_branch,
                repository_path=str(repository),
                task_type=task_type,
                task_local_git_metadata=True,
            )
            try:
                # Locked per-path: `provision_workspace` is check-then-act
                # (`workspace.exists()` then `git worktree add`), not itself
                # safe against two in-process callers racing to create the
                # same new path. See `_provision_lock`'s docstring for what
                # this does and does not cover.
                with _provision_lock(str(isolated_workspace)):
                    evidence = workspace_provisioning.provision_and_verify(spec)
            except workspace_provisioning.WorkspaceVerificationError as exc:
                # A verification failure here (branch already checked out
                # elsewhere, base branch missing, dirty leftover worktree
                # under a stricter policy, ...) is a fact about repository
                # state that a later moment can genuinely cure -- redelivery
                # retries once whatever blocked it clears, bounded by
                # max_attempts.
                return HandlerOutcome.executor_infra_failure(
                    f"workspace isolation failed at {exc.failed_step}: {exc.detail}"
                )
            run_repository = Path(evidence.workspace_path)

        # VOYN-W0-AICC-FORCED-AGENT-CANCELLATION: the same `lease_lost` event
        # this function already checked once, above, *before* starting the
        # subprocess, is now also handed to the runner as `cancel_event` so a
        # lease lost *mid-run* forcibly terminates the process group instead
        # of only being noticed after the fact, once the CLI exits on its own
        # (which could be up to `request.timeout_seconds` later, mutating the
        # isolated worktree the whole time). `run_claude_code` does not
        # return until the process group is either its own natural exit or
        # confirmed terminated (SIGTERM -> bounded grace -> SIGKILL, see
        # `agent_runner._terminate_process_group`) — so by the time this call
        # returns, `daemon._execute`'s `lease_lost.is_set()` check (which
        # discards the outcome without writing it back) is looking at an
        # attempt whose subprocess is provably no longer running, not one
        # still mutating state the daemon can no longer account for. This
        # does not by itself change *when* the queue considers the attempt
        # free for redelivery — that remains the lease's own
        # visibility-window expiry / supersession in `work_queue_store`,
        # unaffected by this change — it closes the narrower, previously-open
        # gap that the OS process kept running regardless of that decision.
        while True:
            run = agent_runner.run_claude_code(
                repository_path=run_repository,
                prompt=request.prompt,
                task_type=task_type,
                timeout_seconds=request.timeout_seconds,
                model=model,
                cancel_event=lease_lost,
                executor=executor,
            )
            if not (
                executor == "codex"
                and run.is_executor_sandbox_error
                and isolated_workspace is not None
                and not lease_lost.is_set()
            ):
                break
            agent_runner.disable_codex_workspace_write(_tail(run.stderr or run.stdout))
            unchanged = workspace_provisioning.task_workspace_is_unchanged(
                run_repository,
                expected_branch=evidence.expected_branch,
                remote_url=evidence.remote_url,
                start_sha=evidence.start_sha,
                trusted_base_sha=evidence.base_sha,
                expected_remote_sha=evidence.remote_task_sha,
                expected_inode=(evidence.workspace_device, evidence.workspace_inode),
            )
            if not unchanged or cascade_step is None:
                break
            fallback_selected = False
            for candidate_step in range(cascade_step + 1, len(request.cascade) + 1):
                candidate = request.cascade[candidate_step - 1]
                candidate_executor = str(candidate.get("executor"))
                if candidate_executor not in agent_runner.COMMAND_BUILDERS:
                    continue
                candidate_task_type = str(
                    candidate.get("task_type", request.task_type)
                )
                candidate_available, _, _ = _executor_preflight(
                    candidate_executor, candidate_task_type
                )
                if not candidate_available:
                    continue
                executor = candidate_executor
                task_type = candidate_task_type
                model = request.model
                model_override = candidate.get("model")
                if isinstance(model_override, str) and model_override.strip():
                    model = model_override
                link = candidate
                cascade_step = candidate_step
                fallback_selected = True
                break
            if not fallback_selected:
                break

        if lease_lost.is_set():
            # Cancellation confirmed: `run_claude_code` did not return until
            # the process group was either its own natural exit or
            # SIGTERM/SIGKILL confirmed terminated, so nothing from this run
            # is still executing. But the lease is gone, so this run's
            # output is unaccountable in exactly the sense the pre-flight
            # `lease_lost.is_set()` check above already guards against -- do
            # not extract/publish anything from it. In particular this must
            # return *before* the `publish_run` call below: publishing is a
            # real `git push` + PR creation, and doing that from a worktree
            # a forcibly-killed, possibly half-written agent run left behind
            # would be the same unaccountable-mutation class this whole
            # change exists to close, just moved one step later. The
            # worktree itself is deliberately left in place (matching every
            # other "do not delete on an ambiguous outcome" branch below) so
            # a later attempt can inspect or reuse it.
            # `daemon._execute` discards whatever `HandlerOutcome` is
            # returned here (its own `lease_lost.is_set()` check, checked
            # again after this handler returns) -- this early return is for
            # this function's own legibility and to keep `publish_run` from
            # ever running against a killed-mid-flight worktree, not to
            # change what the daemon does with the result.
            return HandlerOutcome.executor_infra_failure(
                "lease lost mid-execution; agent process group was forcibly terminated"
            )

        result_text = agent_runner.extract_result_text(run.stdout)
        result = {
            "cascade_step": cascade_step,
            "executor": (link or {}).get("executor", "claude"),
            **_machine_outcome(result_text),
            "status": run.status,
            "exit_code": run.exit_code,
            "duration_seconds": round(run.duration_seconds, 3),
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "stdout_tail": _tail(run.stdout),
            "stderr_tail": _tail(run.stderr),
            "result_text": _tail(result_text),
        }

        def checkpoint_preserved_candidate() -> HandlerOutcome | None:
            """Authenticate the committed prefix before an infrastructure retry."""
            if isolated_workspace is None:
                return None
            try:
                candidate_sha = workspace_provisioning.task_workspace_candidate_sha(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    expected_inode=(
                        evidence.workspace_device,
                        evidence.workspace_inode,
                    ),
                )
                if candidate_sha == evidence.start_sha:
                    return None
                with workspace_provisioning.trusted_publish_clone(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    remote_url=evidence.remote_url,
                    start_sha=evidence.start_sha,
                    trusted_base_sha=evidence.base_sha,
                    expected_remote_sha=evidence.remote_task_sha,
                    expected_inode=(
                        evidence.workspace_device,
                        evidence.workspace_inode,
                    ),
                    expected_candidate_sha=candidate_sha,
                    # An infrastructure failure can happen after a valid
                    # commit but before the agent finishes its remaining
                    # edits.  Only the exact committed SHA is checkpointed;
                    # dirty files stay outside that authority and the next
                    # allow-dirty retry can finish and commit them.
                    require_clean=False,
                ):
                    workspace_provisioning.checkpoint_task_workspace(
                        run_repository,
                        expected_branch=evidence.expected_branch,
                        previous_start_sha=evidence.start_sha,
                        expected_candidate_sha=candidate_sha,
                        expected_inode=(
                            evidence.workspace_device,
                            evidence.workspace_inode,
                        ),
                    )
            except workspace_provisioning.WorkspaceVerificationError as exc:
                return HandlerOutcome.executor_infra_failure(
                    (
                        "infrastructure retry checkpoint failed at "
                        f"{exc.failed_step}: {exc.detail}"
                    ),
                    result=result,
                )
            return None

        if run.status == "failed" and run.exit_code is None and not run.stdout:
            # The process never started (OSError path in the runner):
            # nothing executed, so redelivery is safe and may land on a
            # healthier host. The worktree (if any) holds no run output
            # either -- safe to remove immediately rather than leave an
            # empty one behind for every failed launch attempt.
            if (
                isolated_workspace is not None
                and evidence.provision_outcome == "cloned"
            ):
                workspace_provisioning.remove_workspace(
                    isolated_workspace,
                    repository,
                    verified_clean=True,
                    verified_inode=(evidence.workspace_device, evidence.workspace_inode),
                )
            return HandlerOutcome.executor_infra_failure(
                _tail(run.stderr) or "runner failed to start"
            )
        if (
            run.status in {"failed", "cancelled"}
            and isinstance(run.exit_code, int)
            and run.exit_code < 0
        ):
            # A negative process exit is a Unix signal, not a model verdict.
            # Preserve and authenticate any commit the interrupted process
            # managed to create, then return the attempt to the queue.  The
            # next worker can resume from that exact signed candidate without
            # replaying the already-durable commit.
            checkpoint_failure = checkpoint_preserved_candidate()
            if checkpoint_failure is not None:
                return checkpoint_failure
            return HandlerOutcome.executor_infra_failure(
                f"executor terminated by signal {-run.exit_code}", result=result
            )
        read_only_copilot_failure = (
            executor == "copilot"
            and task_type in agent_runner.READ_ONLY_TASK_TYPES
            and run.status != "completed"
        )
        if run.is_executor_provider_error(executor) or read_only_copilot_failure:
            # Incident 2026-08-21 16:09 UTC: the CLI process itself started
            # (exit code 1, non-empty stdout) but the *account*, not the
            # task, failed -- a rate limit / auth / overload response the
            # CLI reports through its own structured
            # `is_error`+`api_error_status`/`terminal_reason` fields (see
            # `RunResult.is_executor_api_error`), never by executing any
            # task work. Redelivery is safe and may land on a healthier
            # host/account. The clone is nevertheless preserved: a late or
            # spoofed classification must never delete the only local commit.
            checkpoint_failure = checkpoint_preserved_candidate()
            if checkpoint_failure is not None:
                return checkpoint_failure
            return HandlerOutcome.executor_infra_failure(
                (
                    "executor infrastructure failure "
                    f"(provider/auth/quota): {_tail(result_text or run.stderr)}"
                ),
            )
        if run.is_executor_sandbox_error:
            # bwrap failed before Codex could enter the sandbox or run tools.
            if executor == "codex":
                agent_runner.disable_codex_workspace_write(
                    _tail(run.stderr or result_text)
                )
            checkpoint_failure = checkpoint_preserved_candidate()
            if checkpoint_failure is not None:
                return checkpoint_failure
            return HandlerOutcome.executor_infra_failure(
                (
                    "executor infrastructure failure (Codex workspace-write "
                    f"sandbox): {_tail(run.stderr or result_text)}"
                ),
            )
        # BO-S3b: a successful mutating run publishes its commits as a PR so
        # the autonomous loop closes without a human. Opt-in by env
        # (AICC_PUBLISH_DEPLOY_KEY); unset = local commit only, review fleets
        # unaffected. `publish_run` takes a REPOSITORY-scoped lease around the
        # push -- that is the operation which genuinely needs one clone-wide
        # writer at a time, and it stays repository-scoped even though the
        # full-lifecycle lease above is now task-scoped.
        #
        # `release_lease` therefore turns on whether the two are THE SAME LEASE
        # ROW, not on whether an outer lease exists at all. It used to be
        # `not full_lifecycle_lease_held`, which was correct only while both
        # keys were the repository: publish must not drop a row its caller
        # still holds, and `stack.close()` released it. Once
        # VOYN-W0-AICC-LEASE-SCOPE-PER-TASK made the outer key
        # `<project>:<task>`, that reasoning silently inverted -- the caller
        # holds a DIFFERENT row, `writer_lease._release` only ever releases
        # its own, and nothing would have released `<project>`. It would have
        # leaked on the first mutating task and then refused every subsequent
        # push with `VOYN_LEASE_REFUSED active` for the worker process's
        # whole lifetime, un-reapable because `--auto-takeover` and
        # `ops/lease_reap.sh` both require a DEAD recorded holder and this one
        # is alive. Caught in independent review before merge.
        #
        # The comparison keeps the no-`backlog_task_id` fallback correct too:
        # there `_task_lease_scope` returns the bare project, the two keys
        # coincide, and `release_lease=False` is right again.
        deploy_key = os.environ.get("AICC_PUBLISH_DEPLOY_KEY", "")
        if task_type in agent_runner.MUTATING_TASK_TYPES and deploy_key:
            publish_repository = os.environ.get(
                "VOYN_LEASE_REPOSITORY", request.project_id
            )
            caller_holds_this_row = (
                full_lifecycle_lease_held
                and _task_lease_scope(request) == publish_repository
            )
            if not (
                evidence.expected_branch and evidence.remote_url and evidence.start_sha
            ):
                return HandlerOutcome.executor_infra_failure(
                    "guarded publish authority is incomplete"
                )
            try:
                candidate_sha = workspace_provisioning.task_workspace_candidate_sha(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    expected_inode=(
                        evidence.workspace_device,
                        evidence.workspace_inode,
                    ),
                )
                with workspace_provisioning.trusted_publish_clone(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    remote_url=evidence.remote_url,
                    start_sha=evidence.start_sha,
                    trusted_base_sha=evidence.base_sha,
                    expected_remote_sha=evidence.remote_task_sha,
                    expected_inode=(evidence.workspace_device, evidence.workspace_inode),
                    expected_candidate_sha=candidate_sha,
                ) as publish_clone:
                    # The fresh publisher clone above has now proved that the
                    # candidate is a clean descendant of the signed base
                    # without executing any agent-controlled Git config or
                    # hooks.  Advance retry authority before attempting the
                    # fallible lease/push/PR sequence.  If publication fails,
                    # the preserved task clone can therefore be verified and
                    # retried instead of being stranded behind the old signed
                    # start SHA.
                    workspace_provisioning.checkpoint_task_workspace(
                        run_repository,
                        expected_branch=evidence.expected_branch,
                        previous_start_sha=evidence.start_sha,
                        expected_candidate_sha=candidate_sha,
                        expected_inode=(
                            evidence.workspace_device,
                            evidence.workspace_inode,
                        ),
                    )
                    pub = publish_run(
                        publish_clone,
                        PublishConfig(
                            lease_tool=os.environ.get("VOYN_LEASE_TOOL", "voyn-lease"),
                            repository=publish_repository,
                            owner=os.environ.get("AICC_PUBLISH_OWNER", "server-worker"),
                            session=os.environ.get("VOYN_LEASE_SESSION", "server-worker"),
                            task=backlog_task,
                            deploy_key=deploy_key,
                            base=base_branch,
                            release_lease=not caller_holds_this_row,
                            base_sha=evidence.base_sha,
                            remote_sha=evidence.remote_task_sha,
                            remote_sha_known=True,
                        ),
                    )
            except workspace_provisioning.WorkspaceVerificationError as exc:
                return HandlerOutcome.executor_infra_failure(
                    f"guarded publish preparation failed at {exc.failed_step}: {exc.detail}"
                )
            result["publish"] = {
                "ok": pub.ok,
                "branch": pub.branch,
                "pr_url": pub.pr_url,
                "reason": pub.reason,
            }
            if pub.pr_url:
                result["pr_url"] = pub.pr_url
            # Cleanup is conditioned on the branch actually being durable
            # somewhere else first. `pub.ok` (a real push -- the commit now
            # lives on `origin/backlog/<task>`) and
            # `reason == "nothing_to_publish"` (publish.py's own `ok=False`
            # -- HEAD already equals the base branch, so the run made no
            # commit at all) both mean there is nothing local left to lose;
            # in either case the worktree is disposable. Any OTHER failure
            # (lease_unavailable / push_failed / pr_create_failed / cannot
            # read HEAD) deliberately leaves the worktree in place: it may
            # be the only remaining copy of whatever the agent committed,
            # and deleting it on a transient publish failure would be real,
            # unrecoverable data loss for the sake of tidiness.
            # "Recovery-safe" here means recoverable, not merely
            # crash-safe -- the next attempt's `provision_workspace` simply
            # reuses ("reused") the still-present worktree.
            publish_left_nothing_local = pub.ok or pub.reason == "nothing_to_publish"
            if isolated_workspace is not None and publish_left_nothing_local:
                workspace_provisioning.remove_workspace(
                    isolated_workspace,
                    repository,
                    verified_clean=True,
                    verified_inode=(evidence.workspace_device, evidence.workspace_inode),
                )
            if pub.reason in {
                "uncommitted_changes",
                "pinned_base_sha_missing",
                "head_not_descendant_of_pinned_base",
            }:
                return HandlerOutcome.executor_infra_failure(
                    f"publish precondition failed: {pub.reason}",
                    result=result,
                )
        elif isolated_workspace is not None:
            # Publishing is disabled for this deployment (no
            # AICC_PUBLISH_DEPLOY_KEY): the worktree's local commits are the
            # ONLY record of this run's work, exactly as the shared
            # checkout's commits were before this change. Removing it would
            # silently discard a completed mutating run's entire output, so
            # local-only mode never cleans up -- the worktree accumulates
            # the same way the shared checkout used to, and an operator
            # enabling publishing later can still recover it.
            try:
                # Local-only mode still needs a trusted checkpoint.  The
                # saved commit is the only durable task result here, and a
                # later retry (or enabling the guarded publisher) must be
                # able to authenticate and reuse it.
                candidate_sha = workspace_provisioning.task_workspace_candidate_sha(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    expected_inode=(
                        evidence.workspace_device,
                        evidence.workspace_inode,
                    ),
                )
                with workspace_provisioning.trusted_publish_clone(
                    run_repository,
                    expected_branch=evidence.expected_branch,
                    remote_url=evidence.remote_url,
                    start_sha=evidence.start_sha,
                    trusted_base_sha=evidence.base_sha,
                    expected_remote_sha=evidence.remote_task_sha,
                    expected_inode=(
                        evidence.workspace_device,
                        evidence.workspace_inode,
                    ),
                    expected_candidate_sha=candidate_sha,
                ):
                    workspace_provisioning.checkpoint_task_workspace(
                        run_repository,
                        expected_branch=evidence.expected_branch,
                        previous_start_sha=evidence.start_sha,
                        expected_candidate_sha=candidate_sha,
                        expected_inode=(
                            evidence.workspace_device,
                            evidence.workspace_inode,
                        ),
                    )
            except workspace_provisioning.WorkspaceVerificationError as exc:
                return HandlerOutcome.executor_infra_failure(
                    (
                        "local task checkpoint failed at "
                        f"{exc.failed_step}: {exc.detail}"
                    ),
                    result=result,
                )
        return HandlerOutcome.model_result(result)
    finally:
        stack.close()


def build_handlers() -> dict[str, Handler]:
    return {"agent_run": _run_agent}
