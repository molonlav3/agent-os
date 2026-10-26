/**
 * React side of the attention inbox: reads the approval monitor's store, the
 * pending questions, the runtime signals and the session-scoped dismissals,
 * and memoises the merged list. Shared by the web inbox view, the web nav
 * badge, the truth badge and the desktop's attention panel.
 *
 * Everything it reads is pushed by the gateway's own frames (approval
 * monitor, question frames, doctor refresh on the tick revision); the hook
 * adds no timer.
 */
import { useMemo } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useApprovals } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { attentionCount, buildAttentionItems, type AttentionItem } from '@/lib/attention-inbox'
import { usePendingQuestions } from '@/lib/pending-questions'
import {
  deriveRuntimeIssues,
  useDismissedIssues,
  type IssueHost,
  type RuntimeIssue,
} from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'

export interface AttentionHost extends IssueHost {
  /** A failed update the host tracks itself (the desktop's engine updater). */
  updateError?: string | null
}

export interface AttentionInbox {
  items: AttentionItem[]
  count: number
  issues: RuntimeIssue[]
}

/** Typed runtime issues for this host, before dismissals. */
export function useRuntimeIssues(host?: AttentionHost): RuntimeIssue[] {
  const rpcState = useConnection((s) => s.state)
  const signals = useRuntimeSignals(
    useShallow((s) => ({
      everConnected: s.everConnected,
      failedAttempts: s.failedAttempts,
      handshake: s.handshake,
      doctor: s.doctor,
      ready: s.ready,
      updateFailure: s.updateFailure,
      protocol: s.protocol,
    })),
  )
  const canRestartGateway = host?.canRestartGateway ?? false
  const updateError = host?.updateError ?? null
  return useMemo(
    () =>
      deriveRuntimeIssues({
        rpcState,
        signals,
        host: { canRestartGateway },
        updateError,
      }),
    [rpcState, signals, canRestartGateway, updateError],
  )
}

export function useAttentionInbox(host?: AttentionHost): AttentionInbox {
  const approvals = useApprovals((s) => s.pending)
  const questions = usePendingQuestions((s) => s.items)
  const dismissed = useDismissedIssues((s) => s.ids)
  const issues = useRuntimeIssues(host)
  return useMemo(() => {
    const items = buildAttentionItems({ approvals, questions, issues, dismissed })
    return { items, count: attentionCount(items), issues }
  }, [approvals, questions, issues, dismissed])
}
