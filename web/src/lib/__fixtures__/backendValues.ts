// Values the BACKEND can emit to the web UI. Keep in sync with the Python
// sources named next to each list; lib/__tests__/labels.test.ts fails when a
// value here has no human label.

/** command_center/runtime/db/core.py RUN_STATES (also serializers.py state_counts). */
export const RUN_STATES = ['PREPARED', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'UNKNOWN']

/** command_center/db/work_queue_read.py _STATES (work_item.state, /api/v1/queue/items). */
export const QUEUE_STATES = ['ready', 'claimed', 'succeeded', 'dead']

/** command_center/db/sql/0002_queue_claim.up.sql work_attempt_state_valid. */
export const ATTEMPT_STATES = ['active', 'succeeded', 'failed', 'expired']

/** command_center/webapi/serializers.py _UNHEALTHY_REPOSITORY_STATES + workspace_home._discover_worktrees. */
export const REPOSITORY_STATES = ['ok', 'unconfigured', 'invalid_path', 'not_git_repo']

/** command_center/models.py VERDICT_LABELS. */
export const VERDICTS = [
  'APPROVED_FOR_COMMIT',
  'NOT_APPROVED_FOR_COMMIT',
  'READY_FOR_FINAL_REVIEW',
  'NOT_READY_FOR_FINAL_REVIEW',
  'READY_FOR_COMMIT',
  'FAILED',
]

/** command_center/webapi/serializers.py _kpis meta_key values. */
export const KPI_META_KEYS = ['all_healthy', 'running', 'in_progress', 'pending']

/**
 * Exact reason codes: runtime/supervisor.py, runtime/providers.py
 * (PROVIDER_UNAVAILABLE_REASONS), db/sql/0002_queue_claim.up.sql
 * (dead_reason / outcome_reason).
 */
export const REASON_CODES = [
  'timeout',
  'supervision_failed',
  'launch_preparation_failed',
  'launch_setup_failed',
  'executable_missing',
  'provider_launch_failed',
  'session_expired',
  'authentication_failed',
  'quota_limit',
  'provider_api_error',
  'provider_exit_nonzero',
  'daemon_unreachable',
  'model_missing',
  'insufficient_memory',
  'visibility_timeout',
  'visibility_timeout_exhausted',
  'attempt_budget_exhausted',
]

/** Reasons with a free-text suffix: capabilities.py FAILURE_REASON_PREFIX, db/sql/0022/0024/0028 (dead_reason), runtime/outcome.py. */
export const REASON_EXAMPLES_WITH_SUFFIX = [
  'capability_mismatch:web,git',
  'lease_wait_exhausted: lock held',
  'infra_wait_exhausted: no capacity',
  'blocked: denied',
  'incomplete: no report',
]

/** command_center/db/sql/0010_review_cycle_remediation.up.sql backlog_task_status_vocabulary. */
export const BACKLOG_STATUSES = [
  'OPEN',
  'IN_PROGRESS',
  'READY_TO_REVIEW',
  'DONE',
  'REJECTED',
  'UNTRIAGED',
  'DEFER_TO_USER',
  'SPLIT',
  'NEEDS_REFINEMENT',
  'DECIDED',
]
