"""Finalize/outcome side of the v2 Session Supervisor (NIGHT-W9 follow-up).

Extracted verbatim from ``supervisor.py`` behind a small interface: the
Supervisor owns orchestration and delegates terminal-state persistence — the
bounded CAS fallback that marks a confirmed-gone run FAILED, the best-effort
lifecycle audit events, the post-COMPLETED auto-commit, and reading a run's
final ``result`` payload — to this class. Pure move: bodies are unchanged;
only the receiving object differs. The Supervisor keeps thin
``_persist_run_failure``-style delegating methods so existing callers and
tests (including monkeypatching of ``supervisor.db.*``) are unaffected —
this module resolves ``db``/``git_ops``/... through the same facade modules.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from command_center import agent_runner, provider_route
from command_center.models import iso_now
from command_center.runtime import db, git_ops, stream_parser

logger = logging.getLogger(__name__)

# Bounded compare-and-set attempts for terminal-state persistence (canonical
# home since the extraction; `supervisor._TERMINAL_CAS_MAX_ATTEMPTS` aliases
# this for its own CAS loops).
TERMINAL_CAS_MAX_ATTEMPTS = 5


class RunFinalizer:
    """Persists terminal outcomes and post-run bookkeeping for one store."""

    def __init__(self, db_path: Path, *, owner_token: str, owner_pid: int) -> None:
        self.db_path = db_path
        self.owner_token = owner_token
        self.owner_pid = owner_pid

    def _assert_current_process(self) -> None:
        if os.getpid() != self.owner_pid:
            raise RuntimeError(
                "A RunFinalizer inherited across fork cannot use parent authority."
            )

    def append_lifecycle_event_best_effort(self, lifecycle: str, run_id: str, **payload: object) -> None:
        """Persist audit telemetry without making lifecycle safety depend on it."""
        self._assert_current_process()
        try:
            db.append_run_event(
                self.db_path,
                run_id,
                "lifecycle",
                stream_parser.lifecycle_event(lifecycle, **payload)["payload"],
            )
        except Exception:
            logger.exception("Could not persist %s lifecycle event for run %s", lifecycle, run_id)

    def auto_commit_completed_work(self, run_id: str, repo_path: Path) -> str | None:
        """Commit whatever a `COMPLETED` run left uncommitted, so an agent's
        work is never lost to a forgotten commit.

        Best-effort *by construction*: the run's terminal state is already
        persisted when this runs, and every failure path here is swallowed into
        a lifecycle event. A commit that cannot be made must never demote a
        genuine `COMPLETED` — the changes then simply stay in the working tree
        exactly as the agent left them, which is where they were before this
        hook existed.

        Note this never rewrites the run row: `post_run_git_status` /
        `working_tree_changed` deliberately keep recording what the *agent*
        left behind, not what the supervisor did afterwards, because those are
        the inputs `outcome.classify_process_result` already ruled on.

        Returns the new HEAD sha on success, `None` when the tree was already
        clean (an idempotent no-op — the agent committed its own work, or the
        run was read-only) or the commit could not be made.
        """
        self._assert_current_process()
        message = (
            f"chore(agent): auto-commit work from run {run_id}\n"
            "\n"
            "Committed automatically by the AI Command Center supervisor: the "
            "run finished COMPLETED with a dirty working tree.\n"
            "\n"
            f"Run-Id: {run_id}"
        )
        try:
            proc = git_ops.commit_all(repo_path, message=message)
        except Exception as exc:
            logger.exception("Auto-commit failed for run %s", run_id)
            self.append_lifecycle_event_best_effort(
                "auto_commit_failed", run_id, error=str(exc)
            )
            return None

        if proc is None:
            self.append_lifecycle_event_best_effort("auto_commit_skipped_clean_tree", run_id)
            return None
        if proc.returncode != 0:
            self.append_lifecycle_event_best_effort(
                "auto_commit_failed",
                run_id,
                returncode=proc.returncode,
                error=(proc.stderr or "").strip()[:400],
            )
            return None

        head = None
        try:
            head = agent_runner.git_snapshot(repo_path).get("head")
        except Exception:  # pragma: no cover - snapshot is telemetry only
            logger.exception("Could not read HEAD after auto-commit for run %s", run_id)
        self.append_lifecycle_event_best_effort("auto_committed", run_id, head=head)
        return head

    def mark_finalized(self, run_id: str) -> bool:
        """Stamp the run's durability watermark — the last write of any
        finalization path (VOYN-W0-AICC-SRV-09-FINALIZED-AT).

        Every caller must place this *after* the writes it is vouching for: the
        `process_exited` event, the auto-commit, the report. The marker's whole
        meaning is that those are already durable, so a call moved earlier — in
        particular into the `fields` of `update_run_state` — would keep the
        column and destroy the guarantee, leaving a marker that says "finished"
        during the window in which nothing has been written yet.

        Best-effort in the same sense as the lifecycle events: a marker that
        cannot be written leaves the run *unfinalized*, which is the safe
        direction. A reader then waits or recovers a run that was in fact
        complete, where the failure in the other direction — declaring a run
        finalized when its report is missing — is the one that loses work.
        """
        self._assert_current_process()
        try:
            db.mark_run_finalized(
                self.db_path, run_id, owner_token=self.owner_token
            )
        except Exception:
            logger.exception("Could not mark run %s finalized", run_id)
            self.append_lifecycle_event_best_effort("finalization_marker_failed", run_id)
            return False
        current = db.get_run(self.db_path, run_id)
        return bool(current and current.get("finalized_at"))

    def finish_started_attempt(
        self, current: dict, *, fallback_failure_reason: str
    ) -> None:
        """Idempotently close the provider attempt for a terminal run."""
        self._assert_current_process()
        run_id = current["id"]
        attempts = db.list_provider_attempts(self.db_path, run_id)
        if not attempts or attempts[-1]["outcome"] != "started":
            return
        reason = current.get("failure_reason") or fallback_failure_reason
        state = current["state"]
        if state == "COMPLETED":
            attempt_outcome = "succeeded"
            classification = provider_route.SUCCESS
            disposition = provider_route.SUCCEEDED
            error_code = None
        elif state == "CANCELLED":
            attempt_outcome = "cancelled"
            classification = provider_route.CANCELLED
            disposition = provider_route.TERMINAL
            error_code = "cancelled"
        else:
            attempt_outcome = "failed"
            classification = provider_route.classify_failure(reason)
            disposition = provider_route.TERMINAL
            error_code = reason
        db.finish_provider_attempt(
            self.db_path,
            run_id=run_id,
            attempt_number=attempts[-1]["attempt_number"],
            outcome=attempt_outcome,
            classification=classification,
            disposition=disposition,
            error_code=error_code,
            completed_at=current.get("completed_at") or iso_now(),
        )

    def persist_run_failure(
        self,
        run_id: str,
        *,
        exit_code: int | None,
        failure_reason: str,
        lifecycle: str,
    ) -> bool:
        """Bounded CAS fallback after a locally owned child is confirmed gone.

        Return true only when the run is gone or a terminal state is confirmed.
        All exception classes are retried: a transient SQLite failure must not
        permanently strand an exited run in an active state.
        """
        self._assert_current_process()

        def finish_terminal_persistence(current: dict) -> bool:
            self.finish_started_attempt(
                current, fallback_failure_reason=failure_reason
            )
            self.append_lifecycle_event_best_effort(
                lifecycle,
                run_id,
                exit_code=exit_code,
            )
            stored = db.get_run(self.db_path, run_id)
            return bool(stored and stored["state"] in db.TERMINAL_STATES)

        for _ in range(TERMINAL_CAS_MAX_ATTEMPTS):
            try:
                current = db.get_run(self.db_path, run_id)
                if current is None:
                    return True
                if current["state"] in db.TERMINAL_STATES:
                    # A terminal row may belong to another live supervisor
                    # that is still writing its report/commit.  Only the
                    # explicit finalization claim can authorize completing it;
                    # the Supervisor recovery path verifies that ownership and
                    # reconstructs the required artifacts before marking.
                    return bool(current.get("finalized_at"))
                if current["state"] not in db.EXECUTION_CENTER_ACTIVE_STATES:
                    return False
                current = db.update_run_state(
                    self.db_path,
                    run_id,
                    expected_version=current["version"],
                    new_state="FAILED",
                    fields={
                        "exit_code": exit_code,
                        "completed_at": iso_now(),
                        "failure_reason": failure_reason,
                    },
                )
                # This helper persists only the terminal decision and provider
                # attempt.  The owning Supervisor must still create/verify the
                # immutable report before it may stamp finalized_at.
                return finish_terminal_persistence(current)
            except (db.LostUpdateError, db.InvalidTransitionError):
                continue
            except Exception:
                logger.exception("Could not persist %s for run %s", failure_reason, run_id)
                continue
        logger.error(
            "Run %s remained active after %s %s persistence attempts",
            run_id,
            TERMINAL_CAS_MAX_ATTEMPTS,
            failure_reason,
        )
        return False

    def persist_supervision_failure(self, run_id: str, *, exit_code: int | None) -> bool:
        self._assert_current_process()
        return self.persist_run_failure(
            run_id,
            exit_code=exit_code,
            failure_reason="supervision_failed",
            lifecycle="supervision_failed",
        )

    def final_result_payload(self, run_id: str) -> dict | None:
        """The payload of the run's own `result`-type event (the last line of
        `claude -p --output-format stream-json`'s output — carries `result`
        text and, when applicable, a `permission_denials` array), or `None`
        if no such event was persisted. Called only after both stdout/stderr
        reader threads have joined (see `_supervise`), so every event this
        run will ever produce is already committed to `run_event`."""
        self._assert_current_process()
        result_events = db.list_run_events(self.db_path, run_id, after_seq=0, limit=1_000_000, event_type="result")
        if not result_events:
            return None
        return result_events[-1]["payload"]
