import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { InboxPage } from './InboxPage'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useDismissedIssues } from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { approvalMonitor, useApprovals } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }))

vi.mock('@/services/approval-monitor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/approval-monitor')>()
  return {
    ...actual,
    approvalMonitor: {
      resolve: vi.fn().mockResolvedValue(undefined),
      pollNow: vi.fn().mockResolvedValue(undefined),
    },
  }
})

const mockRpc = { call: vi.fn().mockResolvedValue({}), waitForConnection: vi.fn() }
vi.mock('@/app/providers', () => ({ useRpc: () => mockRpc }))

function Where() {
  const location = useLocation()
  return <div data-testid="where">{location.pathname + location.search}</div>
}

function renderInbox() {
  return render(
    <MemoryRouter initialEntries={['/inbox']}>
      <Routes>
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

const NOW = Date.now()

beforeEach(() => {
  mockRpc.call.mockReset().mockResolvedValue({})
  vi.mocked(approvalMonitor.resolve).mockReset().mockResolvedValue(undefined)
  useConnection.getState().setState('connected')
  useRuntimeSignals.getState().reset()
  useRuntimeSignals.setState({
    everConnected: true,
    lastFrameAt: NOW,
    doctor: {
      status: 'degraded',
      ready: true,
      counts: { error: 1 },
      findings: [
        {
          id: 'channel.telegram.dead',
          severity: 'error',
          title: 'Channel telegram is dead',
          detail: 'The configured channel is not able to receive or send messages.',
          fixSteps: [
            { label: 'Restart channel', command: 'agentos channels restart telegram --yes' },
          ],
        },
      ],
      fetchedAt: NOW,
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
        presentedAt: NOW,
        questions: [
          {
            question: 'Which branch?',
            options: [{ label: 'main' }, { label: 'dev' }],
            multiSelect: false,
          },
        ],
      },
    ],
  })
})

afterEach(() => {
  useApprovals.setState({ pending: [], count: 0 })
  usePendingQuestions.getState().reset()
  useDismissedIssues.getState().restoreAll()
  useRuntimeSignals.getState().reset()
})

describe('InboxPage', () => {
  it('lists approvals, questions and runtime issues in one place', () => {
    renderInbox()
    expect(screen.getByRole('heading', { name: 'Attention inbox' })).toBeInTheDocument()
    expect(screen.getByTestId('inbox-stat-approvals')).toHaveTextContent('2')
    expect(screen.getByTestId('inbox-stat-questions')).toHaveTextContent('1')
    expect(screen.getByTestId('inbox-stat-issues')).toHaveTextContent('1')
    const kinds = screen.getAllByRole('article').map((el) => el.getAttribute('data-kind'))
    expect(kinds).toEqual(['approval', 'question', 'issue'])
  })

  it('groups the same approval across sessions and answers each in place', async () => {
    renderInbox()
    const card = screen.getByRole('article', { name: /Approval: bash/ })
    expect(within(card).getByTestId('inbox-seen-in')).toHaveTextContent('Seen in 2 sessions')
    const rows = card.querySelectorAll('[data-member-id]')
    expect(rows).toHaveLength(2)
    fireEvent.click(within(rows[1] as HTMLElement).getByRole('button', { name: 'Deny' }))
    await waitFor(() =>
      expect(approvalMonitor.resolve).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'a2' }),
        'deny',
      ),
    )
    fireEvent.click(within(rows[0] as HTMLElement).getByRole('button', { name: 'Approve once' }))
    await waitFor(() =>
      expect(approvalMonitor.resolve).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'a1' }),
        'once',
      ),
    )
  })

  it('deep-links an occurrence into its session', () => {
    renderInbox()
    fireEvent.click(screen.getByRole('button', { name: 's1' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/chat?session=s1')
  })

  it('answers a question with an option chip through chat.send and clears it', async () => {
    renderInbox()
    const card = screen.getByRole('article', { name: /Question: Which branch\?/ })
    const send = within(card).getByRole('button', { name: 'Send answer' })
    expect(send).toBeDisabled()
    fireEvent.click(within(card).getByRole('button', { name: 'dev' }))
    expect(within(card).getByRole('button', { name: 'dev' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    fireEvent.click(send)
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('chat.send', { message: 'dev', sessionKey: 's3' }),
    )
    await waitFor(() => expect(usePendingQuestions.getState().items).toEqual([]))
  })

  it('answers with free text', async () => {
    renderInbox()
    const card = screen.getByRole('article', { name: /Question: Which branch\?/ })
    fireEvent.change(within(card).getByPlaceholderText('Type a reply…'), {
      target: { value: 'feature/x please' },
    })
    fireEvent.click(within(card).getByRole('button', { name: 'Send answer' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('chat.send', {
        message: 'feature/x please',
        sessionKey: 's3',
      }),
    )
  })

  it('opens an issue’s fix steps in place and dismisses it for the session', () => {
    renderInbox()
    const card = screen.getByRole('article', { name: /Runtime issue: Channel telegram is dead/ })
    expect(card).toHaveAttribute('data-issue-kind', 'channel_terminal')
    // The web console cannot restart the process, so it never offers to.
    expect(within(card).queryByRole('button', { name: 'Restart gateway' })).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: 'Open doctor fix steps' }))
    expect(within(card).getByText('agentos channels restart telegram --yes')).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('article', { name: /Channel telegram is dead/ })).toBeNull()
    expect(screen.getByText('1 issue dismissed for this session.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show them again' }))
    expect(screen.getByRole('article', { name: /Channel telegram is dead/ })).toBeInTheDocument()
  })

  it('keeps answers visible but disabled, with the reason, while offline', () => {
    renderInbox()
    act(() => useConnection.getState().setState('disconnected'))
    const card = screen.getByRole('article', { name: /Approval: bash/ })
    for (const button of within(card).getAllByRole('button', { name: /Approve once|Deny/ })) {
      expect(button).toBeDisabled()
    }
    expect(
      within(card).getAllByText('The gateway is offline. Reconnect to use this.').length,
    ).toBeGreaterThan(0)
    // Offline, the doctor's last report is not repeated as current; the lost
    // connection is the issue.
    expect(screen.queryByRole('article', { name: /Channel telegram is dead/ })).toBeNull()
    expect(
      screen.getByRole('article', { name: /Runtime issue: Gateway unreachable/ }),
    ).toBeInTheDocument()
  })

  it('says so when nothing needs you', () => {
    useApprovals.setState({ pending: [], count: 0 })
    usePendingQuestions.getState().reset()
    useRuntimeSignals.setState({
      doctor: { status: 'ready', ready: true, counts: {}, findings: [], fetchedAt: NOW },
    })
    renderInbox()
    expect(screen.getByText('Nothing needs you')).toBeInTheDocument()
  })

  it('does not claim an empty list means nothing waits when it is not live', () => {
    useApprovals.setState({ pending: [], count: 0 })
    usePendingQuestions.getState().reset()
    useRuntimeSignals.setState({ everConnected: false, lastFrameAt: null, doctor: null })
    useConnection.getState().setState('connecting')
    renderInbox()
    expect(screen.queryByText('Nothing needs you')).toBeNull()
    expect(screen.getByText('Can’t tell what needs you')).toBeInTheDocument()
  })
})
