import { describe, expect, it, vi } from 'vitest'
import { RpcError } from '@/lib/ws-rpc'
import {
  decodeBase64Url,
  disableWebPush,
  enableWebPush,
  pushSupport,
  readWebPushState,
  serviceWorkerScriptUrl,
  WebPushError,
  type WebPushEnv,
} from './web-push'

const SCRIPT = 'https://gw.example/control/static/dist/sw.js'
// An uncompressed P-256 point is 65 bytes starting 0x04.
const KEY_BYTES = Uint8Array.from({ length: 65 }, (_, i) => (i === 0 ? 4 : (i * 37) % 256))
const KEY = btoa(String.fromCharCode(...KEY_BYTES))
  .replace(/\+/g, '-')
  .replace(/\//g, '_')
  .replace(/=+$/, '')

function rpcError(code: string): RpcError {
  const error = new RpcError(code)
  error.code = code
  return error
}

interface FakeSubscription {
  endpoint: string
  options: { applicationServerKey: ArrayBuffer | null }
  toJSON: () => PushSubscriptionJSON
  unsubscribe: ReturnType<typeof vi.fn>
}

function fakeBrowser(
  opts: {
    subscribedWith?: Uint8Array | null
    permission?: NotificationPermission
    grant?: NotificationPermission
    registered?: boolean
  } = {},
) {
  let current: FakeSubscription | null = null
  const order: string[] = []
  const makeSubscription = (key: Uint8Array): FakeSubscription => {
    const endpoint = `https://push.example/${current ? 'second' : 'first'}`
    const subscription: FakeSubscription = {
      endpoint,
      options: { applicationServerKey: key.slice().buffer },
      toJSON: () => ({ endpoint, keys: { p256dh: 'P256DH', auth: 'AUTH' } }),
      unsubscribe: vi.fn(async () => {
        if (current === subscription) current = null
        return true
      }),
    }
    return subscription
  }
  if (opts.subscribedWith) current = makeSubscription(opts.subscribedWith)
  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn(async (options: PushSubscriptionOptionsInit) => {
      order.push('subscribe')
      current = makeSubscription(options.applicationServerKey as Uint8Array)
      return current
    }),
  }
  const registration = { active: {}, installing: null, waiting: null, pushManager }
  let registered = opts.registered ?? Boolean(opts.subscribedWith)
  const serviceWorker = {
    register: vi.fn(async () => {
      order.push('register')
      registered = true
      return registration
    }),
    getRegistration: vi.fn(async () => (registered ? registration : undefined)),
  }
  const notification = {
    permission: opts.permission ?? 'default',
    requestPermission: vi.fn(async () => {
      order.push('permission')
      return opts.grant ?? 'granted'
    }),
  }
  const env = {
    isSecureContext: true,
    serviceWorker,
    hasPushManager: true,
    notification,
    scriptUrl: SCRIPT,
  } as unknown as WebPushEnv
  const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    order.push(method)
    if (method === 'push.web.vapid_public_key') return { publicKey: KEY, enabled: true }
    if (method === 'push.web.unsubscribe') return { ok: true, removed: Boolean(params) }
    return { ok: true }
  })
  return {
    env,
    call,
    order,
    pushManager,
    serviceWorker,
    notification,
    current: () => current,
  }
}

describe('pushSupport', () => {
  it('asks for a secure page before anything else', () => {
    const { env } = fakeBrowser()
    expect(pushSupport({ ...env, isSecureContext: false })).toBe('insecure')
    expect(pushSupport({ ...env, serviceWorker: null })).toBe('unsupported')
    expect(pushSupport({ ...env, hasPushManager: false })).toBe('unsupported')
    expect(pushSupport({ ...env, notification: null })).toBe('unsupported')
    expect(pushSupport(env)).toBe('supported')
  })
})

describe('decodeBase64Url', () => {
  it('decodes an unpadded base64url key', () => {
    expect(Array.from(decodeBase64Url(KEY))).toEqual(Array.from(KEY_BYTES))
    expect(Array.from(decodeBase64Url('-_8'))).toEqual([0xfb, 0xff])
  })
})

describe('serviceWorkerScriptUrl', () => {
  it('sits next to the production bundle the gateway serves', () => {
    expect(serviceWorkerScriptUrl('./', 'https://gw.example/control/static/dist/')).toBe(
      'https://gw.example/control/static/dist/sw.js',
    )
    expect(serviceWorkerScriptUrl('./', 'https://gw.example/static/dist/')).toBe(
      'https://gw.example/static/dist/sw.js',
    )
  })

  it('uses the dev server base on any deep link', () => {
    expect(serviceWorkerScriptUrl('/control/', 'http://localhost:5173/control/chat/x')).toBe(
      'http://localhost:5173/control/sw.js',
    )
  })
})

describe('readWebPushState', () => {
  it('stops at an unsupported browser without asking the gateway', async () => {
    const { env, call } = fakeBrowser()
    await expect(readWebPushState({ ...env, isSecureContext: false }, call)).resolves.toEqual({
      support: 'insecure',
      gatewayEnabled: null,
      permission: 'default',
      subscribed: false,
    })
    expect(call).not.toHaveBeenCalled()
  })

  it("reads the gateway's switch and this browser's subscription", async () => {
    const { env, call, serviceWorker } = fakeBrowser({
      subscribedWith: KEY_BYTES,
      permission: 'granted',
    })
    await expect(readWebPushState(env, call)).resolves.toEqual({
      support: 'supported',
      gatewayEnabled: true,
      permission: 'granted',
      subscribed: true,
    })
    expect(serviceWorker.getRegistration).toHaveBeenCalledWith(SCRIPT)
  })

  it('reports a gateway without Web Push', async () => {
    const { env } = fakeBrowser()
    const call = vi.fn().mockRejectedValue(rpcError('METHOD_NOT_FOUND'))
    await expect(readWebPushState(env, call)).resolves.toMatchObject({
      gatewayEnabled: null,
      subscribed: false,
    })
  })

  it('reports a gateway that turned it off', async () => {
    const { env } = fakeBrowser()
    const call = vi.fn().mockResolvedValue({ publicKey: KEY, enabled: false })
    await expect(readWebPushState(env, call)).resolves.toMatchObject({ gatewayEnabled: false })
  })
})

