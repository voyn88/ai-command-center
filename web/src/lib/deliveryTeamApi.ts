export type TeamAssignment = {
  stage: string; role: string; status: string; blocker: string | null
  next_action: string; due_at: string | null; updated_at: string
}
export type DeliveryTeamSnapshot = {
  source: 'canonical_postgresql'; read_only: true; enabled: boolean; stopped: boolean
  available: boolean; reason: string | null; updated_at: string; limit: number; offset: number
  total: number | null
  counts: { production_accepted: number; blocked: number; sha_recorded: number; merged: null } | null
  roles: { stage: string; name: string }[]
  tasks: { id: string; title: string; project: string; revision: number; backlog_status: string
    merged: null; production_accepted: boolean; assignments: TeamAssignment[] }[]
}

export async function fetchDeliveryTeam(offset = 0, signal?: AbortSignal): Promise<DeliveryTeamSnapshot> {
  const response = await fetch(`/api/team?limit=30&offset=${offset}`, { signal, cache: 'no-store' })
  if (!response.ok) throw new Error('Данные команды временно недоступны')
  return response.json()
}
