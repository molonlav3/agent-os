/**
 * Update preflight: before `updates.apply` (web) or the engine updater
 * (desktop) restarts the gateway, say what the restart would interrupt.
 *
 * - running sessions (`sessions.list`, a queued or running task),
 * - scheduled jobs due in the next 10 minutes (`cron.list` next_run),
 * - pending approvals (the approval monitor's store).
 *
 * A pending approval blocks the update with a reason: the restart would drop
 * it and the agent waiting on it. Running sessions and due jobs only warn;
 * the operator confirms and the update goes ahead.
 *
 * The evaluation is pure; `useUpdatePreflight` runs the two reads when a
 * caller opens its dialog (never on a timer) and keeps approvals live.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRpc } from '@/app/providers'
import { useApprovals } from '@/services/approval-monitor'
import { gate, requireNoPendingApprovals, type Gate } from '@/lib/disabled-reason'
import { t } from '@/i18n'

export const DUE_WINDOW_MS = 10 * 60_000

interface TaskLike {
  status?: string
}

/** The fields of a `sessions.list` row the preflight reads. */
export interface PreflightSession {
  key?: string
  active_task?: TaskLike | null
  activeTask?: TaskLike | null
  run_status?: string
  runStatus?: string
  [key: string]: unknown
}

/** The fields of a `cron.list` row the preflight reads. */
export interface PreflightJob {
  id?: string
  name?: string
  enabled?: boolean
  status?: string
  next_run?: string | number | null
  [key: string]: unknown
}

export interface DueJob {
  id: string
  name: string
  nextRunAt: number
}

export interface UpdatePreflight {
  runningSessions: string[]
  dueJobs: DueJob[]
  pendingApprovals: number
  /** Blocked (pending approvals) or open. */
  gate: Gate
  /** Something would be interrupted: confirm before applying. */
  warn: boolean
}

const ACTIVE = new Set(['queued', 'running'])

export function isSessionRunning(row: PreflightSession): boolean {
  const active = row.active_task ?? row.activeTask ?? null
  const activeStatus = String(active?.status ?? '').toLowerCase()
  if (ACTIVE.has(activeStatus)) return true
  return ACTIVE.has(String(row.run_status ?? row.runStatus ?? '').toLowerCase())
}

/** Epoch ms of a job's next run, or null. Accepts ISO strings and epoch numbers. */
export function nextRunAt(job: PreflightJob): number | null {
  const raw = job.next_run
  if (raw === null || raw === undefined || raw === '') return null
  const value = typeof raw === 'number' && raw < 1e12 ? raw * 1000 : raw
  const ts = new Date(value).getTime()
  return Number.isNaN(ts) ? null : ts
}

export function isJobDueSoon(job: PreflightJob, now: number, windowMs = DUE_WINDOW_MS): boolean {
  if (!job.enabled) return false
  if (job.status === 'running') return true
  const at = nextRunAt(job)
  return at !== null && at >= now && at <= now + windowMs
}

export function evaluateUpdatePreflight(input: {
  sessions: readonly PreflightSession[]
  jobs: readonly PreflightJob[]
  pendingApprovals: number
  now: number
}): UpdatePreflight {
  const runningSessions = input.sessions
    .filter(isSessionRunning)
    .map((s) => String(s.key ?? ''))
    .filter(Boolean)
  const dueJobs = input.jobs
    .filter((j) => isJobDueSoon(j, input.now))
    .map((j) => ({
      id: String(j.id ?? ''),
      name: String(j.name || j.id || ''),
      nextRunAt: nextRunAt(j) ?? input.now,
    }))
  return {
    runningSessions,
    dueJobs,
    pendingApprovals: input.pendingApprovals,
    gate: gate(requireNoPendingApprovals(input.pendingApprovals)),
    warn: runningSessions.length > 0 || dueJobs.length > 0,
  }
}

function rows<T>(raw: unknown, key: string): T[] {
  if (Array.isArray(raw)) return raw as T[]
  const list = (raw as Record<string, unknown> | null)?.[key]
  return Array.isArray(list) ? (list as T[]) : []
}

export interface PreflightState {
  preflight: UpdatePreflight | null
  loading: boolean
  error: string | null
  /** What the Apply button should do: checking, blocked, or open. */
  gate: Gate
  /** Read sessions and jobs now; call it when the confirm dialog opens. */
  run(): void
}

/**
 * The preflight reads, run on demand: `run()` from the handler that opens
 * the confirm dialog (and from its "check again"). Sessions and jobs are a
 * read per run; approvals stay live from the monitor's store, so answering
 * the last one unblocks the dialog without another read.
 */
export function useUpdatePreflight(): PreflightState {
  const rpc = useRpc()
  const pendingApprovals = useApprovals((s) => s.pending.length)
  const [reads, setReads] = useState<{
    sessions: PreflightSession[]
    jobs: PreflightJob[]
    at: number
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const runId = useRef(0)

  const run = useCallback(() => {
    const id = ++runId.current
    setLoading(true)
    setError(null)
    Promise.all([rpc.call('sessions.list', { limit: 200 }), rpc.call('cron.list', {})])
      .then(([sessions, jobs]) => {
        if (id !== runId.current) return
        setReads({
          sessions: rows<PreflightSession>(sessions, 'sessions'),
          jobs: rows<PreflightJob>(jobs, 'jobs'),
          at: Date.now(),
        })
      })
      .catch((err: unknown) => {
        if (id === runId.current) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (id === runId.current) setLoading(false)
      })
  }, [rpc])

  // A late answer from a dialog that has since unmounted must not land.
  useEffect(
    () => () => {
      runId.current += 1
    },
    [],
  )

  const preflight = useMemo(
    () =>
      reads
        ? evaluateUpdatePreflight({
            sessions: reads.sessions,
            jobs: reads.jobs,
            pendingApprovals,
            now: reads.at,
          })
        : null,
    [reads, pendingApprovals],
  )

  const applyGate: Gate = loading
    ? { disabled: true, code: 'preflight_checking', reason: t('shell.reasonPreflightChecking') }
    : gate(requireNoPendingApprovals(pendingApprovals))

  return { preflight, loading, error, gate: applyGate, run }
}
