// Typed client for GET /api/home.
//
// These types are derived from the REAL output of
// `command_center/webapi/serializers.py::serialize_home` (Task 1), not from
// a guessed shape — see that file's docstring/comments for the redaction
// rules that produce this exact structure. Where this disagrees with the
// original task brief's guessed `api.ts`, the real serializer wins; the
// divergences are called out per-type below.

export type Kpi = {
  value: number
  meta_key: string
  // Always present in the real serializer output (every kpi entry sets it,
  // including a hardcoded 0 for `tasks.meta_n`), but kept optional here to
  // stay defensive against future/older payloads.
  meta_n?: number
}

// Diverges from the brief's guess: `health` is an OBJECT
// `{task_count, active_run_count, repository_state}`, not a `number`.
// `_serialize_projects` in serializers.py builds this object per
// non-sensitive project; sensitive/redacted projects omit it entirely.
export type ProjectHealth = {
  task_count: number
  active_run_count: number
  repository_state: string | null
}

export type Project = {
  id: string
  name: string
  healthy: boolean
  // Present (true) only for sensitive/redacted projects (e.g. BANK/LEGAL);
  // when present, `health` is always omitted.
  redacted?: boolean
  // Present only for non-redacted projects.
  health?: ProjectHealth
}

export type QueueItem = {
  title: string
  project: string
  // No progress signal exists anywhere upstream — always 0 today
  // (serializers.py `_serialize_queue`), kept as `number` for forward compat.
  progress: number
  state: string
}

export type StatusItem = {
  project: string
  repository_state: string | null
}

// Diverges from the brief's guess: real `activity` entries are NOT
// `{text, ago}`. `serialize_home` passes `recent_activity` through
// unmodified; those entries come from two different upstream shapes merged
// together (`workspace_home._normalize_activity_entry` for real
// activity-log events, and `_derive_activity_from_v2_runs` for synthesized
// v2 run lifecycle events), so most fields are optional/nullable depending
// on which source produced the entry.
export type ActivityItem = {
  event_type?: string
  // Raw activity-log events also carry the original `type` field verbatim
  // (event_type is copied from it); derived v2-run entries only set
  // event_type.
  type?: string
  ts?: string
  project?: string | null
  task_id?: string | null
  run_id?: string | null
  conversation_id?: string | null
  message?: string
  id?: string
}

export type HomeDTO = {
  projects: Project[]
  kpis: { projects: Kpi; agents: Kpi; tasks: Kpi; reviews: Kpi }
  queue: QueueItem[]
  // Top-level workspace health rollup — `{projects_healthy, projects_total}`
  // (serializers.py `_health`). Not to be confused with `Project.health`.
  health: { projects_healthy: number; projects_total: number }
  activity: ActivityItem[]
  // `{reports_count, artifacts_count, recent_activity_count}`
  // (serializers.py `_overview`).
  overview: { reports_count: number; artifacts_count: number; recent_activity_count: number }
  status: StatusItem[]
}

export async function fetchHome(): Promise<HomeDTO> {
  const r = await fetch('/api/home')
  if (!r.ok) throw new Error('home failed')
  return r.json()
}

// The canonical provenance read model (`run_lineage.build_view`, mirrored
// verbatim by `serialize_execution`): who initiated the run, the exact
// prompt/model/actions that produced it, and a reproducibility hash over
// the inputs a reproduction attempt must match. Any field can be `null` —
// evidence that hasn't been observed yet is never guessed, and its name
// shows up in `unknown_fields` instead.
export type RunProvenance = {
  initiated_by: string | null
  prompt: string | null
  prompt_version: number | null
  model: string | null
  actions: string[] | null
  reproducibility_hash: string | null
  unknown_fields: string[]
}

export type ExecutionRun = {
  id: string
  source: string
  title: string
  project: string
  project_name: string
  task_type: string
  state: string
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  duration_seconds: number | null
  exit_code: number | null
  failure_reason: string | null
  verdict: string | null
  provenance: RunProvenance | null
}

export type ExecutionDTO = {
  summary: { visible_runs: number; active: number; completed: number; needs_attention: number }
  state_counts: Record<string, number>
  runs: ExecutionRun[]
}

export async function fetchExecution(): Promise<ExecutionDTO> {
  const r = await fetch('/api/execution')
  if (!r.ok) throw new Error('execution failed')
  return r.json()
}
