/**
 * Web Push for approval prompts: the browser half of the gateway's
 * `push.web.*` RPCs, behind Settings → "Push notifications for approvals".
 *
 * Turning it on asks for notification permission, registers the service
 * worker (`public/sw.js`), subscribes its push manager with the gateway's
 * VAPID key and hands the subscription to the gateway. The browser APIs are
 * injected (`WebPushEnv`) so the flow runs under test without a push service.
 * A notification click is routed by `web-push-navigation.ts`, which the
 * console shell loads on its own so this module stays out of the entry chunk.
 */
export type RpcCall = (method: string, params?: Record<string, unknown>) => Promise<unknown>

export interface WebPushEnv {
  isSecureContext: boolean
  serviceWorker: Pick<ServiceWorkerContainer, 'register' | 'getRegistration'> | null
  hasPushManager: boolean
  notification: {
    readonly permission: NotificationPermission
    requestPermission: () => Promise<NotificationPermission>
  } | null
  /** Where `public/sw.js` is served: `{controlBase}/static/dist/sw.js`. */
  scriptUrl: string
}

/** `insecure`: push needs HTTPS or localhost. `unsupported`: this browser has none. */
export type PushSupport = 'supported' | 'insecure' | 'unsupported'

export interface WebPushState {
  support: PushSupport
  /** The gateway's `[push] web_enabled`; null for a gateway without Web Push. */
  gatewayEnabled: boolean | null
  permission: NotificationPermission
  /** This browser holds a push subscription. */
  subscribed: boolean
}

export type WebPushFailure = 'denied' | 'disabled' | 'unsupported'

/** A refusal the toggle explains in its own words, not as a raw error. */
export class WebPushError extends Error {
  readonly reason: WebPushFailure
  constructor(reason: WebPushFailure) {
    super(`web push ${reason}`)
    this.reason = reason
  }
}

function errorCode(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code
  return typeof code === 'string' ? code : ''
}

export function pushSupport(env: WebPushEnv): PushSupport {
  // Checked first: an insecure page has no service worker either, and the
  // fix there is the address, not the browser.
  if (!env.isSecureContext) return 'insecure'
  if (!env.serviceWorker || !env.hasPushManager || !env.notification) return 'unsupported'
  return 'supported'
}

/** The applicationServerKey bytes of a base64url VAPID public key. */
export function decodeBase64Url(value: string): Uint8Array<ArrayBuffer> {
  const base64 = value.trim().replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

function sameBytes(a: ArrayBuffer | null, b: Uint8Array): boolean {
  if (!a || a.byteLength !== b.byteLength) return false
  const view = new Uint8Array(a)
  return view.every((byte, i) => byte === b[i])
}

/**
 * The service worker script next to the built bundle. Production resolves the
 * relative build base against the gateway's `<base href>`; the dev server
 * serves `public/` under its own base.
 */
export function serviceWorkerScriptUrl(
  buildBase: string = import.meta.env.BASE_URL,
  documentBase: string = document.baseURI,
): string {
  return new URL('sw.js', new URL(buildBase, documentBase)).href
}

export function browserWebPushEnv(): WebPushEnv {
  const hasWindow = typeof window !== 'undefined'
  const nav = typeof navigator !== 'undefined' ? navigator : null
  return {
    isSecureContext: hasWindow && window.isSecureContext === true,
    serviceWorker: nav && 'serviceWorker' in nav ? nav.serviceWorker : null,
    hasPushManager: hasWindow && 'PushManager' in window,
    notification: typeof Notification !== 'undefined' ? Notification : null,
    scriptUrl: serviceWorkerScriptUrl(),
  }
}

async function currentSubscription(env: WebPushEnv): Promise<PushSubscription | null> {
  const registration = await env.serviceWorker?.getRegistration(env.scriptUrl)
  return (await registration?.pushManager.getSubscription()) ?? null
}

/** The gateway's key, or null for a gateway without Web Push. */
async function vapidKey(call: RpcCall): Promise<{ publicKey: string; enabled: boolean } | null> {
  try {
    const res = (await call('push.web.vapid_public_key', {})) as {
      publicKey?: unknown
      enabled?: unknown
    } | null
    return {
      publicKey: typeof res?.publicKey === 'string' ? res.publicKey : '',
      enabled: res?.enabled === true,
    }
  } catch (error) {
    if (errorCode(error) === 'METHOD_NOT_FOUND') return null
    throw error
  }
}

export async function readWebPushState(env: WebPushEnv, call: RpcCall): Promise<WebPushState> {
  const support = pushSupport(env)
  if (support !== 'supported') {
    return { support, gatewayEnabled: null, permission: 'default', subscribed: false }
  }
  const [key, subscription] = await Promise.all([vapidKey(call), currentSubscription(env)])
  return {
    support,
    gatewayEnabled: key ? key.enabled : null,
    permission: env.notification?.permission ?? 'default',
    subscribed: subscription !== null,
  }
}

/** Resolves once `registration` has an active worker (subscribe needs one). */
function activated(registration: ServiceWorkerRegistration): Promise<void> {
  if (registration.active) return Promise.resolve()
  const worker = registration.installing ?? registration.waiting
  if (!worker) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const onChange = (): void => {
      if (worker.state === 'activated') {
        worker.removeEventListener('statechange', onChange)
        resolve()
      } else if (worker.state === 'redundant') {
        worker.removeEventListener('statechange', onChange)
        reject(new WebPushError('unsupported'))
      }
    }
    worker.addEventListener('statechange', onChange)
  })
}

/**
 * Turn push on for this browser. Call it straight from the click: browsers
 * only show the permission prompt in response to a user gesture, so that
 * comes before anything that waits on the network.
 */
export async function enableWebPush(env: WebPushEnv, call: RpcCall): Promise<void> {
  if (pushSupport(env) !== 'supported' || !env.serviceWorker || !env.notification) {
    throw new WebPushError('unsupported')
  }
  const permission = await env.notification.requestPermission()
  if (permission !== 'granted') throw new WebPushError('denied')

  const key = await vapidKey(call)
  if (!key || !key.enabled || !key.publicKey) throw new WebPushError('disabled')
  const applicationServerKey = decodeBase64Url(key.publicKey)

  const registration = await env.serviceWorker.register(env.scriptUrl)
  await activated(registration)
  let subscription = await registration.pushManager.getSubscription()
  // A subscription made for another key (the gateway's was regenerated)
  // would never be delivered to.
  if (subscription && !sameBytes(subscription.options.applicationServerKey, applicationServerKey)) {
    await subscription.unsubscribe()
    subscription = null
  }
  subscription ??= await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  })

  const json = subscription.toJSON()
  try {
    await call('push.web.subscribe', {
      endpoint: json.endpoint ?? subscription.endpoint,
      keys: { p256dh: json.keys?.p256dh ?? '', auth: json.keys?.auth ?? '' },
    })
  } catch (error) {
    // A subscription the gateway never stored would read "on" and never ring.
    await subscription.unsubscribe().catch(() => false)
    throw error
  }
}

/** Turn push off for this browser, then tell the gateway. */
export async function disableWebPush(env: WebPushEnv, call: RpcCall): Promise<void> {
  const subscription = await currentSubscription(env)
  if (!subscription) return
  const endpoint = subscription.endpoint
  await subscription.unsubscribe()
  // The browser side is what stops the notifications, so it goes first. A
  // gateway that misses this keeps a dead endpoint until the push service
  // turns it away.
  await call('push.web.unsubscribe', { endpoint }).catch(() => undefined)
}
