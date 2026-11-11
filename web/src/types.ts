export type Evidence = { id: string; kind: string; query: string; city: string; status: string; reason?: string; source?: string; source_kind?: string; verification_level?: string; fetched_at?: string; simulated?: boolean; reused?: boolean; items: { name: string; address: string; url?: string; snippet?: string }[] }
export type Activity = { title: string; description: string; evidence_id: string }
export type Result = { status: string; summary?: string; question?: string; days: { day: number; city: string; activities: Activity[] }[]; notes?: string[]; evidence: Evidence[]; model_calls?: { role: string; provider: string; model: string }[]; elapsed_seconds?: number }
export type Job = { id: string; version: number; status: string; error?: string | null; created_at: string }
export type Trip = { id: string; query: string; version: number; result: Result | null; jobs: Job[] }
export type JobEvent = { id: number; kind: string; payload: { stage?: string; message?: string; [key: string]: unknown }; created_at: string }
