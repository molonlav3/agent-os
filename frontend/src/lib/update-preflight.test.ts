import { describe, expect, it } from 'vitest'
import {
  evaluateUpdatePreflight,
  isJobDueSoon,
  isSessionRunning,
  nextRunAt,
} from './update-preflight'

const NOW = Date.UTC(2026, 9, 26, 12, 0, 0)

describe('update preflight', () => {
  it('counts sessions with a queued or running task', () => {
    expect(isSessionRunning({ active_task: { status: 'running' } })).toBe(true)
    expect(isSessionRunning({ activeTask: { status: 'queued' } })).toBe(true)
    expect(isSessionRunning({ run_status: 'running' })).toBe(true)
    expect(isSessionRunning({ active_task: { status: 'succeeded' }, run_status: 'idle' })).toBe(
      false,
    )
    expect(isSessionRunning({})).toBe(false)
  })

  it('finds enabled jobs due within the next 10 minutes', () => {
    const at = (ms: number) => new Date(NOW + ms).toISOString()
    expect(isJobDueSoon({ enabled: true, next_run: at(5 * 60_000) }, NOW)).toBe(true)
    expect(isJobDueSoon({ enabled: true, next_run: at(11 * 60_000) }, NOW)).toBe(false)
    expect(isJobDueSoon({ enabled: false, next_run: at(60_000) }, NOW)).toBe(false)
    expect(isJobDueSoon({ enabled: true, next_run: at(-60_000) }, NOW)).toBe(false)
    expect(isJobDueSoon({ enabled: true, status: 'running' }, NOW)).toBe(true)
    // Epoch seconds and milliseconds both parse.
    expect(nextRunAt({ next_run: NOW / 1000 })).toBe(NOW)
    expect(nextRunAt({ next_run: NOW })).toBe(NOW)
    expect(nextRunAt({ next_run: 'not a date' })).toBeNull()
  })

  it('blocks on pending approvals and only warns about running work', () => {
    const sessions = [
      { key: 's1', active_task: { status: 'running' } },
      { key: 's2', run_status: 'idle' },
    ]
    const jobs = [{ id: 'j1', name: 'Digest', enabled: true, next_run: NOW + 120_000 }]
    const blocked = evaluateUpdatePreflight({ sessions, jobs, pendingApprovals: 2, now: NOW })
    expect(blocked.runningSessions).toEqual(['s1'])
    expect(blocked.dueJobs).toEqual([{ id: 'j1', name: 'Digest', nextRunAt: NOW + 120_000 }])
    expect(blocked.gate).toMatchObject({ disabled: true, code: 'approvals_pending' })
    expect(blocked.warn).toBe(true)

    const warned = evaluateUpdatePreflight({ sessions, jobs, pendingApprovals: 0, now: NOW })
    expect(warned.gate.disabled).toBe(false)
    expect(warned.warn).toBe(true)

    const clear = evaluateUpdatePreflight({ sessions: [], jobs: [], pendingApprovals: 0, now: NOW })
    expect(clear).toMatchObject({ warn: false, gate: { disabled: false } })
  })
})