describe('enableWebPush', () => {
  it('asks permission first, then registers, subscribes and tells the gateway', async () => {
    const browser = fakeBrowser()
    await enableWebPush(browser.env, browser.call)

    // The prompt needs the click's gesture, so it precedes every await on the network.
    expect(browser.order).toEqual([
      'permission',
      'push.web.vapid_public_key',
      'register',
      'subscribe',
      'push.web.subscribe',
    ])
    expect(browser.serviceWorker.register).toHaveBeenCalledWith(SCRIPT)
    const options = browser.pushManager.subscribe.mock.calls[0]?.[0] as PushSubscriptionOptionsInit
    expect(options.userVisibleOnly).toBe(true)
    expect(Array.from(options.applicationServerKey as Uint8Array)).toEqual(Array.from(KEY_BYTES))
    expect(browser.call).toHaveBeenLastCalledWith('push.web.subscribe', {
      endpoint: 'https://push.example/first',
      keys: { p256dh: 'P256DH', auth: 'AUTH' },
    })
  })

  it('stops when the user declines notifications', async () => {
    const browser = fakeBrowser({ grant: 'denied' })
    await expect(enableWebPush(browser.env, browser.call)).rejects.toEqual(
      new WebPushError('denied'),
    )
    expect(browser.serviceWorker.register).not.toHaveBeenCalled()
    expect(browser.call).not.toHaveBeenCalled()
  })

  it('stops when the gateway has push turned off', async () => {
    const browser = fakeBrowser()
    browser.call.mockResolvedValueOnce({ publicKey: KEY, enabled: false })
    await expect(enableWebPush(browser.env, browser.call)).rejects.toMatchObject({
      reason: 'disabled',
    })
    expect(browser.serviceWorker.register).not.toHaveBeenCalled()
  })

  it('reuses a subscription made for the same key', async () => {
    const browser = fakeBrowser({ subscribedWith: KEY_BYTES })
    await enableWebPush(browser.env, browser.call)
    expect(browser.pushManager.subscribe).not.toHaveBeenCalled()
    expect(browser.call).toHaveBeenLastCalledWith(
      'push.web.subscribe',
      expect.objectContaining({ endpoint: 'https://push.example/first' }),
    )
  })

  it('replaces a subscription made for another key', async () => {
    const browser = fakeBrowser({ subscribedWith: new Uint8Array(65) })
    const stale = browser.current()
    await enableWebPush(browser.env, browser.call)
    expect(stale?.unsubscribe).toHaveBeenCalledTimes(1)
    expect(browser.pushManager.subscribe).toHaveBeenCalledTimes(1)
  })

  it('undoes the browser subscription when the gateway refuses it', async () => {
    const browser = fakeBrowser()
    browser.call.mockImplementation(async (method: string) => {
      if (method === 'push.web.vapid_public_key') return { publicKey: KEY, enabled: true }
      throw rpcError('INVALID_REQUEST')
    })
    await expect(enableWebPush(browser.env, browser.call)).rejects.toMatchObject({
      code: 'INVALID_REQUEST',
    })
    expect(browser.current()).toBeNull()
  })

  it('waits for a freshly installed worker to activate', async () => {
    const browser = fakeBrowser()
    const worker = new EventTarget() as EventTarget & { state: string }
    worker.state = 'installing'
    const registration = {
      active: null,
      installing: worker,
      waiting: null,
      pushManager: browser.pushManager,
    }
    browser.serviceWorker.register.mockResolvedValueOnce(registration as never)

    const done = enableWebPush(browser.env, browser.call)
    await vi.waitFor(() => expect(browser.serviceWorker.register).toHaveBeenCalled())
    expect(browser.pushManager.subscribe).not.toHaveBeenCalled()
    worker.state = 'activated'
    worker.dispatchEvent(new Event('statechange'))
    await done
    expect(browser.pushManager.subscribe).toHaveBeenCalledTimes(1)
  })
})

describe('disableWebPush', () => {
  it('unsubscribes this browser, then tells the gateway', async () => {
    const browser = fakeBrowser({ subscribedWith: KEY_BYTES })
    const subscription = browser.current()
    await disableWebPush(browser.env, browser.call)
    expect(subscription?.unsubscribe).toHaveBeenCalledTimes(1)
    expect(browser.call).toHaveBeenCalledWith('push.web.unsubscribe', {
      endpoint: 'https://push.example/first',
    })
  })

  it('stays off locally even when the gateway cannot be told', async () => {
    const browser = fakeBrowser({ subscribedWith: KEY_BYTES })
    browser.call.mockRejectedValue(new Error('Connection closed'))
    await expect(disableWebPush(browser.env, browser.call)).resolves.toBeUndefined()
    expect(browser.current()).toBeNull()
  })

  it('does nothing without a subscription', async () => {
    const browser = fakeBrowser()
    await disableWebPush(browser.env, browser.call)
    expect(browser.call).not.toHaveBeenCalled()
  })
})
