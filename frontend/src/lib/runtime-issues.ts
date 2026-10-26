/**
 * Typed runtime issues: `doctor.status` findings plus the connection's own
 * state, turned into `{id, kind, severity, title, detail, actions[]}` the
 * attention inbox (web) and the attention panel (desktop) can act on.
 *
 * Pure apart from the session-scoped dismiss store at the bottom. Actions
 * are intents (`restart_gateway`, `open_fix_steps`, `open_settings`, …);
 * each host decides how to carry one out, and a host that cannot (the web
 * console cannot restart the process) never receives it.
 */
import { create } from 'zustand'
import { t } from '@/i18n'
import type { RpcState } from '@/lib/ws-rpc'
import type { DoctorFinding, RuntimeSignalsState } from '@/lib/runtime-signals'

export type IssueKind =
  | 'gateway_unreachable'
  | 'auth_failed'
  | 'protocol_mismatch'
  | 'model_auth_required'
  | 'rate_limited'
  | 'channel_terminal'
  | 'safe_mode'
  | 'update_failed'
  | 'doctor_warning'

export type IssueSeverity = 'error' | 'warn'

export type IssueActionKind =
  'restart_gateway' | 'retry_connection' | 'open_fix_steps' | 'open_settings' | 'dismiss'

/** Where `open_settings` should land; each host maps it to its own screen. */
export type SettingsTarget = 'connection' | 'providers' | 'channels' | 'updates'

export interface IssueAction {
  kind: IssueActionKind
  label: string
  /** Ask before running (restart interrupts running turns). */
  confirm?: string
  settings?: SettingsTarget
}

export interface IssueFixStep {
  label?: string
  command?: string
  detail?: string
}

export interface RuntimeIssue {
  /** Stable across refreshes: the dismiss key and the inbox item id. */
  id: string
  kind: IssueKind
  severity: IssueSeverity
  title: string
  detail: string
  /** The doctor finding behind it, when there is one. */
  findingId: string | null
  surface: string | null
  fixSteps: IssueFixStep[]
  actions: IssueAction[]
}

export interface IssueHost {
  /** The host can restart the gateway process (the desktop's managed gateway). */
  canRestartGateway?: boolean
}

export interface IssueInput {
  rpcState: RpcState
  signals: Pick<
    RuntimeSignalsState,
    | 'everConnected'
    | 'failedAttempts'
    | 'handshake'
    | 'doctor'
    | 'ready'
    | 'updateFailure'
    | 'protocol'
  >
  host?: IssueHost
  /** A failed update the host knows about (the desktop's engine updater). */
  updateError?: string | null
}

const MODEL_AUTH_IDS = new Set([
  'provider.active.not_configured',
  'provider.active.missing',
  'router.credentials.missing',
  'router.judge.no_credentials',
])

const CHANNEL_TERMINAL = /^channel\.[^.]+\.(?:dead|exhausted)$/

/** Which kind a doctor finding is. Unknown ids fall back to `doctor_warning`. */
export function classifyFinding(finding: DoctorFinding): IssueKind {
  const id = String(finding.id || '')
  if (MODEL_AUTH_IDS.has(id) || /(?:^|[._])(?:auth|credentials?)(?:[._]|$)/.test(id)) {
    return 'model_auth_required'
  }
  if (id === 'provider.circuit.open' || /rate[._]?limit/.test(id)) return 'rate_limited'
  if (CHANNEL_TERMINAL.test(id)) return 'channel_terminal'
  if (/safe[._]?mode/.test(id)) return 'safe_mode'
  return 'doctor_warning'
}

function action(kind: IssueActionKind, extra: Partial<IssueAction> = {}): IssueAction {
  const labels: Record<IssueActionKind, string> = {
    restart_gateway: t('shell.issueActionRestart'),
    retry_connection: t('shell.issueActionRetry'),
    open_fix_steps: t('shell.issueActionFixSteps'),
    open_settings: t('shell.issueActionSettings'),
    dismiss: t('shell.issueActionDismiss'),
  }
  const out: IssueAction = { kind, label: labels[kind], ...extra }
  if (kind === 'restart_gateway') out.confirm = t('shell.issueRestartConfirm')
  return out
}

function actionsFor(kind: IssueKind, host: IssueHost, restartRequired: boolean): IssueAction[] {
  const restart = host.canRestartGateway ? [action('restart_gateway')] : []
  const dismiss = action('dismiss')
  switch (kind) {
    // Offline the doctor (and the Health view that shows its fix steps)
    // cannot be read, so the connection issues offer a retry instead.
    case 'gateway_unreachable':
      return host.canRestartGateway
        ? [action('restart_gateway'), action('retry_connection'), dismiss]
        : [action('retry_connection'), dismiss]
    case 'auth_failed':
      return [
        action('open_settings', { settings: 'connection' }),
        action('retry_connection'),
        dismiss,
      ]
    case 'protocol_mismatch':
      return [action('open_settings', { settings: 'updates' }), action('retry_connection'), dismiss]
    case 'model_auth_required':
      return [action('open_settings', { settings: 'providers' }), action('open_fix_steps'), dismiss]
    case 'channel_terminal':
      return [
        action('open_settings', { settings: 'channels' }),
        action('open_fix_steps'),
        ...restart,
        dismiss,
      ]
    case 'safe_mode':
      return [...restart, action('open_fix_steps'), dismiss]
    case 'update_failed':
      return [action('open_settings', { settings: 'updates' }), dismiss]
    case 'rate_limited':
      return [action('open_fix_steps'), dismiss]
    default:
      return [action('open_fix_steps'), ...(restartRequired ? restart : []), dismiss]
  }
}

