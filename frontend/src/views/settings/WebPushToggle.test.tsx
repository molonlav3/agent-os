import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WebPushEnv } from '@/services/web-push'
import { WebPushToggle } from './WebPushToggle'

const mockRpc = {
  waitForConnection: vi.fn().mockResolvedValue(undefined),
  call: vi.fn(),
}
vi.mock('@/app/providers', () => ({ useRpc: () => mockRpc }))

function fakeEnv(opts: { subscribed?: boolean; grant?: NotificationPermission } = {}) {
  let subscription: { endpoint: string; unsubscribe: () => Promise<boolean> } | null = null
  const makeSubscription = () => {
    const created = {
      endpoint: 'https://push.example/e1',
      options: { applicationServerKey: null },
      toJSON: () => ({ endpoint: 'https://push.example/e1', keys: { p256dh: 'p', auth: 'a' } }),
      unsubscribe: async () => {
        subscription = null
        return true
      },
    }
    return created
  }
  if (opts.subscribed) subscription = makeSubscription()
  const registration = {
    active: {},
    pushManager: {
      getSubscription: async () => subscription,
      subscribe: async () => (subscription = makeSubscription()),
    },
  }
  return {
    isSecureContext: true,
    hasPushManager: true,
    scriptUrl: 'https://gw.example/control/static/dist/sw.js',
    serviceWorker: {
      register: async () => registration,
      getRegistration: async () => registration,
    },
    notification: {
      permission: 'default',
      requestPermission: async () => opts.grant ?? 'granted',
    },
  } as unknown as WebPushEnv
}

function renderToggle(env: WebPushEnv) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <WebPushToggle env={env} />
    </QueryClientProvider>,
  )
}

function gatewayAnswers(enabled: boolean) {
  mockRpc.call.mockImplementation(async (method: string) => {
    if (method === 'push.web.vapid_public_key') return { publicKey: 'BAAA', enabled }
    return { ok: true, removed: true }
  })
}

describe('WebPushToggle', () => {
  beforeEach(() => {
    mockRpc.call.mockReset()
    gatewayAnswers(true)
  })

  it('explains that push needs a secure page', async () => {
    renderToggle({ ...fakeEnv(), isSecureContext: false })
    const toggle = screen.getByRole('switch', { name: 'Push notifications for approvals' })
    await waitFor(() =>
      expect(toggle).toHaveAccessibleDescription(
        'Needs a secure page: open the console over HTTPS or on localhost',
      ),
    )
    expect(toggle).toBeDisabled()
    expect(mockRpc.call).not.toHaveBeenCalled()
  })

  it('stays disabled while the gateway has push turned off', async () => {
    gatewayAnswers(false)
    renderToggle(fakeEnv())
    const toggle = screen.getByRole('switch')
    await waitFor(() =>
      expect(toggle).toHaveAccessibleDescription(
        'Turned off on the gateway ([push] web_enabled = false)',
      ),
    )
    expect(toggle).toBeDisabled()
  })

  it('turns push on and off for this browser', async () => {
    renderToggle(fakeEnv())
    const toggle = screen.getByRole('switch')
    await waitFor(() => expect(toggle).toBeEnabled())
    expect(toggle).toHaveAttribute('aria-checked', 'false')

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(mockRpc.call).toHaveBeenCalledWith('push.web.subscribe', {
      endpoint: 'https://push.example/e1',
      keys: { p256dh: 'p', auth: 'a' },
    })
    expect(toggle).toHaveAccessibleDescription('On for this browser')

    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    expect(mockRpc.call).toHaveBeenCalledWith('push.web.unsubscribe', {
      endpoint: 'https://push.example/e1',
    })
  })

  it('says so when notifications are declined', async () => {
    renderToggle(fakeEnv({ grant: 'denied' }))
    const toggle = screen.getByRole('switch')
    await waitFor(() => expect(toggle).toBeEnabled())
    fireEvent.click(toggle)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Notifications are blocked for this site in the browser settings',
    )
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('lets an existing subscription be turned off even with push off on the gateway', async () => {
    gatewayAnswers(false)
    renderToggle(fakeEnv({ subscribed: true }))
    const toggle = screen.getByRole('switch')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    expect(toggle).toBeEnabled()
  })
})
