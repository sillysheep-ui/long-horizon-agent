import type { Trip, JobEvent } from './types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `请求失败 (${response.status})`)
  }
  return response.json()
}

export const createTrip = (query: string) => request<{trip_id: string; job_id: string}>('/api/trips', { method: 'POST', body: JSON.stringify({ query }) })
export const reviseTrip = (id: string, instruction: string) => request<{trip_id: string; job_id: string}>(`/api/trips/${id}/revisions`, { method: 'POST', body: JSON.stringify({ instruction }) })
export const getTrip = (id: string) => request<Trip>(`/api/trips/${id}`)
export const getEvents = (id: string, after = 0) => request<JobEvent[]>(`/api/jobs/${id}/events/list?after=${after}`)
