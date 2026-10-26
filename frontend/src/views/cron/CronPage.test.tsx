import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { CronPage } from './CronPage'
import { RpcError } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

const navigateSpy = vi.fn()
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router')
  return { ...actual, useNavigate: () => navigateSpy }
})

// A minimal event-bus stub matching the WsRpcClient surface CronPage uses.
type Handler = (...args: unknown[]) => void
function makeRpc() {
  const listeners = new Map<string, Set<Handler>>()
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn(),
    on: vi.fn((event: string, handler: Handler) => {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event)!.add(handler)
      return () => listeners.get(event)?.delete(handler)
    }),
    emit(event: string, ...args: unknown[]) {
      listeners.get(event)?.forEach((h) => h(...args))
    },
    listenerCount(event: string) {
      return listeners.get(event)?.size ?? 0
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

const FUTURE = new Date(Date.now() + 60 * 60_000).toISOString()

const REMINDER_JOB = {
  id: 'job-rem',
  name: 'Daily standup',
  enabled: true,
  expression: '0 9 * * 1-5',
  payloadKind: 'reminder',
  sessionTarget: 'isolated',
  next_run: FUTURE,
  message: 'time for standup',
  creatorSessionKey: 'agent:main:telegram:standup',
}
const AGENT_JOB = {
  id: 'job-agent',
  name: 'Health check',
  enabled: false,
  expression: '0 * * * *',
  payloadKind: 'agent_turn',
  sessionTarget: 'main',
  last_status: 'ok',
  last_run: Date.now() - 3600_000,
}

const RUNS = [
  {
    started_at: Date.now() - 60_000,
    status: 'ok',
    duration_ms: 120,
    summary: 'ran fine',
    sessionKey: 'agent:main:webchat:x',
  },
]

function wireRpc(
  opts: {
    jobs?: unknown[]
    listReject?: boolean
    updateReject?: boolean
    runReject?: boolean
    runsReject?: boolean
    removeReject?: boolean
    createReject?: boolean
    removePending?: boolean
    createPending?: boolean
    runs?: unknown[]
    runOutput?: string
    runOutputReject?: boolean
  } = {},
) {
  mockRpc.call.mockImplementation((method: string) => {
    switch (method) {
      case 'cron.list':
        return opts.listReject
          ? Promise.reject(new Error('list down'))
          : Promise.resolve(opts.jobs ?? [REMINDER_JOB, AGENT_JOB])
      case 'cron.subscribe':
      case 'cron.unsubscribe':
        return Promise.resolve({})
      case 'cron.create':
        if (opts.createPending) return new Promise(() => undefined)
        return opts.createReject ? Promise.reject(new Error('create failed')) : Promise.resolve({})
      case 'cron.update':
        return opts.updateReject ? Promise.reject(new Error('update failed')) : Promise.resolve({})
      case 'cron.run':
        return opts.runReject
          ? Promise.reject(new Error('run failed'))
          : Promise.resolve({ reply: 'done' })
      case 'cron.runs':
        return opts.runsReject
          ? Promise.reject(new Error('runs down'))
          : Promise.resolve(opts.runs ?? RUNS)
      case 'cron.runOutput':
        return opts.runOutputReject
          ? Promise.reject(new Error('output down'))
          : Promise.resolve({ output: opts.runOutput ?? '' })
      case 'cron.remove':
        if (opts.removePending) return new Promise(() => undefined)
        return opts.removeReject ? Promise.reject(new Error('remove failed')) : Promise.resolve({})
      default:
        return Promise.resolve({})
    }
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <CronPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

function callsTo(method: string) {
  return mockRpc.call.mock.calls.filter(([m]) => m === method).length
}

describe('CronPage', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    useConnection.getState().setState('connected')
    navigateSpy.mockReset()
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.warning).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.info).mockClear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls cron.list after waitForConnection', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('cron.list', {}))
    expect(mockRpc.waitForConnection).toHaveBeenCalled()
  })

  it('renders a card per job with kind pill and schedule', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Daily standup')
    expect(within(card).getByText('Reminder')).toBeInTheDocument()
    expect(within(card).getByText('0 9 * * 1-5')).toBeInTheDocument()
    expect(within(card).getByText('Created from')).toBeInTheDocument()
    expect(within(card).getByText('agent:main:telegram:standup')).toBeInTheDocument()
    // second job renders too
    expect(screen.getByText('Health check')).toBeInTheDocument()
  })

  it('shows the cron id on the card, shortened but recoverable in full', async () => {
    const uuid = '3f9a2b1c-77d0-4e21-9c33-8a1b2c3d9d4e'
    wireRpc({ jobs: [{ ...REMINDER_JOB, id: uuid }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Daily standup')
    expect(within(card).getByText('Cron ID')).toBeInTheDocument()
    // Shortened for the card, full value on the row's title for hover/copy.
    expect(within(card).getByText('3f9a2b1c…9d4e')).toBeInTheDocument()
    expect(within(card).getByTitle(uuid)).toBeInTheDocument()
  })

  it('copies the full cron id and toasts on success', async () => {
    const uuid = '3f9a2b1c-77d0-4e21-9c33-8a1b2c3d9d4e'
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    wireRpc({ jobs: [{ ...REMINDER_JOB, id: uuid }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Daily standup')
    fireEvent.click(within(card).getByRole('button', { name: `Copy cron ID ${uuid}` }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(uuid))
    expect(toast.success).toHaveBeenCalledWith('Copied cron ID', expect.anything())
  })

  it('warns when copying the cron id fails', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    wireRpc({ jobs: [REMINDER_JOB] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Daily standup')
    fireEvent.click(within(card).getByRole('button', { name: 'Copy cron ID job-rem' }))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('Copy failed: denied', expect.anything()),
    )
  })

  it('renders a script job with the Script pill and its script path', async () => {
    wireRpc({
      jobs: [
        {
          id: 'job-script',
          name: 'Memory watchdog',
          enabled: true,
          expression: '*/5 * * * *',
          payloadKind: 'script',
          sessionTarget: 'isolated',
          script: 'watch-memory.sh',
          message: 'watch-memory.sh',
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Memory watchdog')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Memory watchdog')
    expect(within(card).getByText('Script')).toBeInTheDocument()
    expect(within(card).getByText('watch-memory.sh')).toBeInTheDocument()
    expect(within(card).queryByText(/elevated/i)).toBeNull()
  })

  it('shows the elevated badge on the card, without opening the editor', async () => {
    wireRpc({ jobs: [{ ...AGENT_JOB, elevated: 'bypass' }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Health check')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Health check')
    expect(within(card).getByText('Elevated bypass')).toBeInTheDocument()
  })

  it('shows no elevated badge on a default read-only job', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Health check')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Health check')
    expect(within(card).queryByText(/elevated/i)).toBeNull()
  })

  it('reads elevation out of the policy blob when the wire field is absent', async () => {
    wireRpc({ jobs: [{ ...AGENT_JOB, toolPolicy: { elevated: 'full' } }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Health check')).toBeInTheDocument())
    const card = screen.getByLabelText('Cron job Health check')
    expect(within(card).getByText('Elevated full')).toBeInTheDocument()
  })

  it('renders the stat row from the payload', async () => {
    wireRpc()
    renderPage()
    // 2 total jobs, 1 enabled.
    await waitFor(() => expect(screen.getByLabelText('Active schedules')).toHaveTextContent('1'))
  })

  it('groups schedule posture and list controls into the redesigned workspace', async () => {
    wireRpc()
    renderPage()
    const operations = await screen.findByLabelText('Schedule operations')
    expect(within(operations).getByText('Automation clock')).toBeInTheDocument()
    expect(within(operations).getByLabelText('Cron summary')).toBeInTheDocument()
    expect(
      screen.getByRole('searchbox', { name: 'Search jobs' }).closest('.cron-list'),
    ).not.toBeNull()
  })

  it('mounts → cron.subscribe', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('cron.subscribe', {}))
  })

  it('unmounts → cron.unsubscribe and removes the cron.run.finished listener', async () => {
    wireRpc()
    const view = renderPage()
    await waitFor(() => expect(mockRpc.listenerCount('cron.run.finished')).toBe(1))
    view.unmount()
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('cron.unsubscribe', {}))
    expect(mockRpc.listenerCount('cron.run.finished')).toBe(0)
  })

  it('subscribes exactly once across a StrictMode-style double effect and cleans up fully', async () => {
    // Simulate mount → unmount → mount (React 18 StrictMode dev double-invoke):
    // each cleanup must unsubscribe, and no listener may leak across remounts.
    wireRpc()
    const first = renderPage()
    await waitFor(() => expect(mockRpc.listenerCount('cron.run.finished')).toBe(1))
    first.unmount()
    expect(mockRpc.listenerCount('cron.run.finished')).toBe(0)
    const second = renderPage()
    await waitFor(() => expect(mockRpc.listenerCount('cron.run.finished')).toBe(1))
    second.unmount()
    expect(mockRpc.listenerCount('cron.run.finished')).toBe(0)
  })

  it('a cron.run.finished event invalidates the job list (targeted refetch)', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(callsTo('cron.list')).toBe(1))
    mockRpc.emit('cron.run.finished', {})
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
  })

  it('a cron.run.finished event invalidates an open runs drawer', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    // open the runs drawer for the first job
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('cron.runs', { id: 'job-rem', limit: 10 }),
    )
    const before = callsTo('cron.runs')
    mockRpc.emit('cron.run.finished', {})
    await waitFor(() => expect(callsTo('cron.runs')).toBeGreaterThan(before))
  })

  it('toggling enable/disable calls cron.update and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    // Daily standup is enabled → Pause toggles it off.
    fireEvent.click(screen.getByRole('button', { name: /pause daily standup/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('cron.update', { id: 'job-rem', enabled: false }),
    )
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
  })

  it('run-now calls cron.run', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /run daily standup now/i }))
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('cron.run', { id: 'job-rem' }))
  })

  it('opening the runs drawer calls cron.runs and renders history', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('cron.runs', { id: 'job-rem', limit: 10 }),
    )
    expect(await screen.findByText('ran fine')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Run history table' })).toHaveAttribute(
      'tabindex',
      '0',
    )
  })

  it('hides the Chat button for a run whose session was never created', async () => {
    // Script jobs never open a session, and isolated agent sessions are reaped
    // after 24h — the key alone used to send you to "Could not load chat history."
    wireRpc({
      runs: [
        {
          started_at: Date.now(),
          status: 'ok',
          summary: 'script ran',
          sessionKey: 'cron:job-rem:run:deadbeef',
          chatAvailable: false,
        },
        {
          started_at: Date.now() - 60_000,
          status: 'ok',
          summary: 'agent turn',
          sessionKey: 'cron:job-rem:run:cafe1234',
          chatAvailable: true,
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))

    await screen.findByText('script ran')
    expect(screen.getAllByRole('button', { name: '→ Chat' })).toHaveLength(1)
  })

  it('keeps the Chat button when the gateway does not report availability', async () => {
    // An older gateway omits the field; hiding every button would be a regression.
    wireRpc({ runs: [{ started_at: Date.now(), status: 'ok', summary: 'x', sessionKey: 'k' }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))

    expect(await screen.findByRole('button', { name: '→ Chat' })).toBeInTheDocument()
  })

  it('expands a run to show script stdout the cell had to clip', async () => {
    // A script job's stdout is its only trace, and it is routinely multi-line
    // and wider than the cell — the preview must not be the only copy on screen.
    const stdout = 'checked 4 pools\nunilp-3 below floor\n' + 'detail '.repeat(40)
    wireRpc({ runs: [{ started_at: Date.now(), status: 'ok', duration_ms: 8, summary: stdout }] })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))

    const toggle = await screen.findByRole('button', { name: /checked 4 pools/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    // Queried by node, not by text: the stdout is deliberately whitespace-heavy
    // and testing-library's text matcher collapses whitespace.
    expect(document.querySelector('.cron-runs__output')).toBeNull()

    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(document.querySelector('.cron-runs__output')?.textContent).toBe(stdout)

    fireEvent.click(toggle)
    expect(document.querySelector('.cron-runs__output')).toBeNull()
  })

  it('expanding a run fetches the full output the list row only previewed', async () => {
    // cron.runs sends a 500-char preview so a 20-row list stays small; the rest
    // of the output — which is the whole point of opening the row — arrives here.
    const preview = 'checked 4 pools'
    const full = preview + '\n' + 'x'.repeat(4000) + '\n}'
    wireRpc({
      runs: [
        {
          id: 'run-9',
          started_at: Date.now(),
          status: 'ok',
          duration_ms: 8,
          summary: preview,
          summaryTruncated: true,
        },
      ],
      runOutput: full,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))

    const toggle = await screen.findByRole('button', { name: /checked 4 pools/ })
    fireEvent.click(toggle)

    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('cron.runOutput', {
        id: 'job-rem',
        runId: 'run-9',
      }),
    )
    await waitFor(() =>
      expect(document.querySelector('.cron-runs__output')?.textContent).toBe(full),
    )
  })

  it('falls back to the preview when the full output cannot be fetched', async () => {
    const preview = 'checked 4 pools'
    wireRpc({
      runs: [{ id: 'run-9', started_at: Date.now(), status: 'ok', summary: preview }],
      runOutputReject: true,
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Daily standup' }))

    fireEvent.click(await screen.findByRole('button', { name: /checked 4 pools/ }))

    expect(await screen.findByText(/Failed to load full output/)).toBeInTheDocument()
    expect(document.querySelector('.cron-runs__output')?.textContent).toBe(preview)
  })

  it('deleting requires confirmation then calls cron.remove and invalidates', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /delete daily standup/i }))
    // confirm dialog
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^delete$/i }))
    await waitFor(() => expect(mockRpc.call).toHaveBeenCalledWith('cron.remove', { id: 'job-rem' }))
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
  })

  it('cancelling the delete confirmation does not call cron.remove', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /delete daily standup/i }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(callsTo('cron.remove')).toBe(0)
  })

  it('traps delete-confirm focus and restores it to the delete trigger on Escape', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    const trigger = screen.getByRole('button', { name: /delete daily standup/i })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = await screen.findByRole('alertdialog')
    const cancel = within(dialog).getByRole('button', { name: /cancel/i })
    const confirm = within(dialog).getByRole('button', { name: /^delete$/i })
    expect(cancel).toHaveFocus()

    cancel.focus()
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('keeps the delete confirmation open while deletion is pending', async () => {
    wireRpc({ removePending: true })
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /delete daily standup/i }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^delete$/i }))
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /^delete$/i })).toBeDisabled(),
    )

    fireEvent.keyDown(dialog, { key: 'Escape' })
    fireEvent.mouseDown(dialog.parentElement!)
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('refreshes on the Refresh button', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(callsTo('cron.list')).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
  })

  it('shows the empty state when there are no jobs', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
  })

  it('toggles the sort direction and reorders the cards', async () => {
    // Two jobs; sort by name ascending → Daily standup before Health check.
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Sort jobs'), { target: { value: 'name' } })
    const namesAsc = screen
      .getAllByLabelText(/^Cron job /)
      .map((el) => el.getAttribute('aria-label'))
    expect(namesAsc).toEqual(['Cron job Daily standup', 'Cron job Health check'])
    // flip to descending
    fireEvent.click(screen.getByRole('button', { name: /sort direction/i }))
    const namesDesc = screen
      .getAllByLabelText(/^Cron job /)
      .map((el) => el.getAttribute('aria-label'))
    expect(namesDesc).toEqual(['Cron job Health check', 'Cron job Daily standup'])
  })

  it('toasts when cron.list fails', async () => {
    wireRpc({ listReject: true })
    renderPage()
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
  })

  it('sets the document title', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(document.title).toBe('Cron - AgentOS Control'))
  })
})

