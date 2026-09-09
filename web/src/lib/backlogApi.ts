// Typed client for the Postgres-backed autonomous delivery backlog
// (VOYN-W0-APP-CONTROL-S6a/S6c/S6d):
//   GET  /api/v1/backlog/status
//   GET  /api/v1/backlog/tasks
//   POST /api/v1/backlog/intake/draft
//   POST /api/v1/backlog/intake/confirm
//   POST /api/v1/backlog/tasks/{task_id}/reassign
//
// Types mirror `command_center/api/backlog_schemas.py`. Reads are
// unauthenticated at the server (same precedent as `backlog_routes.py`'s own
// docstring); the two intake writes and reassign ride the same platform
// credential as `queueApi.ts` (`Authorization: Bearer <token>`), so this
// module reuses `getOwnerToken`/`QueueAuthError` from there rather than
// inventing a second credential concept for the same boundary.

import { getOwnerToken, QueueAuthError } from './queueApi'

export type BacklogTask = {
  task_id: string
  wave: string
  priority: string | null
  status: string
  kind: string
  title: string
  repo: string | null
  revision: number
}

export type BacklogStatusCounts = {
  counts: Record<string, number>
  total: number
}

export function fetchBacklogStatus(): Promise<BacklogStatusCounts> {
  return fetch('/api/v1/backlog/status').then((response) => {
    if (!response.ok) throw new Error(`GET /api/v1/backlog/status -> ${response.status}`)
    return response.json() as Promise<BacklogStatusCounts>
  })
}

export function fetchBacklogTasks(limit = 200): Promise<{ tasks: BacklogTask[]; total: number }> {
  return fetch(`/api/v1/backlog/tasks?limit=${limit}`).then((response) => {
    if (!response.ok) throw new Error(`GET /api/v1/backlog/tasks -> ${response.status}`)
    return response.json() as Promise<{ tasks: BacklogTask[]; total: number }>
  })
}

export type BacklogEvent = {
  event: string
  outcome: string
  reason: string | null
  actor: string
  detail: Record<string, unknown> | null
  created_at: string
}

export type BacklogEvidence = {
  kind: string
  value: string
  recorded_at: string
}

export type BacklogTaskDetail = {
  task: BacklogTask
  events: BacklogEvent[]
  evidence: BacklogEvidence[]
}

/** The decomposition/progress trail for one backlog task (S6c drill-down):
 * every transition it has been through, plus recorded evidence. */
export function fetchBacklogTaskDetail(taskId: string): Promise<BacklogTaskDetail> {
  const path = `/api/v1/backlog/tasks/${encodeURIComponent(taskId)}`
  return fetch(path).then((response) => {
    if (!response.ok) throw new Error(`GET ${path} -> ${response.status}`)
    return response.json() as Promise<BacklogTaskDetail>
  })
}

export type DraftedTask = {
  task_id: string
  wave: string
  priority: string | null
  status: string
  kind: string
  title: string
  body: string
  repo: string | null
}

/** What the server heard, what it wrote, and every term it repaired — voice
 * intake only (S6b); null for typed text, which is never repaired. */
export type Transcript = {
  heard: string
  text: string
  corrections: { heard: string; written: string }[]
}

export type DraftResult =
  | { ok: true; line: string; task: DraftedTask; transcript: Transcript | null }
  | { ok: false; reason: string; raw_output: string; transcript: Transcript | null }

async function authedPost<T>(path: string, body: unknown): Promise<T> {
  const token = getOwnerToken()
  if (!token) throw new QueueAuthError()
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  })
  if (response.status === 401) throw new QueueAuthError()
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const reason = detail && typeof detail.detail === 'string' ? detail.detail : response.status
    throw new Error(`POST ${path} -> ${reason}`)
  }
  return (await response.json()) as T
}

/** Step 1 of chat/voice intake (S6a/S6b): send free text, get back a
 * proposed backlog line for the owner to read (and, if needed, edit) before
 * anything is written.
 *
 * `source: 'voice'` marks the text as a machine transcript, which is the
 * server's cue to repair dictated domain terms (`voice_transcript.py`) before
 * the model sees them and to report what it changed. Typed text is sent
 * as-is — a repair table must never edit words the owner actually typed. */
export function draftBacklogTask(
  text: string,
  source: 'chat' | 'voice' = 'chat',
): Promise<DraftResult> {
  return authedPost('/api/v1/backlog/intake/draft', { text, source })
}

/** Step 2: the (possibly edited) line is re-parsed from scratch on the
 * server and inserted as a new task. */
export function confirmBacklogTask(line: string): Promise<{ task_id: string; reason: string; changed: boolean }> {
  return authedPost('/api/v1/backlog/intake/confirm', { line })
}

/** S6d: reprioritize an existing task (optimistic revision — the caller must
 * supply the revision it last read). */
export function reassignBacklogTask(
  taskId: string,
  body: { wave: string; priority: string | null; expected_revision: number },
): Promise<{ task_id: string; reason: string; revision: number }> {
  return authedPost(`/api/v1/backlog/tasks/${encodeURIComponent(taskId)}/reassign`, body)
}