function issueFromFinding(finding: DoctorFinding, host: IssueHost): RuntimeIssue | null {
  const severity =
    finding.severity === 'error' ? 'error' : finding.severity === 'warn' ? 'warn' : null
  if (!severity) return null
  const findingId = String(finding.id || '')
  const kind = classifyFinding(finding)
  return {
    id: `issue:${kind}:${findingId || finding.title || 'finding'}`,
    kind,
    severity,
    title: finding.title || t('shell.issueFindingUntitled', { id: findingId || '?' }),
    detail: finding.detail || '',
    findingId: findingId || null,
    surface: finding.surface || null,
    fixSteps: Array.isArray(finding.fixSteps) ? finding.fixSteps : [],
    actions: actionsFor(kind, host, finding.restartRequired === true),
  }
}

function synthetic(
  kind: IssueKind,
  title: string,
  detail: string,
  host: IssueHost,
  severity: IssueSeverity = 'error',
): RuntimeIssue {
  return {
    id: `issue:${kind}`,
    kind,
    severity,
    title,
    detail,
    findingId: null,
    surface: 'gateway',
    fixSteps: [],
    actions: actionsFor(kind, host, false),
  }
}

/**
 * Every current issue, most severe first. While the console is not
 * connected only the connection issue (and a known update failure) is
 * listed: the last doctor report is a snapshot, and repeating its findings
 * as if they were current would be the opposite of honest.
 */
export function deriveRuntimeIssues(input: IssueInput): RuntimeIssue[] {
  const host = input.host ?? {}
  const { signals } = input
  const issues: RuntimeIssue[] = []
  const handshake = signals.handshake

  if (input.rpcState !== 'connected') {
    if (handshake?.kind === 'auth_failed') {
      issues.push(
        synthetic(
          'auth_failed',
          t('shell.issueAuthFailedTitle'),
          t('shell.issueAuthFailedDetail', { message: handshake.message || handshake.code }),
          host,
        ),
      )
    } else if (handshake?.kind === 'protocol_mismatch') {
      issues.push(
        synthetic(
          'protocol_mismatch',
          t('shell.issueProtocolMismatchTitle'),
          t('shell.issueProtocolMismatchDetail', { message: handshake.message }),
          host,
        ),
      )
    } else if (signals.everConnected || signals.failedAttempts > 0) {
      // Not while a fresh page is still making its first attempt; after a
      // failed attempt (or a drop) it stays listed through the reconnect
      // cycles instead of flickering with each "connecting" phase.
      issues.push(
        synthetic(
          'gateway_unreachable',
          t('shell.issueGatewayUnreachableTitle'),
          t('shell.issueGatewayUnreachableDetail'),
          host,
        ),
      )
    }
  } else {
    const ready = signals.ready
    if (ready?.state === 'safe_mode') {
      issues.push(
        synthetic(
          'safe_mode',
          t('shell.issueSafeModeTitle'),
          ready.failing.length
            ? t('shell.issueSafeModeDetail', { failing: ready.failing.join(', ') })
            : t('shell.issueSafeModeDetailBare'),
          host,
        ),
      )
    }
    const seen = new Set(issues.map((i) => i.id))
    for (const finding of signals.doctor?.findings ?? []) {
      const issue = issueFromFinding(finding, host)
      if (!issue || seen.has(issue.id)) continue
      seen.add(issue.id)
      issues.push(issue)
    }
  }

  const updateDetail = input.updateError ?? signals.updateFailure?.detail ?? null
  if (updateDetail) {
    issues.push(synthetic('update_failed', t('shell.issueUpdateFailedTitle'), updateDetail, host))
  }

  // Stable: errors before warnings, original order within a severity.
  return issues
    .map((issue, index) => ({ issue, index }))
    .sort((a, b) => {
      const sa = a.issue.severity === 'error' ? 0 : 1
      const sb = b.issue.severity === 'error' ? 0 : 1
      return sa - sb || a.index - b.index
    })
    .map(({ issue }) => issue)
}

// ── Session-scoped dismissals ─────────────────────────────────────────────

export const DISMISSED_ISSUES_KEY = 'agentos.dismissedIssues'

function loadDismissed(): string[] {
  try {
    const raw = JSON.parse(sessionStorage.getItem(DISMISSED_ISSUES_KEY) || '[]')
    return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

function saveDismissed(ids: readonly string[]): void {
  try {
    sessionStorage.setItem(DISMISSED_ISSUES_KEY, JSON.stringify(ids))
  } catch {
    /* storage unavailable: the dismissal still holds for this page */
  }
}

/**
 * Dismissed issue ids, for this browser session only (sessionStorage): a
 * dismissal hides an issue until the tab or window is closed, never for
 * good. The issue comes back by itself if it is still true next session.
 */
export const useDismissedIssues = create<{
  ids: readonly string[]
  dismiss(id: string): void
  restoreAll(): void
}>((set, get) => ({
  ids: loadDismissed(),
  dismiss: (id) => {
    if (get().ids.includes(id)) return
    const ids = [...get().ids, id]
    saveDismissed(ids)
    set({ ids })
  },
  restoreAll: () => {
    saveDismissed([])
    set({ ids: [] })
  },
}))
