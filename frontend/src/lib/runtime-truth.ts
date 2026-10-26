/**
 * The truth badge's derivation: one honest answer to "is what I am seeing
 * true and fresh, and what do I do about it".
 *
 * Pure. `deriveRuntimeTruth` reads the connection state, the runtime signals
 * (frame age, refused handshake, doctor summary, /ready) and, on the desktop,
 * the supervisor's view of the gateway process, and returns:
 *
 * - a state: Ready / Degraded / Connecting / Blocked / Offline / Unknown,
 * - the reason behind it,
 * - where the data on screen comes from and how old it is (live frames, a
 *   snapshot from before the connection dropped, or nothing at all),
 * - exactly one recovery action.
 *
 * The web footer and the desktop toolbar render it with their own
 * components; the label helpers below are shared copy, not UI.
 */
import { t, tPlural } from '@/i18n'
import type { RpcState } from '@/lib/ws-rpc'
import {
  DEFAULT_TICK_MS,
  doctorErrorCount,
  EXPECTED_PROTOCOL,
  type RuntimeSignalsState,
} from '@/lib/runtime-signals'

export type TruthState = 'ready' | 'degraded' | 'connecting' | 'blocked' | 'offline' | 'unknown'
export type TruthSource = 'live' | 'snapshot' | 'unavailable'
export type TruthAction = 'retry_connection' | 'start_gateway' | 'open_inbox' | 'open_health'
export type TruthReason =
  | 'ok'
  | 'connecting'
  | 'gateway_starting'
  | 'gateway_stopped'
  | 'disconnected'
  | 'never_connected'
  | 'auth_failed'
  | 'protocol_mismatch'
  | 'handshake_refused'
  | 'stale'
  | 'doctor_errors'
  | 'safe_mode'
  | 'draining'
  | 'starting'
  | 'checking'
  | 'doctor_unavailable'

/** The supervisor's process states (desktop); mirrors `@shared/gateway`. */
export type HostGatewayState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'

/**
 * What only the host knows. The web console leaves this empty: it is served
 * by the gateway, so it cannot see or start the process.
 */
export interface TruthHost {
  gatewayState?: HostGatewayState
  /** The app may start the gateway itself (a managed, not an external, gateway). */
  canStartGateway?: boolean
}

export type TruthSignals = Pick<
  RuntimeSignalsState,
  | 'everConnected'
  | 'lastFrameAt'
  | 'tickIntervalMs'
  | 'protocol'
  | 'handshake'
  | 'doctor'
  | 'doctorError'
  | 'ready'
>

export interface TruthInput {
  rpcState: RpcState
  signals: TruthSignals
  /** Items waiting in the attention inbox. */
  attentionCount: number
  host?: TruthHost
}

export interface RuntimeTruth {
  state: TruthState
  reason: TruthReason
  source: TruthSource
  /** Age of the newest gateway frame, or null when there has never been one. */
  ageMs: number | null
  action: TruthAction
  /** Error-level doctor findings in the last report. */
  errorCount: number
  attentionCount: number
  /** The server's own words, when it gave any (a refused handshake). */
  detail: string | null
}

/** A connected view with no frame for this long is a snapshot, not live. */
export function staleAfterMs(tickIntervalMs: number): number {
  const tick = tickIntervalMs > 0 ? tickIntervalMs : DEFAULT_TICK_MS
  return Math.max(10_000, tick * 2.5)
}

