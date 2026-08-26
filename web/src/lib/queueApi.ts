// Typed client for the owner-gated server queue API (VOYN-W0-APP-CONTROL-S1):
//   GET  /api/v1/queue/items
//   GET  /api/v1/queue/items/{id}
//
// Types mirror the REAL output of
// `command_center/db/work_queue_read.py` (the columns of the
// `work_item_public` / `work_attempt_public` views plus the loaded
// `work_result.payload`), the same derivation rule `api.ts` follows for the
// home routes.
//
// Auth: every request carries `Authorization: Bearer <platform credential>` —
// the same identity model as the rest of the HTTP boundary (#316): the
// platform says WHO via whoami, AICC's grant map says WHAT. There is no
// separate "owner token".
//
// Storage decision (recorded per acceptance of #325): sessionStorage, not
// localStorage. The credential is a live platform token; localStorage would
// persist it across sessions and hand any later XSS a durable credential,
// while a cookie would need server-side session plumbing this slice has no
// authority to add. sessionStorage bounds the exposure to one tab's
// lifetime at the cost of re-entering the key per tab — acceptable for a
// single-owner tool. A 401 raises `QueueAuthError` so the screen can
// distinguish "locked" from "broken".

export type QueueItem = {
  work_item_id: string
  queue: string
  idempotency_key: string
  task_id: string | null
  repository_id: string | null
  priority: number
  available_at: string | null
  state: 'ready' | 'claimed' | 'succeeded' | 'dead' | string
  attempt_count: number
  max_attempts: number
  current_attempt_id: string | null
  result_id: string | null
  dead_reason: string | null
  dead_at: string | null
  created_at: string | null
  updated_at: string | null
}

export type QueueAttempt = {
  attempt_id: string
  work_item_id: string
  attempt_no: number
  claimed_by_role: string | null
  visibility_seconds: number
  visible_until: string | null
  heartbeat_at: string | null
  state: string
  outcome_reason: string | null
  result_id: string | null
  created_at: string | null
  updated_at: string | null
}

export type QueueItemDetail = QueueItem & {
  attempts: QueueAttempt[]
  // agent_run results carry status/result_text/stdout_tail etc.; other kinds
  // may differ, so the type stays open.
  result: Record<string, unknown> | null
}

const TOKEN_KEY = 'aicc_platform_token'

export class QueueAuthError extends Error {
  constructor() {
    super('owner token missing or rejected')
    this.name = 'QueueAuthError'
  }
}

export function getOwnerToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setOwnerToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token)
  } catch {
    // Private-mode storage failure: the session simply re-asks next visit.
  }
}

async function request<T>(path: string): Promise<T> {
  const token = getOwnerToken()
  if (!token) throw new QueueAuthError()
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (response.status === 401) throw new QueueAuthError()
  if (!response.ok) throw new Error(`GET ${path} -> ${response.status}`)
  return (await response.json()) as T
}

export function fetchQueueItems(state?: string): Promise<{ items: QueueItem[] }> {
  const query = state && state !== 'ALL' ? `?state=${encodeURIComponent(state)}` : ''
  return request(`/api/v1/queue/items${query}`)
}

export function fetchQueueItem(workItemId: string): Promise<QueueItemDetail> {
  return request(`/api/v1/queue/items/${encodeURIComponent(workItemId)}`)
}
