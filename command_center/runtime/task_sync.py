"""One-way, conservative sync from a v2 `run` (the execution-state source of
truth) onto the Kanban task it was launched for.

Never re-derives execution state from a task field — always reads the
`run` row fresh via `ExecutionCenterAPI`/`db`. Touches `launch_status` and
the small set of display/bookkeeping fields (`current_run_id`,
`report_path`, `repository_path`, `branch`, `last_run_at`,
`latest_verdict`, `pull_request_url`) that already exist on every task
record — and, as of this remediation, also `current_stage`/`progress` for a
genuinely `Completed` run's terminal sync (see `_apply_terminal_fields`),
mirroring the exact `models.set_current_stage` calls the retired v1
synchronous launch flow made. Before
this fix, progress/stage were *never* advanced for a v2-launched task: a
task launched once (which sets stage to "Workspace Verified", progress 5 —
see `launch.begin_launch`) stayed frozen at 5% forever, even after its run
reached `launch_status="Completed"` — a direct violation of "Completed
implies progress == 100" and the literal shape of the reported defect
(`state=COMPLETED`, `progress` stuck at 5%).
"""

from __future__ import annotations

from command_center import models, project_config, report_parser
from command_center.runtime import completion as completion_states
from command_center.runtime import db, providers, reports, session_view
from command_center.runtime.api import ExecutionCenterAPI
from command_center.runtime.completion_service import CompletionOrchestrator

# Session-view display status -> Kanban `models.LAUNCH_STATUSES` value, for
# every status *except* `STATUS_COMPLETED` — a genuinely-completed run does
# not automatically become launch_status "Completed"; see
# `_resolve_target_launch_status` for why (progress must reach 100 first,
# per the "Completed implies progress == 100" invariant — otherwise the
# task is "Needs Review": the agent's work is done, but nothing has
# reviewed/merged it yet).
_LAUNCH_STATUS_BY_DISPLAY_STATUS: dict[str, str] = {
    session_view.STATUS_LAUNCHING: "Launching",
    # `Starting` (spawned, awaiting first output) and `Stale` (spawned, probe
    # momentarily old) are *live, non-terminal* states — the process exists.
    # They map to the active Kanban launch statuses ("Launching"/"Running"),
    # never to a terminal one: a successfully spawned run must never flip its
    # task to Failed/Requires-Attention merely because early output or a fresh
    # probe has not arrived. Because neither is in
    # `session_view.TERMINAL_DISPLAY_STATUSES`, `_apply_terminal_fields` never
    # runs for them either.
    session_view.STATUS_STARTING: "Launching",
    session_view.STATUS_RUNNING: "Running",
    session_view.STATUS_STALE: "Running",
    session_view.STATUS_WAITING: "Requires Attention",
    session_view.STATUS_REQUIRES_ATTENTION: "Requires Attention",
    session_view.STATUS_BLOCKED: "Blocked",
    session_view.STATUS_INCOMPLETE: "Incomplete",
    session_view.STATUS_FAILED: "Failed",
    # An operator-stopped run is not a failure: it gets its own launch status
    # and moves the task to the "Closed" board lane (AICC-IMP-006, see
    # `_apply_terminal_fields`), instead of masquerading as "Failed".
    session_view.STATUS_CANCELLED: "Cancelled",
}

_TERMINAL_LAUNCH_STATUSES = frozenset({"Completed", "Needs Review", "Failed", "Blocked", "Incomplete", "Cancelled"})

# Launch statuses that mean "this run stranded the task; nothing will auto-
# advance it." An executor-availability failure (AICC-DESKTOP-017) flips any of
# these back to "Ready" so the next available agent in the chain is tried,
# instead of leaving the task parked on a dead provider.
_STRANDED_LAUNCH_STATUSES = frozenset({"Failed", "Requires Attention", "Blocked", "Incomplete"})

