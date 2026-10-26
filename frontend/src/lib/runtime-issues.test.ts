import { afterEach, describe, expect, it } from 'vitest'
import {
  classifyFinding,
  deriveRuntimeIssues,
  DISMISSED_ISSUES_KEY,
  useDismissedIssues,
  type IssueInput,
} from './runtime-issues'
import type { DoctorFinding, DoctorSummary, HandshakeFailure } from './runtime-signals'

function refusal(
  kind: HandshakeFailure['kind'],
  code: string,
  message: string,
  extra: Partial<HandshakeFailure> = {},
): HandshakeFailure {
  return {
    kind,
    code,
    detailCode: '',
    message,
    retryAfterMs: null,
    reloadRequired: false,
    at: 1,
    ...extra,
  }
}

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
    // Offline the doctor (and Health) cannot be read: retry, not fix steps.
    expect(issues[0]!.actions.map((a) => a.kind)).toEqual(['retry_connection', 'dismiss'])
    expect(
      deriveRuntimeIssues(
        input({ rpcState: 'disconnected', host: { canRestartGateway: true } }),
      )[0]!.actions.map((a) => a.kind),
    ).toEqual(['restart_gateway', 'retry_connection', 'dismiss'])
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
          handshake: refusal('auth_failed', 'UNAUTHORIZED', 'Authentication failed'),
        },
      ),
    )
    expect(auth.map((i) => i.kind)).toEqual(['auth_failed'])
    expect(auth[0]!.detail).toContain('Authentication failed')
    const proto = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        {
          handshake: refusal(
            'protocol_mismatch',
            'INVALID_REQUEST',
            'Unsupported protocol version range',
          ),
        },
      ),
    )
    expect(proto.map((i) => i.kind)).toEqual(['protocol_mismatch'])
  })

  it('maps the typed refusals: rate-limited sign-in, and a reload for an older console', () => {
    const limited = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        {
          handshake: refusal('auth_rate_limited', 'UNAUTHORIZED', 'Too many', {
            detailCode: 'AUTH_RATE_LIMITED',
            retryAfterMs: 45_000,
          }),
        },
      ),
    )
    expect(limited.map((i) => [i.kind, i.id])).toEqual([
      ['rate_limited', 'issue:rate_limited:auth'],
    ])
    expect(limited[0]!.detail).toContain('45 s')
    expect(limited[0]!.actions.map((a) => a.kind)).toEqual([
      'open_settings',
      'retry_connection',
      'dismiss',
    ])
    const reload = deriveRuntimeIssues(
      input(
        { rpcState: 'disconnected' },
        {
          handshake: refusal('protocol_mismatch', 'INVALID_REQUEST', 'x', {
            detailCode: 'PROTOCOL_MISMATCH',
            reloadRequired: true,
          }),
        },
      ),
    )
    expect(reload[0]!.detail).toMatch(/Reload the page/)
  })

  it('lists safe mode once when /ready and the doctor both report it', () => {
    const issues = deriveRuntimeIssues(
      input(
        {},
        {
          ready: { ready: false, state: 'safe_mode', failing: ['safe_mode'], fetchedAt: 1 },
          doctor: doctor([
            {
              id: 'gateway.safe_mode',
              severity: 'warn',
              title: 'Gateway is in safe mode',
              detail: '4 unclean gateway boots in the last 300s',
            },
          ]),
        },
      ),
    )
    expect(issues.filter((i) => i.kind === 'safe_mode').map((i) => i.title)).toEqual([
      'Gateway is in safe mode',
    ])
    // Without the doctor's word, /ready alone still raises it.
    const bare = deriveRuntimeIssues(
      input(
        {},
        { ready: { ready: false, state: 'safe_mode', failing: ['safe_mode'], fetchedAt: 1 } },
      ),
    )
    expect(bare.map((i) => [i.kind, i.detail])).toEqual([
      ['safe_mode', 'The gateway started in safe mode; some subsystems are off.'],
    ])
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
