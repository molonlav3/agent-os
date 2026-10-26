import type { RuntimeTruthHost } from '@/lib/useRuntimeTruth'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { useUpdates } from '~/stores/updates'

/**
 * What only the desktop knows about the runtime, for the shared truth and
 * issue derivations: the supervisor's process state, whether the app may
 * start or restart that process (a managed gateway, not an external one),
 * and the engine updater's last error.
 */
export function useRuntimeHost(): RuntimeTruthHost {
  const gatewayState = useGateway((s) => s.status.state)
  const managed = useSettings((s) => s.settings.gateway.mode !== 'external')
  const engineError = useUpdates((s) => (s.engine.phase === 'error' ? s.engine.error : null))
  return {
    gatewayState,
    canStartGateway: managed,
    canRestartGateway: managed,
    updateError: engineError || null,
  }
}