# --- P0 remediation: a resolved task masking an explicit rejection (audit D3) -
# Completion-pipeline states that are an explicit rejection/failure — the pipeline
# judged the delivered work NOT acceptable. A `Done` task whose completion sits in
# one of these has regressed *after* it was closed (e.g. a later review rejected
# the change), so its board status must stop asserting a green terminal outcome.
# Deliberately keyed on the completion pipeline's own verdict, not a raw run
# `state`: a benign failed *re-run* (working_tree_unchanged) seeds no completion,
# so a genuinely-resolved task keeps its green status.
_COMPLETION_REJECTION_STATES = frozenset(
    {
        completion_states.CompletionState.VALIDATION_FAILED,
        completion_states.CompletionState.REVIEW_REJECTED,
        completion_states.CompletionState.PR_CLOSED_UNMERGED,
        completion_states.CompletionState.MERGE_BLOCKED,
        completion_states.CompletionState.REQUIRES_ATTENTION,
        completion_states.CompletionState.RECOVERY_FAILED,
    }
)
# Launch statuses that assert the delivered work landed. A rejected completion
# must not be allowed to keep hiding behind one of these.
_SUCCESS_LAUNCH_STATUSES = frozenset({"Completed", "Needs Review"})


def _resolve_target_launch_status(status: str, task: dict) -> str:
    """`status` is the process-level `session_view.derive_status` result.
    Every non-`Completed` status maps 1:1 via `_LAUNCH_STATUS_BY_DISPLAY_
    STATUS`. `Completed` is resolved dynamically against the task's *current*
    `progress` (already advanced by `_apply_terminal_fields`, which always
    runs — for a terminal, not-yet-finalized run — before this is called):
    Progress 100 has two truthful terminal stages: "Merged" when merge
    evidence exists, and "Completed Locally" for the explicit local-only
    completion policy. A normal agent run can reach only "PR Ready" (95);
    the completion pipeline owns both terminal stages. Therefore an ordinary
    successful run remains "Needs Review" until that pipeline resolves it —
    this keeps "Completed implies progress == 100" a real invariant without
    making a false merge claim."""
    if status != session_view.STATUS_COMPLETED:
        return _LAUNCH_STATUS_BY_DISPLAY_STATUS[status]
    # A read-only task (review/audit/gate) has no merge/review lifecycle: its
    # report is the deliverable, so a clean `COMPLETED` is terminally "Completed",
    # not "Needs Review" (there is no PR to review) and never "Requires Attention"
    # (there is nothing to merge, so no completion pipeline runs for it — see
    # `_seed_and_project_completion`).
    if session_view.is_read_only_task_type(task.get("task_type")):
        return "Completed"
    return "Completed" if (task.get("progress") or 0) >= 100 else "Needs Review"