export function deriveRuntimeTruth(input: TruthInput, now: number): RuntimeTruth {
  const { rpcState, signals, attentionCount, host } = input
  const ageMs = signals.lastFrameAt === null ? null : Math.max(0, now - signals.lastFrameAt)
  const errorCount = doctorErrorCount(signals.doctor)
  const offlineSource: TruthSource =
    signals.everConnected && signals.lastFrameAt !== null ? 'snapshot' : 'unavailable'
  const base = { ageMs, errorCount, attentionCount, detail: null as string | null }
  const startOrRetry: TruthAction =
    host?.canStartGateway && host.gatewayState !== 'running' && host.gatewayState !== 'starting'
      ? 'start_gateway'
      : 'retry_connection'

  // The desktop sees the process itself: a stopped gateway is offline
  // whatever the socket last said, and a starting one is not "offline" yet.
  if (host?.gatewayState && host.gatewayState !== 'running') {
    if (host.gatewayState === 'starting') {
      return {
        ...base,
        state: 'connecting',
        reason: 'gateway_starting',
        source: offlineSource,
        action: 'retry_connection',
      }
    }
    return {
      ...base,
      state: 'offline',
      reason: 'gateway_stopped',
      source: offlineSource,
      action: startOrRetry,
    }
  }

  if (rpcState !== 'connected' && signals.handshake) {
    const reason: TruthReason =
      signals.handshake.kind === 'auth_failed'
        ? 'auth_failed'
        : signals.handshake.kind === 'protocol_mismatch'
          ? 'protocol_mismatch'
          : 'handshake_refused'
    return {
      ...base,
      state: 'blocked',
      reason,
      source: offlineSource,
      action: 'open_health',
      detail: signals.handshake.message || null,
    }
  }

  if (rpcState === 'connecting') {
    return {
      ...base,
      state: 'connecting',
      reason: 'connecting',
      source: offlineSource,
      action: 'retry_connection',
    }
  }

  if (rpcState === 'disconnected') {
    return {
      ...base,
      state: 'offline',
      reason: signals.everConnected ? 'disconnected' : 'never_connected',
      source: offlineSource,
      action: startOrRetry,
    }
  }

  // Connected from here on.
  if (signals.protocol !== null && signals.protocol !== EXPECTED_PROTOCOL) {
    return {
      ...base,
      state: 'blocked',
      reason: 'protocol_mismatch',
      source: 'live',
      action: 'open_health',
    }
  }

  const fresh = ageMs !== null && ageMs <= staleAfterMs(signals.tickIntervalMs)
  if (!fresh) {
    return {
      ...base,
      state: 'degraded',
      reason: 'stale',
      source: 'snapshot',
      action: 'retry_connection',
    }
  }

  const readyState = signals.ready?.state ?? ''
  if (readyState === 'safe_mode') {
    return {
      ...base,
      state: 'degraded',
      reason: 'safe_mode',
      source: 'live',
      action: 'open_health',
    }
  }
  if (readyState === 'draining') {
    return { ...base, state: 'degraded', reason: 'draining', source: 'live', action: 'open_health' }
  }
  if (readyState === 'starting') {
    return {
      ...base,
      state: 'connecting',
      reason: 'starting',
      source: 'live',
      action: 'open_health',
    }
  }

  if (errorCount > 0) {
    return {
      ...base,
      state: 'degraded',
      reason: 'doctor_errors',
      source: 'live',
      action: 'open_health',
    }
  }
  if (!signals.doctor) {
    return {
      ...base,
      state: 'unknown',
      reason: signals.doctorError ? 'doctor_unavailable' : 'checking',
      source: 'live',
      action: 'open_health',
    }
  }
  return { ...base, state: 'ready', reason: 'ok', source: 'live', action: 'open_inbox' }
}

// ── Shared copy ──────────────────────────────────────────────────────────

/** "12 s", "3 min", "2 h". */
export function formatAge(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return t('shell.ageSeconds', { seconds })
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return t('shell.ageMinutes', { minutes })
  return t('shell.ageHours', { hours: Math.round(minutes / 60) })
}

export function truthStateLabel(state: TruthState): string {
  switch (state) {
    case 'ready':
      return t('shell.truthReady')
    case 'degraded':
      return t('shell.truthDegraded')
    case 'connecting':
      return t('shell.truthConnecting')
    case 'blocked':
      return t('shell.truthBlocked')
    case 'offline':
      return t('shell.truthOffline')
    default:
      return t('shell.truthUnknown')
  }
}

/** "Live · 12 s", "Snapshot · 3 min", "Unavailable". */
export function truthFreshnessLabel(truth: Pick<RuntimeTruth, 'source' | 'ageMs'>): string {
  if (truth.source === 'unavailable') return t('shell.truthUnavailable')
  if (truth.ageMs === null) return t('shell.truthSnapshotAgeless')
  const age = formatAge(truth.ageMs)
  return truth.source === 'live' ? t('shell.truthLive', { age }) : t('shell.truthSnapshot', { age })
}

export function truthReasonLabel(truth: RuntimeTruth): string {
  switch (truth.reason) {
    case 'ok':
      return t('shell.truthReasonOk')
    case 'connecting':
      return t('shell.truthReasonConnecting')
    case 'gateway_starting':
      return t('shell.truthReasonGatewayStarting')
    case 'gateway_stopped':
      return t('shell.truthReasonGatewayStopped')
    case 'disconnected':
      return t('shell.truthReasonDisconnected')
    case 'never_connected':
      return t('shell.truthReasonNeverConnected')
    case 'auth_failed':
      return t('shell.truthReasonAuthFailed')
    case 'protocol_mismatch':
      return t('shell.truthReasonProtocolMismatch')
    case 'handshake_refused':
      return t('shell.truthReasonHandshakeRefused')
    case 'stale':
      return t('shell.truthReasonStale', { age: formatAge(truth.ageMs ?? 0) })
    case 'doctor_errors':
      return tPlural('shell.truthReasonDoctorErrors', truth.errorCount)
    case 'safe_mode':
      return t('shell.truthReasonSafeMode')
    case 'draining':
      return t('shell.truthReasonDraining')
    case 'starting':
      return t('shell.truthReasonStarting')
    case 'checking':
      return t('shell.truthReasonChecking')
    default:
      return t('shell.truthReasonDoctorUnavailable')
  }
}

export function truthActionLabel(action: TruthAction): string {
  switch (action) {
    case 'retry_connection':
      return t('shell.truthActionRetry')
    case 'start_gateway':
      return t('shell.truthActionStart')
    case 'open_inbox':
      return t('shell.truthActionInbox')
    default:
      return t('shell.truthActionHealth')
  }
}

/** Status tone shared by both renderers' dots: ok / warn / danger / dim. */
export function truthTone(state: TruthState): 'ok' | 'warn' | 'danger' | 'dim' {
  if (state === 'ready') return 'ok'
  if (state === 'degraded' || state === 'connecting') return 'warn'
  if (state === 'unknown') return 'dim'
  return 'danger'
}