describe('CronPage — create/edit panel', () => {
  it('traps panel focus and restores it to New job after Escape', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    const trigger = screen.getByRole('button', { name: /new job/i })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = await screen.findByRole('dialog')
    const first = within(dialog).getByLabelText(/^name$/i)
    const last = within(dialog).getByRole('button', { name: /save schedule/i })
    expect(first).toHaveFocus()

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(first).toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('keeps the create panel open while saving is pending', async () => {
    wireRpc({ jobs: [], createPending: true })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), {
      target: { value: 'Standup' },
    })
    fireEvent.change(within(dialog).getByLabelText('Cron expression', { selector: 'input' }), {
      target: { value: '0 9 * * 1-5' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(within(dialog).getByRole('button', { name: /save schedule/i })).toBeDisabled(),
    )

    fireEvent.keyDown(dialog, { key: 'Escape' })
    fireEvent.mouseDown(dialog.parentElement!)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('New job opens the create panel; Save with a name+cron calls cron.create and invalidates', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), { target: { value: 'Standup' } })
    fireEvent.change(within(dialog).getByLabelText('Cron expression', { selector: 'input' }), {
      target: { value: '0 9 * * 1-5' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'cron.create',
        expect.objectContaining({
          name: 'Standup',
          payloadKind: 'reminder',
          schedule: { kind: 'cron', expr: '0 9 * * 1-5' },
        }),
      ),
    )
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
    // panel closes on success
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('blocks submit and toasts when the name is blank (no RPC)', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith('Name is required', expect.anything()),
    )
    expect(callsTo('cron.create')).toBe(0)
  })

  it('edit prefills the form and Save sends the full payload via cron.update {id}', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /edit daily standup/i }))
    const dialog = await screen.findByRole('dialog')
    // prefilled name + cron
    expect(within(dialog).getByLabelText(/^name$/i)).toHaveValue('Daily standup')
    expect(within(dialog).getByLabelText('Cron expression', { selector: 'input' })).toHaveValue(
      '0 9 * * 1-5',
    )
    // change the name and save
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), {
      target: { value: 'Daily standup v2' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'cron.update',
        expect.objectContaining({
          id: 'job-rem',
          name: 'Daily standup v2',
          schedule: { kind: 'cron', expr: '0 9 * * 1-5' },
        }),
      ),
    )
    await waitFor(() => expect(callsTo('cron.list')).toBeGreaterThanOrEqual(2))
  })

  it('switching schedule type to interval validates a positive integer', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), { target: { value: 'Ping' } })
    fireEvent.change(within(dialog).getByLabelText(/schedule type/i), {
      target: { value: 'every' },
    })
    // interval field now visible; 0 is invalid
    fireEvent.change(within(dialog).getByLabelText(/interval/i), { target: { value: '0' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(
        'Interval must be an integer number of seconds',
        expect.anything(),
      ),
    )
    expect(callsTo('cron.create')).toBe(0)
    // a valid interval saves
    fireEvent.change(within(dialog).getByLabelText(/interval/i), { target: { value: '60' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith(
        'cron.create',
        expect.objectContaining({ schedule: { kind: 'every', every_seconds: 60 } }),
      ),
    )
  })

  it('agent-task mode reveals the session-target picker and a named-session key requirement', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), { target: { value: 'Task' } })
    fireEvent.change(within(dialog).getByLabelText('Cron expression', { selector: 'input' }), {
      target: { value: '* * * * *' },
    })
    fireEvent.change(within(dialog).getByLabelText(/job mode/i), {
      target: { value: 'agent_turn' },
    })
    // session target appears; pick "session" (named) with no key → blocked
    fireEvent.change(within(dialog).getByLabelText(/session target/i), {
      target: { value: 'session' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /save schedule/i }))
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(
        'Named session key is required',
        expect.anything(),
      ),
    )
    expect(callsTo('cron.create')).toBe(0)
  })

  it('closes the panel on Cancel without an RPC', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /new job/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^cancel$/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(callsTo('cron.create')).toBe(0)
  })

  it('the empty-state CTA opens the create panel', async () => {
    wireRpc({ jobs: [] })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No schedules yet/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /create your first schedule/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})