def _apply_terminal_fields(task: dict, run: dict, *, status: str, db_path) -> None:
    """The one-time work done exactly once per terminal run, guarded by the
    caller (`sync_task_from_run`). Reads the run's final result text via the
    same deterministic `report_parser.parse_report` v1.2 already uses — no
    new extraction logic. `status` is the already-computed
    `session_view.derive_status(run)` value, passed in rather than re-read
    from `task["launch_status"]` (which has not been updated for this sync
    yet — see `sync_task_from_run`'s ordering)."""
    report_row = db.get_report(db_path, run["id"])
    if report_row:
        task["report_path"] = report_row["path"]

    task["repository_path"] = run.get("repository_path")
    live_status = session_view.live_git_status(run.get("repository_path"))
    task["branch"] = (live_status or {}).get("branch") or run.get("expected_branch") or task.get("branch")
    task["last_run_at"] = run.get("completed_at")
    # Which provider actually produced this terminal outcome — recorded even
    # for failures, so the board can show "last tried with X" and the failover
    # logic's history survives the run row's eventual eviction from the window.
    if run.get("provider_id"):
        task["last_provider_id"] = run["provider_id"]

    # An operator-cancelled run retires its task to the "Closed" lane
    # (AICC-IMP-006): the operator explicitly stopped this work, so it must
    # leave the planning lanes rather than look abandoned there. Never demotes
    # a task already resolved as Done.
    if status == session_view.STATUS_CANCELLED and task.get("status") != "Done":
        task["status"] = "Closed"

    events = db.list_run_events(db_path, run["id"], after_seq=0, limit=1_000_000)
    parsed = report_parser.parse_report(reports.result_text(events)) if events else report_parser.empty_parsed_result()
    if parsed.get("verdict"):
        task["latest_verdict"] = parsed["verdict"]
    if parsed.get("pull_request_url"):
        task["pull_request_url"] = parsed["pull_request_url"]

    commit_hash = parsed.get("commit_hash")
    pull_request_url = parsed.get("pull_request_url")
    if commit_hash or pull_request_url:
        try:
            db.set_run_result_fields(
                db_path,
                run["id"],
                expected_version=run["version"],
                commit_hash=commit_hash,
                pull_request_url=pull_request_url,
            )
        except db.LostUpdateError:
            pass  # cosmetic run-row enrichment only; the task fields above are already set

    # Advance `current_stage`/`progress` for a genuinely completed run —
    # same rule the retired v1 synchronous launch flow used. Never for
    # `Blocked`/`Incomplete`/`Failed`/`Cancelled`: none of
    # those represent real forward progress, so `progress` must stay exactly
    # where it was (whatever stage the task was actually verified to reach),
    # not be nudged forward by a run that didn't deliver.
    if status == session_view.STATUS_COMPLETED:
        if pull_request_url:
            models.set_current_stage(task, "PR Ready")
            models.append_timeline_event(task, "pr_created", pull_request_url)
        elif models.is_passing_verdict(parsed.get("verdict")):
            models.set_current_stage(task, "Validation")
            models.append_timeline_event(task, "tests_passed", f"Вердикт: {parsed.get('verdict')}")

    event_type = "completed" if status == session_view.STATUS_COMPLETED else "launch_requires_attention"
    models.append_timeline_event(
        task, event_type, f"Синхронизировано из прогона `{run['id'][:8]}` (Live Execution Center v2)."
    )


