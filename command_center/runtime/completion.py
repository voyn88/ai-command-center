"""Domain core of the autonomous task-completion pipeline (AICC-AUTONOMY-001).

This module holds the *pure* decision logic — no subprocesses, no database, no
Streamlit — so it is exhaustively unit-testable in isolation:

  * the completion **state machine** vocabulary (`CompletionState`),
  * machine-readable **reason codes** (`ReasonCode`) and **actions**
    (`CompletionAction`),
  * the **policy** governing recovery/merge behavior (`CompletionPolicy`),
  * the **evaluator** (`CompletionEvaluator`) that maps a full evidence snapshot
    to a structured `CompletionAssessment` — never a bare boolean.

The evaluator is the single source of truth for "given everything we know about
this run, what completion state are we in and what should happen next?" The
side-effecting orchestration (running validation, pushing, opening/merging PRs,
verifying the target branch, persisting state) lives in
`runtime.completion_service`, which consults this evaluator but never
duplicates its decision rules.

Relationship to the other two state axes (documented for operators):
  * `run.state` (db.py) — the *execution* lifecycle of a Claude subprocess;
    terminal once the process exits. The completion pipeline begins only after a
    run reaches a terminal `COMPLETED` execution state.
  * `completion_state` (this module) — the *engineering* lifecycle from
    "process finished" to "merged into the target branch and verified".
  * Kanban `launch_status` (models.LAUNCH_STATUSES) — the coarse, user-facing
    projection, derived from the above by `session_view`/`task_sync`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# State machine vocabulary
# --------------------------------------------------------------------------

class CompletionState:
    # Happy path
    EXECUTION_FINISHED = "EXECUTION_FINISHED"
    VALIDATING_RESULT = "VALIDATING_RESULT"
    RESULT_VALID = "RESULT_VALID"
    # Independent review gate: the change is valid on its own terms, but a
    # separate read-only reviewer has not yet approved it. Blocking by
    # design — no pull request is opened from this state.
    AWAITING_REVIEW = "AWAITING_REVIEW"
    PREPARING_PULL_REQUEST = "PREPARING_PULL_REQUEST"
    PULL_REQUEST_OPEN = "PULL_REQUEST_OPEN"
    AWAITING_MERGE = "AWAITING_MERGE"
    MERGED = "MERGED"
    VERIFYING_TARGET_BRANCH = "VERIFYING_TARGET_BRANCH"
    COMPLETED = "COMPLETED"
    # Failure / intervention
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PR_CLOSED_UNMERGED = "PR_CLOSED_UNMERGED"
    MERGE_BLOCKED = "MERGE_BLOCKED"
    REQUIRES_ATTENTION = "REQUIRES_ATTENTION"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    # The independent reviewer rejected the change. Not a failure of the
    # machinery — a judgement about the work — so it is terminal here and is
    # picked up by the pipeline's rework loop, exactly like VALIDATION_FAILED.
    REVIEW_REJECTED = "REVIEW_REJECTED"


ALL_STATES: frozenset[str] = frozenset(
    {
        CompletionState.EXECUTION_FINISHED,
        CompletionState.VALIDATING_RESULT,
        CompletionState.RESULT_VALID,
        CompletionState.AWAITING_REVIEW,
        CompletionState.PREPARING_PULL_REQUEST,
        CompletionState.PULL_REQUEST_OPEN,
        CompletionState.AWAITING_MERGE,
        CompletionState.MERGED,
        CompletionState.VERIFYING_TARGET_BRANCH,
        CompletionState.COMPLETED,
        CompletionState.VALIDATION_FAILED,
        CompletionState.REVIEW_REJECTED,
        CompletionState.PR_CLOSED_UNMERGED,
        CompletionState.MERGE_BLOCKED,
        CompletionState.REQUIRES_ATTENTION,
        CompletionState.RECOVERY_PENDING,
        CompletionState.RECOVERY_FAILED,
    }
)

# Terminal success — the only state that means "the engineering task is done".
SUCCESS_STATE = CompletionState.COMPLETED

# States the pipeline will not auto-advance out of: either done, or awaiting a
# human. `REQUIRES_ATTENTION`/`VALIDATION_FAILED`/`RECOVERY_FAILED` all need a
# person (fix + re-run, or manual recovery). `COMPLETED` is success.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        CompletionState.COMPLETED,
        CompletionState.VALIDATION_FAILED,
        CompletionState.REVIEW_REJECTED,
        CompletionState.REQUIRES_ATTENTION,
        CompletionState.RECOVERY_FAILED,
    }
)

# States the bounded poller should keep advancing (with backoff).
ACTIVE_STATES: frozenset[str] = ALL_STATES - TERMINAL_STATES


# Explicit allowed-transition model for the completion state machine — the
# domain-level structural guard analogous to `db.ALLOWED_TRANSITIONS` for
# `run.state`. The orchestrator (`runtime.completion_service`) is the sole
# writer and already refuses to advance terminal rows in its control flow; this
# map makes the legal transition set explicit *data* so the persistence layer
# (`db.update_completion`) can refuse an illegal completion-state change — a
# backward jump, or any move out of a terminal state — independent of that
# control flow. Every entry is derived from a real orchestrator transition:
#
#   * validation phase: EXECUTION_FINISHED/VALIDATING_RESULT -> RESULT_VALID |
#     VALIDATION_FAILED | REQUIRES_ATTENTION | COMPLETED (already-in-target);
#   * PR phase (RESULT_VALID/PREPARING_PULL_REQUEST/PULL_REQUEST_OPEN/
#     AWAITING_MERGE/MERGE_BLOCKED): open/merge/verify/wait plus closed-unmerged
#     recovery (-> PULL_REQUEST_OPEN | RECOVERY_PENDING | RECOVERY_FAILED),
#     completion, and attention;
#   * verify phase: MERGED <-> VERIFYING_TARGET_BRANCH -> COMPLETED | attention;
#   * recovery: PR_CLOSED_UNMERGED/RECOVERY_PENDING -> PULL_REQUEST_OPEN |
#     RECOVERY_FAILED.
#
# A *same-state* update (retry-metadata / evidence enrichment — the bulk of
# `_transition` writes) is always permitted and is deliberately NOT encoded as a
# self-edge here; see `is_valid_completion_transition`. Terminal states map to
# an empty set: no ordinary advancement ever leaves them.
COMPLETION_TRANSITIONS: dict[str, frozenset[str]] = {
    CompletionState.EXECUTION_FINISHED: frozenset(
        {
            CompletionState.VALIDATING_RESULT,
            CompletionState.RESULT_VALID,
            CompletionState.VALIDATION_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.VALIDATING_RESULT: frozenset(
        {
            CompletionState.RESULT_VALID,
            CompletionState.VALIDATION_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.RESULT_VALID: frozenset(
        {
            CompletionState.AWAITING_REVIEW,
            CompletionState.PREPARING_PULL_REQUEST,
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.AWAITING_MERGE,
            CompletionState.MERGE_BLOCKED,
            CompletionState.MERGED,
            CompletionState.PR_CLOSED_UNMERGED,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    # The gate itself. It may proceed to the pull-request phase (approved),
    # terminate as REVIEW_REJECTED (the reviewer said no — the rework loop takes
    # it from there), reach COMPLETED if the change turns out to be in the target
    # branch already, or escalate. It may NOT go backwards to RESULT_VALID:
    # re-entering the pre-gate state would let a second pass skip the gate.
    CompletionState.AWAITING_REVIEW: frozenset(
        {
            CompletionState.PREPARING_PULL_REQUEST,
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.REVIEW_REJECTED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.PREPARING_PULL_REQUEST: frozenset(
        {
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.AWAITING_MERGE,
            CompletionState.MERGE_BLOCKED,
            CompletionState.MERGED,
            CompletionState.PR_CLOSED_UNMERGED,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.PULL_REQUEST_OPEN: frozenset(
        {
            CompletionState.AWAITING_MERGE,
            CompletionState.MERGE_BLOCKED,
            CompletionState.MERGED,
            CompletionState.PR_CLOSED_UNMERGED,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.AWAITING_MERGE: frozenset(
        {
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.MERGE_BLOCKED,
            CompletionState.MERGED,
            CompletionState.PR_CLOSED_UNMERGED,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.MERGE_BLOCKED: frozenset(
        {
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.AWAITING_MERGE,
            CompletionState.MERGED,
            CompletionState.PR_CLOSED_UNMERGED,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.MERGED: frozenset(
        {
            CompletionState.VERIFYING_TARGET_BRANCH,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.VERIFYING_TARGET_BRANCH: frozenset(
        {
            CompletionState.MERGED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.PR_CLOSED_UNMERGED: frozenset(
        {
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.RECOVERY_PENDING,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    CompletionState.RECOVERY_PENDING: frozenset(
        {
            CompletionState.PULL_REQUEST_OPEN,
            CompletionState.RECOVERY_FAILED,
            CompletionState.COMPLETED,
            CompletionState.REQUIRES_ATTENTION,
        }
    ),
    # Terminal — no outgoing transitions (defense in depth against reprocessing).
    CompletionState.COMPLETED: frozenset(),
    CompletionState.VALIDATION_FAILED: frozenset(),
    CompletionState.REVIEW_REJECTED: frozenset(),
    CompletionState.REQUIRES_ATTENTION: frozenset(),
    CompletionState.RECOVERY_FAILED: frozenset(),
}


def is_valid_completion_transition(current: str, new: str) -> bool:
    """Whether persisting completion-state `current` -> `new` is legal.

    A same-state update (`current == new`) is always allowed: it carries retry
    metadata (`next_retry_at`/`retry_count`) or refreshed evidence, not a
    lifecycle move, and is the common case for the poller's waiting/backoff
    writes. Otherwise `new` must appear in `COMPLETION_TRANSITIONS[current]`; an
    unrecognized `current` state permits no transition. Terminal states have an
    empty target set, so no ordinary advancement can move a row out of
    COMPLETED / VALIDATION_FAILED / REQUIRES_ATTENTION / RECOVERY_FAILED."""
    if current == new:
        return True
    return new in COMPLETION_TRANSITIONS.get(current, frozenset())


class CompletionAction:
    """What the orchestration layer should DO next to advance this run.
    `WAIT` means "no action; re-check later" (e.g. awaiting a human merge or
    pending CI). `NONE` means terminal — nothing further to do."""

    NONE = "none"
    RUN_VALIDATION = "run_validation"
    OPEN_PULL_REQUEST = "open_pull_request"
    WAIT = "wait"
    MERGE = "merge"
    VERIFY_TARGET = "verify_target"
    RECOVER_PULL_REQUEST = "recover_pull_request"


class ReasonCode:
    EXECUTION_OK = "EXECUTION_OK"
    WORKTREE_MISSING = "WORKTREE_MISSING"
    NOT_A_REPOSITORY = "NOT_A_REPOSITORY"
    UNCOMMITTED_CHANGES = "UNCOMMITTED_CHANGES"
    NO_TASK_COMMIT = "NO_TASK_COMMIT"
    VALIDATION_PENDING = "VALIDATION_PENDING"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    LOCAL_ONLY_COMPLETE = "LOCAL_ONLY_COMPLETE"
    PR_MISSING = "PR_MISSING"
    PR_OPEN = "PR_OPEN"
    AWAITING_MANUAL_MERGE = "AWAITING_MANUAL_MERGE"
    CHECKS_PENDING = "CHECKS_PENDING"
    CHECKS_FAILING = "CHECKS_FAILING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PR_CONFLICTING = "PR_CONFLICTING"
    READY_TO_MERGE = "READY_TO_MERGE"
    PR_MERGED = "PR_MERGED"
    TARGET_NOT_VERIFIED = "TARGET_NOT_VERIFIED"
    TARGET_VERIFIED = "TARGET_VERIFIED"
    PR_CLOSED_UNMERGED = "PR_CLOSED_UNMERGED"
    RECOVERY_DISABLED = "RECOVERY_DISABLED"
    RECOVERY_NOT_POSSIBLE = "RECOVERY_NOT_POSSIBLE"
    RECOVERABLE = "RECOVERABLE"
    NO_PR_NOT_IN_TARGET = "NO_PR_NOT_IN_TARGET"
    RETRY_LIMIT_REACHED = "RETRY_LIMIT_REACHED"
    PUBLICATION_FENCE_LOST = "PUBLICATION_FENCE_LOST"
    # Independent review gate (blocking, pre-pull-request).
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_REJECTED = "REVIEW_REJECTED"


# --------------------------------------------------------------------------
# Merge policy
# --------------------------------------------------------------------------

MERGE_MANUAL = "manual"
MERGE_AUTO_AFTER_CHECKS = "auto_after_checks"
MERGE_AUTO_AFTER_CHECKS_AND_REVIEW = "auto_after_checks_and_review"
MERGE_MODES = (MERGE_MANUAL, MERGE_AUTO_AFTER_CHECKS, MERGE_AUTO_AFTER_CHECKS_AND_REVIEW)

MERGE_METHOD_MERGE = "merge"
MERGE_METHOD_SQUASH = "squash"
MERGE_METHOD_REBASE = "rebase"
MERGE_METHODS = (MERGE_METHOD_MERGE, MERGE_METHOD_SQUASH, MERGE_METHOD_REBASE)

DEFAULT_MAX_RETRIES = 20


@dataclass
class CompletionPolicy:
    """Per-task completion policy, resolved from task/project config with
    conservative defaults. Defaults never auto-merge and never auto-recover —
    both require explicit opt-in."""

    requires_pull_request: bool = True
    allow_local_only: bool = False
    allow_pr_recovery: bool = False
    validation_required: bool = True
    # Blocking independent review before any pull request is opened. Off by
    # default like every other automation gate: it costs an extra agent run
    # per task, so it is a deliberate choice, never an implicit one.
    requires_independent_review: bool = False
    merge_mode: str = MERGE_MANUAL
    merge_method: str = MERGE_METHOD_SQUASH
    max_retries: int = DEFAULT_MAX_RETRIES
    # Optional persisted scheduler fence for privileged daily-audit
    # publication. Ordinary completion rows leave both fields unset.
    publication_fence_campaign_id: str | None = None
    publication_fence_owner: str | None = None

    def __post_init__(self) -> None:
        if self.merge_mode not in MERGE_MODES:
            self.merge_mode = MERGE_MANUAL
        if self.merge_method not in MERGE_METHODS:
            self.merge_method = MERGE_METHOD_SQUASH

    @property
    def is_auto_merge(self) -> bool:
        return self.merge_mode in (MERGE_AUTO_AFTER_CHECKS, MERGE_AUTO_AFTER_CHECKS_AND_REVIEW)

    @property
    def requires_review_for_merge(self) -> bool:
        return self.merge_mode == MERGE_AUTO_AFTER_CHECKS_AND_REVIEW

    def to_dict(self) -> dict:
        return {
            "requires_pull_request": self.requires_pull_request,
            "allow_local_only": self.allow_local_only,
            "allow_pr_recovery": self.allow_pr_recovery,
            "validation_required": self.validation_required,
            "requires_independent_review": self.requires_independent_review,
            "merge_mode": self.merge_mode,
            "merge_method": self.merge_method,
            "max_retries": self.max_retries,
            "publication_fence_campaign_id": self.publication_fence_campaign_id,
            "publication_fence_owner": self.publication_fence_owner,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "CompletionPolicy":
        data = data or {}
        return cls(
            requires_pull_request=bool(data.get("requires_pull_request", True)),
            allow_local_only=bool(data.get("allow_local_only", False)),
            allow_pr_recovery=bool(data.get("allow_pr_recovery", False)),
            validation_required=bool(data.get("validation_required", True)),
            requires_independent_review=bool(data.get("requires_independent_review", False)),
            merge_mode=str(data.get("merge_mode", MERGE_MANUAL)),
            merge_method=str(data.get("merge_method", MERGE_METHOD_SQUASH)),
            max_retries=int(data.get("max_retries", DEFAULT_MAX_RETRIES)),
            publication_fence_campaign_id=(
                str(data["publication_fence_campaign_id"])
                if data.get("publication_fence_campaign_id")
                else None
            ),
            publication_fence_owner=(
                str(data["publication_fence_owner"])
                if data.get("publication_fence_owner")
                else None
            ),
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "CompletionPolicy":
        if not raw:
            return cls()
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls()

    @classmethod
    def resolve(
        cls,
        *,
        task: dict | None = None,
        project_cfg: dict | None = None,
        overrides: dict | None = None,
    ) -> "CompletionPolicy":
        """Layer `overrides` over task config over project config over
        defaults. A later layer's key wins over an earlier one's; absent keys
        keep the default.

        `overrides` is the operator layer: a machine-wide, explicitly persisted
        opt-in (see `command_center.pipeline_settings`) that the desktop
        pipeline applies when it seeds a completion row, so enabling
        auto-merge once does not require editing every task. It is the highest
        layer precisely because it is the most explicit — a human toggled it
        this session — but it still only chooses a *policy*: whether a merge
        actually happens remains the evaluator's decision, gated on checks,
        review and mergeability."""
        merged: dict[str, Any] = {}
        for source in (project_cfg or {}, task or {}, overrides or {}):
            for key in (
                "requires_pull_request",
                "allow_local_only",
                "allow_pr_recovery",
                "validation_required",
                "requires_independent_review",
                "merge_mode",
                "merge_method",
                "max_retries",
            ):
                if key in source and source[key] is not None:
                    merged[key] = source[key]
        # Publication fencing is a privileged completion capability: task
        # packages and project config are data inputs, so they must never be
        # able to claim or renew a daily-audit lease. Only the explicit trusted
        # operator/backend override channel may persist these two values.
        for key in (
            "publication_fence_campaign_id",
            "publication_fence_owner",
        ):
            if key in (overrides or {}) and overrides[key] is not None:
                merged[key] = overrides[key]
        return cls.from_dict(merged)


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------

@dataclass
class CompletionAssessment:
    """Structured verdict of the evaluator. Never a bare boolean."""

    status: str
    action: str
    reason_code: str
    recommended_action: str
    is_complete: bool = False
    is_recoverable: bool = False
    requires_human: bool = False
    evidence: dict = field(default_factory=dict)
    missing_requirements: list[str] = field(default_factory=list)
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    pull_request_state: str | None = None
    merge_commit: str | None = None


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------

class CompletionEvaluator:
    """Pure evaluator: full evidence snapshot -> `CompletionAssessment`.

    `repository_state` is a `runtime.repo_state.RepositoryState`,
    `pull_request` a `runtime.github.PullRequestState | None`, `validation` a
    `runtime.validation.ValidationOutcome | None` (None = not yet run). The
    evaluator never performs I/O — it only interprets what it is given."""

    def evaluate(
        self,
        task: dict | None,
        run: dict | None,
        repository_state,
        pull_request=None,
        *,
        validation=None,
        policy: CompletionPolicy | None = None,
        enforce_clean_tree: bool = True,
    ) -> CompletionAssessment:
        policy = policy or CompletionPolicy()
        rs = repository_state
        evidence: dict[str, Any] = {
            "execution_completed": bool(run and run.get("state") == "COMPLETED"),
            "worktree_exists": bool(getattr(rs, "worktree_exists", False)),
            "is_repo": bool(getattr(rs, "is_repo", False)),
            "clean_worktree": not bool(getattr(rs, "dirty", False)),
            "has_task_commit": bool(getattr(rs, "has_task_commit", False)),
            "head_is_base_tip": bool(getattr(rs, "head_is_base_tip", False)),
            "commit_in_target": bool(getattr(rs, "commit_in_target", False)),
            "remote_branch_exists": bool(getattr(rs, "remote_branch_exists", False)),
            "head_commit": getattr(rs, "head_commit", None),
        }

        pr = pull_request
        if pr is not None:
            evidence["pull_request_number"] = pr.number
            evidence["pull_request_state"] = pr.state
            evidence["pr_merged"] = pr.is_merged
            evidence["pr_closed_unmerged"] = pr.is_closed_unmerged

        # 1. Repository reachability -------------------------------------------------
        if not evidence["worktree_exists"]:
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.WORKTREE_MISSING,
                recommended_action="The run's worktree is missing; cannot verify the task result. "
                "Restore the worktree or re-run the task.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["worktree"],
            )
        if not evidence["is_repo"]:
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.NOT_A_REPOSITORY,
                recommended_action="The worktree is not a readable git repository.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["repository"],
            )

        # 2. Uncommitted changes — the agent did not finish cleanly. Enforced
        #    only before validation: validation/verification themselves may
        #    leave build artifacts (__pycache__, .pytest_cache) in the tree, so
        #    later phases pass enforce_clean_tree=False.
        if enforce_clean_tree and not evidence["clean_worktree"]:
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.UNCOMMITTED_CHANGES,
                recommended_action="Worktree has uncommitted changes. Review and commit or discard them, "
                "then re-run.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["clean_worktree"],
            )

        # 3. No task commit — HEAD is exactly the base tip, so the run produced
        #    no committed work at all (distinct from a merged change, whose HEAD
        #    differs from the base tip while being reachable from it).
        if evidence["head_is_base_tip"]:
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.NO_TASK_COMMIT,
                recommended_action="No task-related commit exists relative to the base branch. "
                "The agent produced no committed work.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["task_commit"],
            )

        # 4. Already present in the target branch — the definitive "done" signal.
        #    Covers squash merges (merge commit reachable) and a change already
        #    in the target despite a closed PR (recovery no longer needed).
        if evidence["commit_in_target"]:
            return CompletionAssessment(
                status=CompletionState.COMPLETED,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.TARGET_VERIFIED,
                recommended_action="Task change is present in the target branch. Completed.",
                is_complete=True,
                evidence=evidence,
                pull_request_number=getattr(pr, "number", None),
                pull_request_url=getattr(pr, "url", None),
                pull_request_state=getattr(pr, "state", None),
                merge_commit=getattr(pr, "merge_commit", None),
            )

        # 5. Validation ---------------------------------------------------------------
        if policy.validation_required:
            if validation is None or not getattr(validation, "ran", False):
                return CompletionAssessment(
                    status=CompletionState.VALIDATING_RESULT,
                    action=CompletionAction.RUN_VALIDATION,
                    reason_code=ReasonCode.VALIDATION_PENDING,
                    recommended_action="Run the configured validation plan.",
                    evidence=evidence,
                    missing_requirements=["validation"],
                )
            evidence["validation_passed"] = bool(validation.passed)
            evidence["validation_summary"] = validation.summary()
            if not validation.passed:
                return CompletionAssessment(
                    status=CompletionState.VALIDATION_FAILED,
                    action=CompletionAction.NONE,
                    reason_code=ReasonCode.VALIDATION_FAILED,
                    recommended_action="Validation failed: "
                    + validation.summary()
                    + ". Fix the issues and re-run the task.",
                    requires_human=True,
                    evidence=evidence,
                    missing_requirements=["validation"],
                )

        # 6. No-PR / local-only completion -------------------------------------------
        if not policy.requires_pull_request:
            if policy.allow_local_only:
                return CompletionAssessment(
                    status=CompletionState.COMPLETED,
                    action=CompletionAction.NONE,
                    reason_code=ReasonCode.LOCAL_ONLY_COMPLETE,
                    recommended_action="Local-only task validated with a committed change. Completed.",
                    is_complete=True,
                    evidence=evidence,
                )
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.NO_PR_NOT_IN_TARGET,
                recommended_action="Task requires no pull request but its change is not in the target "
                "branch and local-only completion is not enabled.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["target_branch"],
            )

        # 7. Pull request required ----------------------------------------------------
        if pr is None:
            return CompletionAssessment(
                status=CompletionState.PREPARING_PULL_REQUEST,
                action=CompletionAction.OPEN_PULL_REQUEST,
                reason_code=ReasonCode.PR_MISSING,
                recommended_action="Push the branch and open a pull request.",
                evidence=evidence,
                missing_requirements=["pull_request"],
            )

        # 8. PR exists — branch on its true state ------------------------------------
        if pr.is_merged:
            return CompletionAssessment(
                status=CompletionState.MERGED,
                action=CompletionAction.VERIFY_TARGET,
                reason_code=ReasonCode.PR_MERGED,
                recommended_action="Pull request is merged. Verify the merge is present in the target branch.",
                evidence=evidence,
                pull_request_number=pr.number,
                pull_request_url=pr.url,
                pull_request_state=pr.state,
                merge_commit=pr.merge_commit,
            )

        if pr.is_closed_unmerged:
            return self._assess_closed_unmerged(pr, rs, policy, evidence)

        # PR is open.
        return self._assess_open_pr(pr, policy, evidence)

    # -- open-PR sub-decision ---------------------------------------------------------
    def _assess_open_pr(self, pr, policy: CompletionPolicy, evidence: dict) -> CompletionAssessment:
        evidence["checks_state"] = pr.checks_state
        evidence["review_decision"] = pr.review_decision
        evidence["mergeable"] = pr.mergeable

        common = dict(
            pull_request_number=pr.number,
            pull_request_url=pr.url,
            pull_request_state=pr.state,
        )

        # Manual mode: never auto-merge; track and wait for a human.
        if not policy.is_auto_merge:
            return CompletionAssessment(
                status=CompletionState.AWAITING_MERGE,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.AWAITING_MANUAL_MERGE,
                recommended_action="Pull request is open; awaiting manual merge (merge_mode=manual).",
                evidence=evidence,
                **common,
            )

        if pr.checks_failing:
            return CompletionAssessment(
                status=CompletionState.MERGE_BLOCKED,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.CHECKS_FAILING,
                recommended_action="Required checks are failing; will not merge. Fix CI, or merge manually.",
                evidence=evidence,
                **common,
            )
        if pr.checks_pending:
            return CompletionAssessment(
                status=CompletionState.AWAITING_MERGE,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.CHECKS_PENDING,
                recommended_action="Waiting for required checks to complete before merging.",
                evidence=evidence,
                **common,
            )
        # Nothing verified this change (audit M7). `checks_state` is NONE — the
        # repo has no CI — *and* local validation was not required, so an
        # auto-merge here would rest on no evidence at all. When validation ran —
        # the normal case — NONE checks are fine: this phase is only reached after
        # validation passed, so validation is the "successful check".
        if not pr.checks_passing and not policy.validation_required:
            return CompletionAssessment(
                status=CompletionState.MERGE_BLOCKED,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.CHECKS_FAILING,
                recommended_action=(
                    "No CI checks and no local validation verified this change; "
                    "will not auto-merge. Merge manually."
                ),
                requires_human=True,
                evidence=evidence,
                **common,
            )
        # A required review must be an *explicit* approval (audit M7): a missing
        # review (review_decision is None) is not the same as an approved one, so
        # `auto_after_checks_and_review` must not merge a PR nobody reviewed.
        if policy.requires_review_for_merge and pr.review_decision != "APPROVED":
            return CompletionAssessment(
                status=CompletionState.MERGE_BLOCKED,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.REVIEW_REQUIRED,
                recommended_action="Required review is not an explicit approval; will not merge without it.",
                evidence=evidence,
                **common,
            )
        if pr.mergeable == "CONFLICTING":
            return CompletionAssessment(
                status=CompletionState.MERGE_BLOCKED,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.PR_CONFLICTING,
                recommended_action="Pull request has conflicts and is not mergeable. Resolve conflicts.",
                requires_human=True,
                evidence=evidence,
                **common,
            )
        # Mergeability not yet confirmed (audit M8): GitHub returns UNKNOWN
        # transiently on a fresh PR. Wait and re-check rather than attempting a
        # merge we cannot prove is safe; a later tick sees MERGEABLE and proceeds.
        if pr.mergeable != "MERGEABLE":
            return CompletionAssessment(
                status=CompletionState.AWAITING_MERGE,
                action=CompletionAction.WAIT,
                reason_code=ReasonCode.CHECKS_PENDING,
                recommended_action="Mergeability is not yet confirmed (UNKNOWN); waiting to re-check before merging.",
                evidence=evidence,
                **common,
            )
        # Cleared to merge automatically.
        return CompletionAssessment(
            status=CompletionState.AWAITING_MERGE,
            action=CompletionAction.MERGE,
            reason_code=ReasonCode.READY_TO_MERGE,
            recommended_action=f"Checks satisfied; merging via {policy.merge_method}.",
            evidence=evidence,
            **common,
        )

    # -- closed-unmerged sub-decision -------------------------------------------------
    def _assess_closed_unmerged(self, pr, rs, policy: CompletionPolicy, evidence: dict) -> CompletionAssessment:
        common = dict(
            pull_request_number=pr.number,
            pull_request_url=pr.url,
            pull_request_state=pr.state,
        )
        local_work_intact = bool(getattr(rs, "has_task_commit", False))
        if not local_work_intact:
            # Local branch/commit is gone AND it never reached target (checked
            # earlier) — a human must decide.
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.RECOVERY_NOT_POSSIBLE,
                recommended_action="Pull request was closed without merging and the local branch no longer "
                "contains the work. Manual recovery required.",
                requires_human=True,
                evidence=evidence,
                missing_requirements=["task_commit", "pull_request"],
                **common,
            )
        if not policy.allow_pr_recovery:
            return CompletionAssessment(
                status=CompletionState.REQUIRES_ATTENTION,
                action=CompletionAction.NONE,
                reason_code=ReasonCode.RECOVERY_DISABLED,
                recommended_action="Pull request was closed without merging. Automatic recovery is disabled "
                "(set allow_pr_recovery=true to auto-recreate the branch and PR, or recover manually).",
                is_recoverable=True,
                requires_human=True,
                evidence=evidence,
                **common,
            )
        return CompletionAssessment(
            status=CompletionState.PR_CLOSED_UNMERGED,
            action=CompletionAction.RECOVER_PULL_REQUEST,
            reason_code=ReasonCode.PR_CLOSED_UNMERGED,
            recommended_action="Pull request was closed without merging; recreating the remote branch and "
            "opening a replacement pull request.",
            is_recoverable=True,
            evidence=evidence,
            **common,
        )
