import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatPage } from './ChatPage'
import { KeyboardShortcutProvider } from '@/components/KeyboardShortcuts'

// Issue #3520: the plan card's Approve turned plan mode off on the gateway but
// never refreshed `plan.mode.get`, so the Toolbar's plan toggle kept showing
// plan mode on until something else re-read it.

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const KEY = 'agent:main:webchat:default'
type Handler = (...args: unknown[]) => void

// The gateway's plan flag, as `plan.mode.set` writes it and `plan.mode.get`
// reads it back.
let planMode = true
let failSet = false
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(async (method: string, params?: { key?: string; mode?: string }) => {
      if (method === 'commands.list_for_surface') return { surface: 'web_chat', commands: [] }
      if (method === 'chat.history') return { messages: [], history_scope: 'complete' }
      if (method === 'plan.mode.set') {
        if (failSet) throw new Error('gateway busy')
        planMode = params?.mode === 'on'
        return { key: params?.key, planMode }
      }
      if (method === 'plan.mode.get') return { key: params?.key, planMode }
      return {}
    }),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
      listeners.get('*')?.forEach((h) => h(event, ...args))
    },
  }
}
let mockRpc = makeRpc()

vi.mock('@/app/providers', () => ({
  useRpc: () => mockRpc,
  useBootstrap: () => ({
    version: '1',
    ws_url: 'ws://127.0.0.1:18791/ws',
    auth_mode: 'none',
    base_path: '/control',
    features: {},
  }),
}))

// The Toolbar's plan toggle reads this same query; standing it up next to the
// page shows whether the toggle would follow the approval.
function PlanPill() {
  const { data } = useQuery<{ planMode?: boolean }>({
    queryKey: ['plan.mode.get', KEY],
    queryFn: () => mockRpc.call('plan.mode.get', { key: KEY }),
    staleTime: Infinity,
  })
  return <div data-testid="plan-pill">{data?.planMode ? 'on' : 'off'}</div>
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/chat']}>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <KeyboardShortcutProvider>
          <PlanPill />
          <ChatPage />
        </KeyboardShortcutProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function presentPlan() {
  renderPage()
  const thread = document.querySelector('.chat-thread') as HTMLElement
  await waitFor(() => expect(thread).toHaveAttribute('data-history-ready', 'true'))
  await waitFor(() => expect(screen.getByTestId('plan-pill')).toHaveTextContent('on'))
  await act(async () => {
    mockRpc.emit(
      'session.event.tool_use_start',
      { key: KEY, tool_use_id: 'plan-1', name: 'exit_plan_mode', input: {} },
      {},
    )
    mockRpc.emit(
      'session.event.tool_result',
      {
        key: KEY,
        tool_use_id: 'plan-1',
        name: 'exit_plan_mode',
        result: JSON.stringify({ status: 'plan_presented', plan: '1. Read\n2. Write' }),
      },
      {},
    )
    // The gateway ends the turn once the plan is presented; Approve is only
    // taken while nothing is streaming.
    mockRpc.emit('session.event.done', { key: KEY, stream_seq: 3, text: '' }, {})
  })
  // A loaded CI runner can take longer than findBy's 1 s default to render it.
  return screen.findByRole('button', { name: /approve plan/i }, { timeout: 5000 })
}

beforeEach(() => {
  planMode = true
  failSet = false
  mockRpc = makeRpc()
})

describe('ChatPage · plan card Approve', () => {
  it('turns plan mode off and the plan toggle follows', async () => {
    const approve = await presentPlan()
    await act(async () => {
      fireEvent.click(approve)
    })

    expect(mockRpc.call).toHaveBeenCalledWith('plan.mode.set', { key: KEY, mode: 'off' })
    await waitFor(() => expect(screen.getByTestId('plan-pill')).toHaveTextContent('off'))
  })

  it('re-reads the real flag when the gateway refuses', async () => {
    failSet = true
    const approve = await presentPlan()
    const reads = () => mockRpc.call.mock.calls.filter(([m]) => m === 'plan.mode.get').length
    const before = reads()
    await act(async () => {
      fireEvent.click(approve)
    })

    await waitFor(() => expect(reads()).toBeGreaterThan(before))
    expect(screen.getByTestId('plan-pill')).toHaveTextContent('on')
  })
})
