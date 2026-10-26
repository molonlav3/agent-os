/**
 * The truth badge's hook: the derivation from runtime-truth.ts fed from the
 * live stores, re-evaluated once a second so the freshness readout ("Live ·
 * 12 s") ages honestly between frames. The clock only re-renders the badge;
 * it never fetches anything.
 *
 * Web and desktop call it with their own host facts and render the result
 * with their own components.
 */
import { useEffect, useMemo, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { useConnection } from '@/stores/connection'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { deriveRuntimeTruth, type RuntimeTruth, type TruthHost } from '@/lib/runtime-truth'
import { useAttentionInbox, type AttentionHost } from '@/lib/useAttentionInbox'

/** Wall clock in ms, ticking every `periodMs` while mounted. */
export function useClock(periodMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), periodMs)
    return () => clearInterval(id)
  }, [periodMs])
  return now
}

export interface RuntimeTruthHost extends TruthHost, AttentionHost {}

export function useRuntimeTruth(host?: RuntimeTruthHost): RuntimeTruth {
  const rpcState = useConnection((s) => s.state)
  const signals = useRuntimeSignals(
    useShallow((s) => ({
      everConnected: s.everConnected,
      lastFrameAt: s.lastFrameAt,
      tickIntervalMs: s.tickIntervalMs,
      protocol: s.protocol,
      handshake: s.handshake,
      shutdown: s.shutdown,
      doctor: s.doctor,
      doctorError: s.doctorError,
      ready: s.ready,
    })),
  )
  const { count } = useAttentionInbox(host)
  const now = useClock(1000)
  const gatewayState = host?.gatewayState
  const canStartGateway = host?.canStartGateway
  return useMemo(
    () =>
      deriveRuntimeTruth(
        { rpcState, signals, attentionCount: count, host: { gatewayState, canStartGateway } },
        now,
      ),
    [rpcState, signals, count, gatewayState, canStartGateway, now],
  )
}