def sync_task_from_run(task: dict, run: dict, *, db_path) -> bool:
    """Returns whether `task` was mutated (so the caller knows to persist
    via `save_tasks`)."""
    status = session_view.derive_status(run)
    mutated = False

    is_new_run_for_task = task.get("current_run_id") != run["id"]
    if (
        not is_new_run_for_task
        and status in session_view.TERMINAL_DISPLAY_STATUSES
        and task.get("terminal_projection_run_id") == run["id"]
    ):
        return False
    # ``launch_status`` cannot be the idempotency key: provider failures are
    # deliberately projected back to ``Ready`` for failover/retry, which made
    # every subsequent UI refresh re-append the same terminal and
    # ``executor_failed`` events.  Persist the run whose terminal facts were
    # projected instead.  A new remediation attempt has a new run id and is
    # therefore projected independently, while reload/restart remains a no-op.
    already_finalized_for_this_run = (
        not is_new_run_for_task
        and (
            task.get("launch_status") in _TERMINAL_LAUNCH_STATUSES
        )
    )

    if is_new_run_for_task:
        task["current_run_id"] = run["id"]
        mutated = True

    # A run's `state` (hence `status`) turns terminal before its finalization
    # — the `process_exited` event, the post-COMPLETED auto-commit, the report
    # write (`runtime.run_finalizer`, VOYN-W0-AICC-SRV-09-FINALIZED-AT) — is
    # durable: `Supervisor._supervise` commits the terminal row and finishes
    # that work afterwards, on a daemon thread this sync never waits on
    # (measured up to ~150ms wide on a run that left a dirty tree). Projecting
    # terminal fields from inside that window would read a still-missing
    # report/PR url, and the idempotency guard above (`terminal_projection_
    # run_id`) would then mark this run "already handled" forever — the real
    # data landing a moment later would never be picked up. So a terminal run
    # without `finalized_at` yet is left exactly as this tick found it: the
    # `current_run_id` bookkeeping above still applies, but no terminal field
    # or launch status is published until a later tick observes `finalized_at`
    # set.
    if status in session_view.TERMINAL_DISPLAY_STATUSES and not run.get("finalized_at"):
        return mutated

    # Advance progress for in-progress runs. While a run is RUNNING, the only
    # observable milestone is `first_output_at` — the agent has started
    # producing output. Move the task from the pre-launch "Workspace Verified"
    # (5%) to "Implementation" (40%) so the board reflects live activity instead
    # of staying frozen at 5% for the entire execution window. This is a
    # one-way forward advance: `set_current_stage` respects `progress_mode`
    # (manual overrides are preserved) and stages only move forward in the
    # STAGE_PROGRESS table.
    if (
        status == session_view.STATUS_RUNNING
        and run.get("first_output_at")
        and not already_finalized_for_this_run
    ):
        current_stage = task.get("current_stage")
        if current_stage in (None, "Created", "Workspace Verified"):
            models.set_current_stage(task, "Implementation")
            mutated = True

    if status in session_view.TERMINAL_DISPLAY_STATUSES and not already_finalized_for_this_run:
        # Must run *before* `target_launch_status` is resolved below: a
        # `Completed` run's launch status depends on `task["progress"]`
        # *after* this call's own stage advancement, not before it.
        _apply_terminal_fields(task, run, status=status, db_path=db_path)
        task["terminal_projection_run_id"] = run["id"]
        mutated = True

    target_launch_status = _resolve_target_launch_status(status, task)

    # --- Executor fallback auto-retry (AICC-DESKTOP-017) -----------------
    # A run that died because the *executor itself* is unavailable is a strong
    # signal the configured provider cannot run this task right now — e.g. an
    # expired OAuth token (`session_expired`), an exhausted quota
    # (`quota_limit`), a 5xx API outage (`provider_api_error`), or a startup
    # crash with no output (INTERRUPTED). `provider.availability()` only probes
    # `--version`, so it cannot detect any of these; the only honest signal is
    # the run's own `failure_reason`. Instead of stranding the task as
    # "Failed"/"Requires Attention" (or retrying the same dead provider until
    # the retry budget is gone), record the failed executor and flip the task
    # back to "Ready" so the next launch automatically tries the next available
    # agent in the chain (see `execution_queue.select_available_executor`). The
    # task only stays stranded when *every* allowed executor has failed (the
    # launch layer then reports `LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR`).
    if not already_finalized_for_this_run:
        reason = run.get("failure_reason")
        died_on_startup = (
            run.get("state") == "INTERRUPTED" and not run.get("first_output_at")
        )
        provider_unavailable = reason in providers.PROVIDER_UNAVAILABLE_REASONS
        if died_on_startup or provider_unavailable:
            failed_executor = run.get("provider_id") or task.get("executor") or "claude_code"
            failed_list = list(task.get("failed_executors") or [])
            if failed_executor not in failed_list:
                failed_list.append(failed_executor)
                task["failed_executors"] = failed_list
                mutated = True
            # Flip any "stranded, won't auto-relaunch" status back to "Ready" so
            # the next `launch_ready` picks up the next executor in the chain.
            # `died_on_startup` (INTERRUPTED, no output) resolves to "Requires
            # Attention"; a classified provider failure (state=FAILED) resolves to
            # "Failed" — both are stranded statuses, so both are re-queued. A
            # non-provider failure (`incomplete:working_tree_unchanged`,
            # `blocked:permission_denied:*`) never reaches here, so its
            # "Incomplete"/"Blocked" status is preserved.
            if target_launch_status in _STRANDED_LAUNCH_STATUSES:
                target_launch_status = "Ready"
                detail = (
                    f"Исполнитель «{failed_executor}» недоступен ({reason or 'нет вывода'})."
                    if provider_unavailable
                    else f"Исполнитель «{failed_executor}» не запустился (нет вывода)."
                )
                models.append_timeline_event(
                    task,
                    "executor_failed",
                    detail
                    + " Задача возвращена в очередь для повтора другим агентом.",
                )
                mutated = True
        elif run.get("state") == "INTERRUPTED":
            # Mid-run interruption (the agent HAD produced output, so the
            # provider itself is fine): a host/dispatcher death, a SIGKILL, a
            # reboot (NIGHT-W7-AICC-AUTONOMY). Stranding this as "Requires
            # Attention" would make every dispatcher restart a human incident;
            # instead re-queue for an automatic bounded retry — the scheduler's
            # own gate (`_RETRY_GATED_STATES` + `RetryPolicy` budget/backoff)
            # still governs when and how often it relaunches. The executor is
            # NOT marked failed: nothing here is evidence against the provider.
            if target_launch_status in _STRANDED_LAUNCH_STATUSES:
                target_launch_status = "Ready"
                models.append_timeline_event(
                    task,
                    "run_interrupted",
                    "Прогон прерван (перезапуск диспетчера или падение хоста). "
                    "Задача возвращена в очередь для автоматического повтора.",
                )
                # Explicit signal for the pipeline's re-enqueue step (6b):
                # the launched queue entry is closed, so without this the
                # planner would never see the task again.
                task["relaunch_requested"] = True
                mutated = True

    # On a clean completion, clear any accumulated executor failures — the
    # chain is healthy again and a future re-launch should try the configured
    # executor from scratch.
    if status == session_view.STATUS_COMPLETED and task.get("failed_executors"):
        task.pop("failed_executors", None)
        mutated = True

    if task.get("launch_status") != target_launch_status:
        task["launch_status"] = target_launch_status
        mutated = True

    if mutated:
        task["updated_at"] = models.iso_now()
    return mutated


