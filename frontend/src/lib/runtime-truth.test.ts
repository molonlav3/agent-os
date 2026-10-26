import { describe, expect, it } from 'vitest'
import {
  deriveRuntimeTruth,
  formatAge,
  staleAfterMs,
  truthActionLabel,
  truthFreshnessLabel,
  truthReasonLabel,
  truthStateLabel,
  type TruthInput,
  type TruthSignals,
} from './runtime-truth'
import type { DoctorSummary, HandshakeFailure } from './runtime-signals'

const NOW = 10_000_000

const clean: DoctorSummary = {
  status: 'ready',
  ready: true,
  counts: { error: 0, warn: 1 },
  findings: [],
  fetchedAt: NOW,
}

function refusal(kind: HandshakeFailure['kind'], code: string, message: string): HandshakeFailure {
  return { kind, code, detailCode: '', message, retryAfterMs: null, reloadRequired: false, at: NOW }
}

function signals(over: Partial<TruthSignals> = {}): TruthSignals {
  return {
    everConnected: true,
    lastFrameAt: NOW - 12_000,
    tickIntervalMs: 30_000,
    protocol: 3,
    handshake: null,
    shutdown: null,
    doctor: clean,
    doctorError: null,
    ready: null,
    ...over,
  }
}

function derive(over: Partial<TruthInput> = {}, s: Partial<TruthSignals> = {}) {
  return deriveRuntimeTruth(
    { rpcState: 'connected', signals: signals(s), attentionCount: 0, ...over },
    NOW,
  )
}

