import { useId, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BellRingIcon } from 'lucide-react'
import { useRpc } from '@/app/providers'
import { t } from '@/i18n'
import '@/i18n/en/settings'
import {
  browserWebPushEnv,
  disableWebPush,
  enableWebPush,
  readWebPushState,
  WebPushError,
  type RpcCall,
  type WebPushEnv,
  type WebPushState,
} from '@/services/web-push'

export const WEB_PUSH_QUERY_KEY = ['web-push', 'state'] as const

function failureText(error: unknown): string {
  if (error instanceof WebPushError) {
    if (error.reason === 'denied') return t('settings.pushDenied')
    if (error.reason === 'disabled') return t('settings.pushGatewayOff')
    return t('settings.pushUnsupported')
  }
  return t('settings.pushFailed', {
    message: error instanceof Error ? error.message : String(error),
  })
}

/** Why the switch cannot be flipped right now, or null when it can. */
function blockedReason(state: WebPushState): string | null {
  if (state.support === 'insecure') return t('settings.pushInsecure')
  if (state.support === 'unsupported') return t('settings.pushUnsupported')
  // An existing subscription can always be turned off.
  if (state.subscribed) return null
  if (state.gatewayEnabled === null) return t('settings.pushGatewayMissing')
  if (!state.gatewayEnabled) return t('settings.pushGatewayOff')
  if (state.permission === 'denied') return t('settings.pushDenied')
  return null
}

/**
 * The opt-in for approval notifications in this browser (services/web-push.ts).
 * The state is read back from the browser's own subscription, so it shows
 * what this browser will actually receive.
 */
export function WebPushToggle({ env: envOverride }: { env?: WebPushEnv }) {
  const rpc = useRpc()
  const [env] = useState<WebPushEnv>(() => envOverride ?? browserWebPushEnv())
  const [call] = useState(() => {
    const callRpc: RpcCall = async (method, params) => {
      await rpc.waitForConnection()
      return rpc.call(method, params)
    }
    return callRpc
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const titleId = useId()
  const noteId = useId()

  const stateQuery = useQuery<WebPushState>({
    queryKey: WEB_PUSH_QUERY_KEY,
    queryFn: () => readWebPushState(env, call),
    retry: false,
    // Permission can change in the browser's own settings meanwhile.
    refetchOnWindowFocus: true,
  })
  const state = stateQuery.data
  const subscribed = state?.subscribed === true
  const blocked = state ? blockedReason(state) : null
  const note = busy
    ? t('settings.pushBusy')
    : !state
      ? stateQuery.isError
        ? failureText(stateQuery.error)
        : t('settings.pushChecking')
      : (blocked ?? (subscribed ? t('settings.pushOn') : t('settings.pushOff')))

  const onToggle = async (): Promise<void> => {
    if (!state || busy) return
    setBusy(true)
    setError('')
    try {
      // Straight from the click: the permission prompt needs the gesture.
      if (subscribed) await disableWebPush(env, call)
      else await enableWebPush(env, call)
    } catch (err) {
      setError(failureText(err))
    } finally {
      setBusy(false)
      void stateQuery.refetch()
    }
  }

  return (
    <section className="settings-push" aria-label={t('settings.pushLandmark')}>
      <button
        type="button"
        role="switch"
        className="settings-push__switch"
        aria-checked={subscribed}
        aria-labelledby={titleId}
        aria-describedby={noteId}
        aria-busy={busy}
        disabled={busy || !state || blocked !== null}
        onClick={() => void onToggle()}
      >
        <span className="settings-push__icon" aria-hidden="true">
          <BellRingIcon />
        </span>
        <span className="settings-push__copy">
          <strong id={titleId}>{t('settings.pushTitle')}</strong>
          <small id={noteId}>{note}</small>
        </span>
        <span className="settings-push__track" aria-hidden="true">
          <span />
        </span>
      </button>
      {error ? (
        <p className="settings-push__error tone-danger" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}