describe('CronPage job health and overlap', () => {
  beforeEach(() => {
    mockRpc = makeRpc()
    useConnection.getState().setState('connected')
  })

  it('shows nothing extra when the gateway sends no health or concurrency', async () => {
    wireRpc()
    renderPage()
    await waitFor(() => expect(screen.getByText('Daily standup')).toBeInTheDocument())
    expect(screen.queryByTestId('cron-health')).toBeNull()
    expect(screen.queryByTestId('cron-degraded')).toBeNull()
    expect(screen.queryByRole('combobox', { name: /overlap policy/i })).toBeNull()
  })

  it('marks a degraded job and shows its failures and last error', async () => {
    wireRpc({
      jobs: [
        {
          ...REMINDER_JOB,
          health: { consecutive_failures: 3, last_error: 'boom: provider 500', degraded: true },
        },
      ],
    })
    renderPage()
    const card = await screen.findByRole('article', { name: /daily standup/i })
    expect(within(card).getByTestId('cron-degraded')).toHaveTextContent('Degraded')
    expect(within(card).getByTestId('cron-health')).toHaveTextContent('3 consecutive failures')
    expect(within(card).getByText('boom: provider 500')).toBeInTheDocument()
    expect(card).toHaveAttribute('data-degraded', 'true')
  })

  it('offers the overlap picker only when the job carries concurrency, and saves it', async () => {
    wireRpc({
      jobs: [
        { ...REMINDER_JOB, concurrency: 'forbid', health: { consecutive_failures: 0 } },
        AGENT_JOB,
      ],
    })
    renderPage()
    const picker = await screen.findByRole('combobox', {
      name: /overlap policy for daily standup/i,
    })
    expect(screen.queryByRole('combobox', { name: /overlap policy for health check/i })).toBeNull()
    expect(screen.getByTestId('cron-health')).toHaveTextContent('Healthy')
    fireEvent.change(picker, { target: { value: 'replace' } })
    await waitFor(() =>
      expect(mockRpc.call).toHaveBeenCalledWith('cron.update', {
        id: 'job-rem',
        concurrency: 'replace',
      }),
    )
  })

  it('disables run and pause with the reason while the gateway is offline', async () => {
    wireRpc()
    renderPage()
    const card = await screen.findByRole('article', { name: /daily standup/i })
    useConnection.getState().setState('disconnected')
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: /run daily standup now/i })).toBeDisabled(),
    )
    expect(within(card).getByRole('button', { name: /pause daily standup/i })).toBeDisabled()
    expect(
      within(card).getByText('The gateway is offline. Reconnect to use this.'),
    ).toBeInTheDocument()
  })

  it('disables run and pause once the scheduler answers UNAVAILABLE', async () => {
    wireRpc()
    const base = mockRpc.call.getMockImplementation()!
    mockRpc.call.mockImplementation((method: string, params?: unknown) => {
      if (method === 'cron.run') {
        const err = new RpcError('Cron scheduler is not available')
        err.code = 'UNAVAILABLE'
        return Promise.reject(err)
      }
      return base(method, params)
    })
    renderPage()
    const card = await screen.findByRole('article', { name: /daily standup/i })
    fireEvent.click(within(card).getByRole('button', { name: /run daily standup now/i }))
    await waitFor(() =>
      expect(
        within(card).getByText('The scheduler is not running on this gateway.'),
      ).toBeInTheDocument(),
    )
    expect(within(card).getByRole('button', { name: /run daily standup now/i })).toBeDisabled()
    expect(within(card).getByRole('button', { name: /pause daily standup/i })).toBeDisabled()
  })
})