describe('deriveRuntimeTruth', () => {
  it('is Ready over a live connection with a clean doctor report', () => {
    const truth = derive()
    expect(truth).toMatchObject({ state: 'ready', source: 'live', action: 'open_inbox' })
    expect(truthFreshnessLabel(truth)).toBe('Live · 12 s')
  })

  it('is Degraded when any finding is error-level', () => {
    const truth = derive({}, { doctor: { ...clean, counts: { error: 2 } } })
    expect(truth).toMatchObject({ state: 'degraded', reason: 'doctor_errors', errorCount: 2 })
    expect(truth.action).toBe('open_health')
    expect(truthReasonLabel(truth)).toBe('2 doctor findings are at error level.')
  })

  it('is Unknown until the first doctor report, and says why', () => {
    expect(derive({}, { doctor: null })).toMatchObject({ state: 'unknown', reason: 'checking' })
    expect(derive({}, { doctor: null, doctorError: 'boom' })).toMatchObject({
      state: 'unknown',
      reason: 'doctor_unavailable',
    })
  })

  it('treats a connected but silent socket as a stale snapshot', () => {
    const truth = derive({}, { lastFrameAt: NOW - 80_000 })
    expect(truth).toMatchObject({ state: 'degraded', reason: 'stale', source: 'snapshot' })
    expect(truth.action).toBe('retry_connection')
    expect(staleAfterMs(30_000)).toBe(75_000)
  })

  it('reads the /ready state when the gateway sends one', () => {
    const ready = (state: string) => ({ ready: false, state, failing: [], fetchedAt: NOW })
    expect(derive({}, { ready: ready('safe_mode') }).reason).toBe('safe_mode')
    expect(derive({}, { ready: ready('draining') }).state).toBe('degraded')
    expect(derive({}, { ready: ready('starting') }).state).toBe('connecting')
    expect(
      derive({}, { ready: { ready: true, state: 'ready', failing: [], fetchedAt: NOW } }).state,
    ).toBe('ready')
  })

  it('is Connecting while the socket opens', () => {
    expect(derive({ rpcState: 'connecting' })).toMatchObject({
      state: 'connecting',
      action: 'retry_connection',
      source: 'snapshot',
    })
  })

  it('is Offline after a drop, showing the last snapshot', () => {
    const truth = derive({ rpcState: 'disconnected' }, { lastFrameAt: NOW - 180_000 })
    expect(truth).toMatchObject({ state: 'offline', reason: 'disconnected', source: 'snapshot' })
    expect(truthFreshnessLabel(truth)).toBe('Snapshot · 3 min')
    expect(truthActionLabel(truth.action)).toBe('Retry connection')
  })

  it('is Offline with nothing to show before the first connection', () => {
    const truth = derive(
      { rpcState: 'disconnected' },
      { everConnected: false, lastFrameAt: null, doctor: null },
    )
    expect(truth).toMatchObject({
      state: 'offline',
      reason: 'never_connected',
      source: 'unavailable',
    })
    expect(truthFreshnessLabel(truth)).toBe('Unavailable')
  })

  it('is Blocked by a refused handshake, keeping the server words', () => {
    const truth = derive(
      { rpcState: 'disconnected' },
      {
        handshake: refusal('auth_failed', 'UNAUTHORIZED', 'Authentication failed'),
      },
    )
    expect(truth).toMatchObject({ state: 'blocked', reason: 'auth_failed', action: 'open_health' })
    expect(truth.detail).toBe('Authentication failed')
    expect(truthStateLabel(truth.state)).toBe('Blocked')
  })

  it('is Blocked on a protocol mismatch', () => {
    expect(
      derive(
        { rpcState: 'disconnected' },
        {
          handshake: refusal('protocol_mismatch', 'INVALID_REQUEST', 'x'),
        },
      ).reason,
    ).toBe('protocol_mismatch')
    expect(derive({}, { protocol: 4 })).toMatchObject({
      state: 'blocked',
      reason: 'protocol_mismatch',
    })
  })

  it('is Blocked by a rate-limited sign-in, with the server words', () => {
    const truth = derive(
      { rpcState: 'disconnected' },
      { handshake: refusal('auth_rate_limited', 'UNAUTHORIZED', 'Too many failed attempts') },
    )
    expect(truth).toMatchObject({ state: 'blocked', reason: 'auth_rate_limited' })
    expect(truthReasonLabel(truth)).toMatch(/Too many failed sign-in attempts/)
  })

  it('reads an announced in-place restart as Connecting, not Offline', () => {
    const shutdown = { reason: 'restart:update', restartExpectedMs: 4000, at: NOW - 2000 }
    const truth = derive({ rpcState: 'disconnected' }, { shutdown })
    expect(truth).toMatchObject({
      state: 'connecting',
      reason: 'restarting',
      detail: 'restart:update',
    })
    // Long past the time it gave, it is just offline again.
    expect(
      derive({ rpcState: 'disconnected' }, { shutdown: { ...shutdown, at: NOW - 120_000 } }).state,
    ).toBe('offline')
    // A terminal stop (no restart time) is offline.
    expect(
      derive({ rpcState: 'disconnected' }, { shutdown: { ...shutdown, restartExpectedMs: null } })
        .state,
    ).toBe('offline')
  })

  it('lets the desktop see the process: stopped means Start gateway', () => {
    const truth = derive(
      { rpcState: 'connected', host: { gatewayState: 'stopped', canStartGateway: true } },
      {},
    )
    expect(truth).toMatchObject({
      state: 'offline',
      reason: 'gateway_stopped',
      action: 'start_gateway',
    })
    expect(truthActionLabel(truth.action)).toBe('Start gateway')
    // An external gateway cannot be started from the app: retry instead.
    expect(derive({ host: { gatewayState: 'error', canStartGateway: false } }).action).toBe(
      'retry_connection',
    )
    expect(derive({ host: { gatewayState: 'starting', canStartGateway: true } })).toMatchObject({
      state: 'connecting',
      reason: 'gateway_starting',
    })
    // A running process defers to the socket.
    expect(derive({ host: { gatewayState: 'running', canStartGateway: true } }).state).toBe('ready')
  })

  it('carries exactly one action for every state', () => {
    const cases = [
      derive(),
      derive({ rpcState: 'connecting' }),
      derive({ rpcState: 'disconnected' }),
      derive({}, { doctor: null }),
      derive({}, { doctor: { ...clean, counts: { error: 1 } } }),
    ]
    for (const truth of cases) {
      expect(typeof truth.action).toBe('string')
      expect(truthActionLabel(truth.action)).not.toBe('')
    }
  })
})

describe('formatAge', () => {
  it('reads seconds, minutes and hours', () => {
    expect(formatAge(400)).toBe('0 s')
    expect(formatAge(59_000)).toBe('59 s')
    expect(formatAge(61_000)).toBe('1 min')
    expect(formatAge(2 * 3_600_000)).toBe('2 h')
  })
})