# Completion-pipeline state -> Kanban `launch_status`. Once a run has a
# completion row, the completion pipeline (not the raw run outcome) governs the
# user-facing status: a finished process is "Needs Review" until something
# actually advances it, "Running" only while the pipeline is *actively*
# advancing (validating), "Needs Review" while a PR is open/merging/finalizing,
# and only "Completed" once the change is verified in the target branch. Closed-
# unmerged / validation-failed / blocked all surface as "Requires Attention".
#
# `EXECUTION_FINISHED` is the seed state every completion row is born in. It
# means the Claude *process* has exited (a completion row is only ever created
# for a terminal, `COMPLETED` run) — it is NOT evidence of any live activity.
# With the completion autopilot disabled (the default; see
# `Supervisor.start_completion_autopilot`), nothing advances the row past this
# seed, so mapping it to "Running" would strand a finished, PR-ready task as
# perpetually "Running" — a persisted, user-visible regression that conflates
# execution activity with engineering-lifecycle progress. It therefore projects
# to the honest, actionable "Needs Review" (matching the raw-run projection a
# completed run already resolves to), preserving default behavior. When the
# autopilot IS enabled, the row leaves `EXECUTION_FINISHED` within a tick and
# surfaces the genuinely-active "Running"/"Needs Review" states below.
_LAUNCH_STATUS_BY_COMPLETION_STATE: dict[str, str] = {
    completion_states.CompletionState.EXECUTION_FINISHED: "Needs Review",
    completion_states.CompletionState.VALIDATING_RESULT: "Running",
    completion_states.CompletionState.RESULT_VALID: "Running",
    completion_states.CompletionState.PREPARING_PULL_REQUEST: "Running",
    completion_states.CompletionState.RECOVERY_PENDING: "Running",
    # Waiting on the blocking independent review: honest "Needs Review" — the
    # work is done and something is judging it.
    completion_states.CompletionState.AWAITING_REVIEW: "Needs Review",
    completion_states.CompletionState.PULL_REQUEST_OPEN: "Needs Review",
    completion_states.CompletionState.AWAITING_MERGE: "Needs Review",
    completion_states.CompletionState.MERGED: "Needs Review",
    completion_states.CompletionState.VERIFYING_TARGET_BRANCH: "Needs Review",
    completion_states.CompletionState.COMPLETED: "Completed",
    completion_states.CompletionState.VALIDATION_FAILED: "Requires Attention",
    completion_states.CompletionState.REVIEW_REJECTED: "Requires Attention",
    completion_states.CompletionState.PR_CLOSED_UNMERGED: "Requires Attention",
    completion_states.CompletionState.MERGE_BLOCKED: "Requires Attention",
    completion_states.CompletionState.REQUIRES_ATTENTION: "Requires Attention",
    completion_states.CompletionState.RECOVERY_FAILED: "Requires Attention",
}


