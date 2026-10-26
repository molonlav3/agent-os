/**
 * Disabled-with-reason: the console's convention for a control that cannot
 * be used right now. It stays visible, it is disabled, and it says why in
 * words, never hidden and never a silent no-op.
 *
 * This module is headless. A check returns `null` when it passes or a
 * `{code, reason}` when it blocks; `gate()` takes the first block. The web
 * console renders the reason with its `DisabledReason` wrapper; the desktop
 * renders the same `Gate` with its own markup.
 *
 *   const run = gate(requireConnection(rpcState), requireScheduler(unavailable))
 *   <Button disabled={run.disabled || busy}>Run</Button>
 *   {run.reason}
 *
 * Transient "busy" states are deliberately not reasons: a spinner already
 * says that, and a reason that flickers on every click is noise.
 */
import { t, tPlural } from '@/i18n'
import type { RpcState } from '@/lib/ws-rpc'

export type GateCode =
  | 'gateway_offline'
  | 'gateway_connecting'
  | 'scheduler_unavailable'
  | 'approvals_pending'
  | 'preflight_checking'
  | 'custom'

export interface GateBlock {
  code: GateCode
  reason: string
}

export interface Gate {
  disabled: boolean
  code: GateCode | null
  reason: string | null
}

export const OPEN_GATE: Gate = { disabled: false, code: null, reason: null }

/** The first blocking check wins; no block means the control is usable. */
export function gate(...checks: ReadonlyArray<GateBlock | null | undefined | false>): Gate {
  for (const check of checks) {
    if (check) return { disabled: true, code: check.code, reason: check.reason }
  }
  return OPEN_GATE
}

/** A one-off reason a caller words itself. */
export function blockedBecause(reason: string): GateBlock {
  return { code: 'custom', reason }
}

/** Needs the gateway connection (anything that sends an RPC or answers a prompt). */
export function requireConnection(state: RpcState): GateBlock | null {
  if (state === 'connected') return null
  if (state === 'connecting') {
    return { code: 'gateway_connecting', reason: t('shell.reasonGatewayConnecting') }
  }
  return { code: 'gateway_offline', reason: t('shell.reasonGatewayOffline') }
}

/** Needs the cron scheduler, which a gateway can run without. */
export function requireScheduler(unavailable: boolean): GateBlock | null {
  return unavailable
    ? { code: 'scheduler_unavailable', reason: t('shell.reasonSchedulerUnavailable') }
    : null
}

/** An update restarts the gateway; a pending approval would be dropped with it. */
export function requireNoPendingApprovals(count: number): GateBlock | null {
  return count > 0
    ? { code: 'approvals_pending', reason: tPlural('shell.reasonApprovalsPending', count) }
    : null
}
