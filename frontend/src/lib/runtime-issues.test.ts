import { afterEach, describe, expect, it } from 'vitest'
import {
  classifyFinding,
  deriveRuntimeIssues,
  DISMISSED_ISSUES_KEY,
  useDismissedIssues,
  type IssueInput,
} from './runtime-issues'
import type { DoctorFinding, DoctorSummary } from './runtime-signals'

function doctor(findings: DoctorFinding[]): DoctorSummary {
  return { status: 'degraded', ready: true, counts: {}, findings, fetchedAt: 1 }
}

function input(
  over: Partial<IssueInput> = {},
  signals: Partial<IssueInput['signals']> = {},
): IssueInput {
  return {
    rpcState: 'connected',
    signals: {
      everConnected: true,
      failedAttempts: 0,
      handshake: null,
      doctor: null,
      ready: null,
      updateFailure: null,
      protocol: 3,
      ...signals,
    },
    ...over,
  }
}

describe('classifyFinding', () => {
  it.each([
    ['provider.active.not_configured', 'model_auth_required'],
    ['router.credentials.missing', 'model_auth_required'],
    ['router.judge.no_credentials', 'model_auth_required'],
    ['provider.circuit.open', 'rate_limited'],
    ['provider.rate_limited', 'rate_limited'],
    ['channel.telegram.dead', 'channel_terminal'],
    ['channel.slack.exhausted', 'channel_terminal'],
    ['gateway.safe_mode', 'safe_mode'],
    ['logs.gateway_file_log.missing', 'doctor_warning'],
    ['channel.telegram.stopped', 'doctor_warning'],
  ])('%s -> %s', (id, kind) => {
    expect(classifyFinding({ id })).toBe(kind)
  })
})

describe('deriveRuntimeIssues', () => {
  it('turns error and warn findings into typed issues, errors first, skipping info/ok', () => {
    const issues = deriveRuntimeIssues(
      input(
        {},
        {
          doctor: doctor([
            { id: 'logs.gateway_file_log.missing', severity: 'warn', title: 'Log file missing' },
            { id: 'search.provider.ready', severity: 'ok', title: 'ok' },
            {
              id: 'provider.active.not_configured',
              severity: 'error',
              title: 'Active provider is not configured',
              detail: 'openrouter is active but missing required configuration.',
              fixSteps: [{ label: 'Configure provider', command: 'agentos providers configure x' }],
            },
            { id: 'channels.none_configured', severity: 'info', title: 'none' },
          ]),
        },
      ),
    )
    expect(issues.map((i) => [i.kind, i.severity])).toEqual([
      ['model_auth_required', 'error'],
      ['doctor_warning', 'warn'],
    ])
    const auth = issues[0]!
    expect(auth.id).toBe('issue:model_auth_required:provider.active.not_configured')
    expect(auth.fixSteps).toHaveLength(1)
    expect(auth.actions.map((a) => a.kind)).toEqual(['open_settings', 'open_fix_steps', 'dismiss'])
    expect(auth.actions[0]!.settings).toBe('providers')
  })

  it('offers Restart gateway only to a host that can run it, with a confirmation', () => {
    const finding = {
      id: 'channel.telegram.dead',
      severity: 'error',
      title: 'Channel telegram is dead',
    }
    const web = deriveRuntimeIssues(input({}, { doctor: doctor([finding]) }))
    expect(web[0]!.actions.some((a) => a.kind === 'restart_gateway')).toBe(false)
    const desk = deriveRuntimeIssues(
      input({ host: { canRestartGateway: true } }, { doctor: doctor([finding]) }),
    )
    const restart = desk[0]!.actions.find((a) => a.kind === 'restart_gateway')
    expect(restart?.confirm).toMatch(/Restart the gateway\?/)
  })

  it('reports the lost connection instead of repeating a stale doctor report', () => {
    const issues = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        { doctor: doctor([{ id: 'x', severity: 'error', title: 'old' }]) },
      ),
    )
    expect(issues.map((i) => i.kind)).toEqual(['gateway_unreachable'])
    expect(issues[0]!.actions.map((a) => a.kind)).toEqual([
      'retry_connection',
      'open_fix_steps',
      'dismiss',
    ])
    expect(
      deriveRuntimeIssues(
        input({ rpcState: 'disconnected', host: { canRestartGateway: true } }),
      )[0]!.actions[0]!.kind,
    ).toBe('restart_gateway')
  })

  it('waits for a first attempt to fail before calling a fresh page unreachable', () => {
    const fresh = { everConnected: false, failedAttempts: 0 }
    expect(deriveRuntimeIssues(input({ rpcState: 'disconnected' }, fresh))).toEqual([])
    expect(deriveRuntimeIssues(input({ rpcState: 'connecting' }, fresh))).toEqual([])
    // Once an attempt failed it is listed, and stays listed through the
    // reconnect cycles instead of flickering with each "connecting" phase.
    const failed = { everConnected: false, failedAttempts: 1 }
    expect(deriveRuntimeIssues(input({ rpcState: 'disconnected' }, failed))[0]!.kind).toBe(
      'gateway_unreachable',
    )
    expect(deriveRuntimeIssues(input({ rpcState: 'connecting' }, failed))[0]!.kind).toBe(
      'gateway_unreachable',
    )
    expect(deriveRuntimeIssues(input({ rpcState: 'connecting' }))[0]!.kind).toBe(
      'gateway_unreachable',
    )
  })

  it('names a refused handshake', () => {
    const auth = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        {
          handshake: {
            kind: 'auth_failed',
            code: 'UNAUTHORIZED',
            message: 'Authentication failed',
            at: 1,
          },
        },
      ),
    )
    expect(auth.map((i) => i.kind)).toEqual(['auth_failed'])
    expect(auth[0]!.detail).toContain('Authentication failed')
    const proto = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        {
          handshake: {
            kind: 'protocol_mismatch',
            code: 'INVALID_REQUEST',
            message: 'Unsupported protocol version range',
            at: 1,
          },
        },
      ),
    )
    expect(proto.map((i) => i.kind)).toEqual(['protocol_mismatch'])
  })

  it('reports safe mode from /ready and a failed update', () => {
    const issues = deriveRuntimeIssues(
      input(
        { updateError: 'uv tool upgrade exited 1' },
        {
          ready: {
            ready: false,
            state: 'safe_mode',
            failing: ['memory', 'channels'],
            fetchedAt: 1,
          },
        },
      ),
    )
    expect(issues.map((i) => i.kind)).toEqual(['safe_mode', 'update_failed'])
    expect(issues[0]!.detail).toContain('memory, channels')
    expect(issues[1]!.detail).toBe('uv tool upgrade exited 1')
  })
})

describe('useDismissedIssues', () => {
  afterEach(() => useDismissedIssues.getState().restoreAll())

  it('dismisses for this browser session only (sessionStorage)', () => {
    useDismissedIssues.getState().dismiss('issue:rate_limited:provider.circuit.open')
    useDismissedIssues.getState().dismiss('issue:rate_limited:provider.circuit.open')
    expect(useDismissedIssues.getState().ids).toEqual(['issue:rate_limited:provider.circuit.open'])
    expect(JSON.parse(sessionStorage.getItem(DISMISSED_ISSUES_KEY)!)).toEqual([
      'issue:rate_limited:provider.circuit.open',
    ])
    expect(localStorage.getItem(DISMISSED_ISSUES_KEY)).toBeNull()
  })
})