def sync_task_from_completion(task: dict, completion: dict) -> bool:
    """Project a completion row onto its task. Once seeded, the completion
    pipeline is the authority for `launch_status`; on terminal success it also
    advances the task to progress 100 and, **only when the completion carries
    real merge evidence** (a `merge_commit` or `pull_request_url`), sets
    `pull_request_status="merged"` (the field `recommend.py` reads). A local-only
    completion (`allow_local_only`: COMPLETED with no PR and no merge commit)
    never merged anything, so it neither claims "merged" nor records a
    "Merged into target branch" event (audit D4). Returns whether the task was
    mutated."""
    state = completion["completion_state"]
    mutated = False

    pr_url = completion.get("pull_request_url")
    if pr_url and task.get("pull_request_url") != pr_url:
        task["pull_request_url"] = pr_url
        mutated = True

    if state == completion_states.CompletionState.COMPLETED:
        # "merged" is a factual claim read by recommendation scoring and shown in
        # the UI; assert it only when a merge actually happened. The verified-
        # merge path records a `merge_commit`/`pull_request_url`; the opt-in
        # local-only path (allow_local_only) records neither.
        has_merge_evidence = bool(completion.get("merge_commit") or pr_url)
        desired_stage = "Merged" if has_merge_evidence else "Completed Locally"
        was_below_terminal_progress = (task.get("progress") or 0) < 100
        if task.get("current_stage") != desired_stage or was_below_terminal_progress:
            # A completion verdict is the terminal lifecycle fact. Preserve the
            # user's progress mode, but correct legacy rows that the old D4 code
            # already stamped as "Merged" without evidence.
            stage_mode = "manual" if task.get("progress_mode") == "manual" else "auto"
            models.set_current_stage(task, desired_stage, mode=stage_mode)
            mutated = True
        if was_below_terminal_progress:
            models.append_timeline_event(
                task,
                "completed",
                "Merged into target branch (completion pipeline)."
                if has_merge_evidence
                else "Завершено локально — merge не выполнялся (allow_local_only).",
            )
            mutated = True
        if has_merge_evidence and task.get("pull_request_status") != "merged":
            task["pull_request_status"] = "merged"
            mutated = True

    target = _LAUNCH_STATUS_BY_COMPLETION_STATE.get(state)
    if target and task.get("launch_status") != target:
        task["launch_status"] = target
        mutated = True

    if mutated:
        task["updated_at"] = models.iso_now()
    return mutated


def project_completion_to_kanban(task: dict, completion: dict) -> bool:
    """The final, planning-lane half of the completion projection: move the
    Kanban task itself to `Done` once — and only once — the completion pipeline
    has *verified* the merge landed in the target branch.

    Kept separate from `sync_task_from_completion` (which owns the execution-
    facing `launch_status`/stage/progress fields) because these are two
    genuinely different assertions with different consequences. `launch_status
    == "Completed"` says "this run's lifecycle finished"; `status == "Done"` is
    what `models.unmet_dependencies` reads, so setting it *releases every
    dependent task for execution*. That is the single most consequential field
    write in the whole pipeline, and it is deliberately gated on
    `CompletionState.COMPLETED` — the state reached only after
    `VERIFYING_TARGET_BRANCH` confirms the commit is reachable from the base
    branch. A PR that is merely open, or merged-but-unverified, never unblocks
    anything.

    Returns whether `task` was mutated."""
    if completion.get("completion_state") != completion_states.CompletionState.COMPLETED:
        return False
    if task.get("status") == "Done":
        return False
    task["status"] = "Done"
    models.append_timeline_event(
        task, "completed", "Задача закрыта: merge подтверждён в целевой ветке."
    )
    task["updated_at"] = models.iso_now()
    return True


