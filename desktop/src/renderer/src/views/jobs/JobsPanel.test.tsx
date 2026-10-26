import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STOPPED_GATEWAY } from '@shared/gateway'
import { useGateway } from '~/stores/gateway'
import { useUi } from '~/stores/ui'
import { JobsPanel } from './JobsPanel'

type Handler = (...args: unknown[]) => void
const handlers = new Map<string, Set<Handler>>()
const rpc = {
  call: vi.fn(async (method: string) => (method === 'cron.list' ? [] : {})),
  waitForConnection: vi.fn(async () => {}),
  on: (event: string, handler: Handler) => {
    if (!handlers.has(event)) handlers.set(event, new Set())
    handlers.get(event)!.add(handler)
    return () => handlers.get(event)?.delete(handler)
  },
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const listCalls = () => rpc.call.mock.calls.filter(([m]) => m === 'cron.list').length

beforeEach(() => {
  handlers.clear()
  rpc.call.mockClear()
  useUi.setState({ jobsOpen: true })
  useGateway.setState({ status: { ...STOPPED_GATEWAY, state: 'running' } })
})
afterEach(() => useUi.setState({ jobsOpen: false }))

describe('JobsPanel', () => {
  it('reads the list on a finished run, then once more when its health is recorded', async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <JobsPanel />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(listCalls()).toBe(1))
    handlers.get('cron.run.finished')?.forEach((h) => h({ jobId: 'j1', success: false }))
    await waitFor(() => expect(listCalls()).toBe(2))
    // The event precedes the run record; one settle read follows it.
    await waitFor(() => expect(listCalls()).toBe(3), { timeout: 3000 })
  })
})
