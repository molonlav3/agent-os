import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STOPPED_GATEWAY } from '@shared/gateway'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useDismissedIssues } from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { useAttentionPanel } from '~/stores/attention'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { AttentionPanel } from './AttentionPanel'

const rpc = {
  call: vi.fn(),
  waitForConnection: vi.fn(() => new Promise<void>(() => {})),
  on: () => () => {},
}
vi.mock('@/app/providers', () => ({ useRpc: () => rpc }))

const restart = vi.fn(async () => {})

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/sessions']}>
        <AttentionPanel />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  rpc.call.mockReset().mockResolvedValue({})
  restart.mockClear()
  vi.spyOn(approvalMonitor, 'resolve').mockResolvedValue(undefined)
  useGateway.setState({ status: { ...STOPPED_GATEWAY, state: 'running' }, busy: false, restart })
  useSettings.setState((s) => ({
    settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'managed' } },
  }))
  useConnection.getState().setState('connected')
  useRuntimeSignals.getState().reset()
  useRuntimeSignals.setState({
    everConnected: true,
    lastFrameAt: Date.now(),
    doctor: {
      status: 'degraded',
      ready: true,
      counts: { error: 1 },
      findings: [
        {
          id: 'channel.telegram.dead',
          severity: 'error',
          title: 'Channel telegram is dead',
          fixSteps: [
            { label: 'Restart channel', command: 'agentos channels restart telegram --yes' },
          ],
        },
      ],
      fetchedAt: 1,
    },
  })
  useApprovals.setState({
    pending: [
      { id: 'a1', namespace: 'exec', toolName: 'bash', command: 'make deploy', sessionKey: 's1' },
      { id: 'a2', namespace: 'exec', toolName: 'bash', command: 'make deploy', sessionKey: 's2' },
    ],
    count: 2,
    elevatedMode: '',
  })
  usePendingQuestions.setState({
    items: [
      {
        id: 'question:s3',
        sessionKey: 's3',
        toolUseId: null,
        presentedAt: Date.now(),
        questions: [
          {
            question: 'Ship it?',
            options: [{ label: 'yes' }, { label: 'no' }],
            multiSelect: false,
          },
        ],
      },
    ],
  })
  useAttentionPanel.setState({ open: false })
})

afterEach(() => {
  vi.restoreAllMocks()
  useApprovals.setState({ pending: [], count: 0 })
  usePendingQuestions.getState().reset()
  useDismissedIssues.getState().restoreAll()
  useRuntimeSignals.getState().reset()
})

describe('AttentionPanel (desktop)', () => {
  it('counts everything waiting and lists it grouped', () => {
    renderPanel()
    expect(screen.getByTestId('attention-count')).toHaveTextContent('4')
    fireEvent.click(screen.getByRole('button', { name: 'Needs you (4)' }))
    const pop = screen.getByRole('dialog', { name: 'Needs you' })
    const kinds = within(pop)
      .getAllByRole('article')
      .map((a) => a.getAttribute('data-kind'))
    expect(kinds).toEqual(['approval', 'question', 'issue'])
    expect(within(pop).getByTestId('attention-seen-in')).toHaveTextContent('Seen in 2 sessions')
  })

  it('answers one occurrence of a grouped approval in place', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    const row = document.querySelector('[data-member-id="approval:exec:a2"]') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Approve once' }))
    await waitFor(() =>
      expect(approvalMonitor.resolve).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'a2' }),
        'once',
      ),
    )
  })

  it('answers a question with a chip', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    fireEvent.click(screen.getByRole('button', { name: 'yes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() =>
      expect(rpc.call).toHaveBeenCalledWith('chat.send', { message: 'yes', sessionKey: 's3' }),
    )
  })

  it('restarts the gateway only after the confirmation', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    const issue = screen.getByRole('article', { name: 'Channel telegram is dead' })
    fireEvent.click(within(issue).getByRole('button', { name: 'Restart gateway' }))
    expect(restart).not.toHaveBeenCalled()
    expect(within(issue).getByRole('alert')).toHaveTextContent('Restart the gateway?')
    fireEvent.click(within(issue).getByRole('button', { name: 'Restart' }))
    expect(restart).toHaveBeenCalledTimes(1)
  })

  it('shows fix steps in place and dismisses for the session', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    const issue = screen.getByRole('article', { name: 'Channel telegram is dead' })
    fireEvent.click(within(issue).getByRole('button', { name: 'Open doctor fix steps' }))
    expect(within(issue).getByText('agentos channels restart telegram --yes')).toBeInTheDocument()
    fireEvent.click(within(issue).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('article', { name: 'Channel telegram is dead' })).toBeNull()
  })

  it('keeps answers visible but disabled with the reason while offline', () => {
    useConnection.getState().setState('disconnected')
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    const approval = screen.getAllByRole('article')[0]!
    for (const button of within(approval).getAllByRole('button', { name: /Approve once|Deny/ })) {
      expect(button).toBeDisabled()
    }
    expect(
      within(approval).getByText('The gateway is offline. Reconnect to use this.'),
    ).toBeInTheDocument()
  })

  it('deep-links into the owning session', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Needs you/ }))
    fireEvent.click(screen.getByRole('button', { name: 's1' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/sessions/s1')
    expect(useAttentionPanel.getState().open).toBe(false)
  })
})