def _seed_and_project_completion(
    api: ExecutionCenterAPI, task: dict, run: dict, *, policy_overrides: dict | None = None
) -> bool:
    """Ensure a completion row exists for a terminally-completed run (seeding is
    cheap and DB-only — it never runs validation or touches GitHub; that
    happens in the Supervisor's completion autopilot), then project its state
    onto the task. No-op for runs that did not complete successfully.

    `policy_overrides` is forwarded to `begin_completion` and therefore applies
    only to a row being created right now (see that method's docstring)."""
    # A read-only task (review/audit/gate) has no merge lifecycle — its report is
    # the deliverable. The completion pipeline is a validate → PR → merge → verify
    # state machine; running it for an analysis has nothing to do and spuriously
    # lands in MERGE_BLOCKED, which then projects the task to "Requires Attention"
    # even though the run succeeded. So a read-only run never seeds a completion,
    # and any completion left over from before this guard is ignored: the run's
    # own terminal projection (`_resolve_target_launch_status` → "Completed")
    # already says the true, terminal outcome.
    task_type = run.get("task_type") or task.get("task_type")
    if session_view.is_read_only_task_type(task_type):
        return False

    completion = db.get_completion(api.db_path, run["id"])
    if completion is None:
        # Not just terminal, but *finalized* (see the matching guard and its
        # rationale in `sync_task_from_run`): `begin_completion` below reads
        # the repository's current HEAD as the completion's `head_commit`.
        # Seeding inside the pre-finalization window would capture the SHA
        # from *before* the post-COMPLETED auto-commit lands, seeding a
        # completion — and, from it, a pull request — that is missing the
        # agent's own work.
        if run.get("state") != "COMPLETED" or not run.get("finalized_at"):
            return False
        cfg = project_config.get_project_config(run["project"])
        completion = CompletionOrchestrator(api.db_path).begin_completion(
            run, task=task, project_cfg=cfg, policy_overrides=policy_overrides
        )
        if completion is None:
            return False
    return sync_task_from_completion(task, completion)


def flag_done_regression(task: dict, completion: dict | None) -> bool:
    """Keep a resolved (`status == "Done"`) task's `launch_status` truthful about
    its completion pipeline, without ever un-resolving it.

    A `Done` task deliberately stays Done so its dependents remain released (see
    `project_completion_to_kanban`), and `sync_tasks` therefore skips the normal
    run/completion projection for it. But that skip is exactly why a resolved
    task could keep showing a green "Completed" while its completion pipeline has
    since landed in an explicit rejection state (audit D3: 8 such tasks masking
    REVIEW_REJECTED / MERGE_BLOCKED / REQUIRES_ATTENTION). This closes the gap in
    the one safe way — it touches only the *display* `launch_status`, never
    `status`, so no dependent is re-blocked:

    - a completion in an explicit rejection state while the task still shows a
      success status -> surface "Requires Attention" and record it once;
    - a completion that returns to COMPLETED after a prior regression -> clear the
      flag and restore "Completed".

    A benign failed *re-run* (e.g. `working_tree_unchanged`) seeds no completion,
    so `completion is None` and a genuinely-resolved task is left untouched.

    Pure w.r.t. I/O: the caller supplies the already-fetched completion row, so
    this never re-derives lifecycle state from a task field. Returns whether
    `task` was mutated."""
    if task.get("status") != "Done" or not completion:
        return False
    state = completion.get("completion_state")
    if state in _COMPLETION_REJECTION_STATES:
        if task.get("launch_status") not in _SUCCESS_LAUNCH_STATUSES:
            return False  # already surfaced (or never green) — idempotent
        task["launch_status"] = "Requires Attention"
        task["regressed_after_done"] = True
        models.append_timeline_event(
            task,
            "regressed_after_done",
            f"Completion pipeline: {state} уже после закрытия задачи "
            "— помечено «Требует внимания».",
        )
        task["updated_at"] = models.iso_now()
        return True
    if state == completion_states.CompletionState.COMPLETED and task.get("regressed_after_done"):
        task["regressed_after_done"] = False
        task["launch_status"] = "Completed"
        models.append_timeline_event(
            task, "regression_cleared", "Completion вернулся в COMPLETED — регрессия снята."
        )
        task["updated_at"] = models.iso_now()
        return True
    return False


def sync_tasks(
    api: ExecutionCenterAPI,
    tasks: list[dict],
    *,
    policy_overrides: dict | None = None,
) -> list[dict]:
    """Sync every task that has a `current_run_id` (i.e. was launched, at some
    point, through the v2 bridge) against that run's current state, seeding and
    projecting its completion row. Tasks never touched by v2 launch have no
    `current_run_id` and are left completely alone. Returns the mutated tasks
    for the caller to persist.

    Split out of `reconcile_and_sync` so a caller that has *already* reconciled
    — the desktop pipeline runs reconcile, then a completion advance, then this
    — can order those steps itself without paying for a second redundant
    `Supervisor.reconcile()`. `reconcile_and_sync` remains the one-call form for
    every existing caller."""
    mutated: list[dict] = []
    for task in tasks:
        # ``Done`` is the canonical engineering outcome: it is only assigned
        # after the operator or completion pipeline accepts the delivered
        # result. Historical run rows remain queryable, but a later refresh
        # must not project an older failed/review run back onto the resolved
        # task and turn Completed into Failed/Needs Review again — so the task
        # stays Done and its dependents stay released. It must, however, stop
        # asserting a green terminal status when its completion pipeline has
        # since landed in an explicit rejection (audit D3): `flag_done_regression`
        # surfaces that on the display `launch_status` only, never `status`.
        if task.get("status") == "Done":
            task_id = task.get("id")
            # Completion verdicts and raw runs are separate histories. A newer
            # benign run may legitimately have no completion row; looking only
            # at that run would then hide the task's latest explicit rejection.
            # Read the latest completion for the task itself instead.
            completion = api.get_completion_by_task(task_id) if task_id else None
            if flag_done_regression(task, completion):
                mutated.append(task)
            continue
        run_id = task.get("current_run_id")
        if not run_id:
            continue
        run = api.get_run(run_id)
        # Self-heal a stale or dangling `current_run_id`. A lost update can
        # clobber the launch's single-task `upsert_task` with a stale full-list
        # `save_tasks`, freezing the pointer on an old run while a newer run
        # exists in the DB (the old run is usually still present, so a plain
        # `get_run` does NOT return None here). Prefer the actually-newest run
        # for the task so a fresh FAILED run is still synced (and its executor
        # failover still fires) instead of stranding the task on the old run's
        # status forever. When the pointer is current, `latest` equals `run` and
        # nothing changes; when no runs exist, both stay None and we skip.
        task_id = task.get("id")
        if task_id:
            latest = api.get_latest_run_for_task(task_id)
            if latest is not None and (run is None or latest["id"] != run_id):
                run = latest
        if run is None:
            continue
        changed = sync_task_from_run(task, run, db_path=api.db_path)
        # Seed + project the completion pipeline (post-execution lifecycle).
        if _seed_and_project_completion(api, task, run, policy_overrides=policy_overrides):
            changed = True
        if changed:
            mutated.append(task)
    return mutated


def reconcile_and_sync(api: ExecutionCenterAPI, tasks: list[dict]) -> list[dict]:
    """Reconciles every persisted `RUNNING` row against real OS processes
    (`Supervisor.reconcile()`, already implemented and tested — never
    duplicated here), then syncs every launched task against its run's current
    state via `sync_tasks`. Returns the mutated tasks for the caller to
    persist."""
    api.reconcile()
    return sync_tasks(api, tasks)
