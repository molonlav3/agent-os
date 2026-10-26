import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_LOCALE, registerCatalog, setLocale } from '@/i18n'
import { useConnection } from '@/stores/connection'
import { AppShell } from './AppShell'
import { getViews, routeChildren } from './routes'
import type { Bootstrap } from '@/lib/bootstrap'

// #258 — the regression guard for module-scope t(). Every string asserted below
// used to be resolved once at module-evaluation time, so it kept the boot
// locale forever while the rest of the UI switched. The catalog overrides only
// the keys under test; everything else still falls back to English, which is
// also how a real partial translation would behave.
registerCatalog('zz', {
  shell: {
    navGroupControl: 'ZZ Control',
    viewOverview: 'ZZ Overview',
    viewHealth: 'ZZ Health',
    truthOffline: 'ZZ Offline',
  },
  overview: {
    documentTitle: 'ZZ Overview - AgentOS Control',
    sessionRunning: 'ZZ Running',
  },
})

const mockBootstrap: Bootstrap = {
  version: '',
  ws_url: 'ws://localhost/ws',
  auth_mode: '',
  base_path: '/control',
  features: { diagnostics: false },
}

const noopRpc = {
  waitForConnection: () => new Promise<void>(() => {}),
  call: () => new Promise(() => {}),
  on: () => () => {},
  connect: () => {},
  disconnect: () => {},
}

vi.mock('./providers', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./providers')>()
  return { ...actual, useBootstrap: () => mockBootstrap, useRpc: () => noopRpc }
})

function renderShellAt(path: string) {
  const router = createMemoryRouter([{ element: <AppShell />, children: routeChildren }], {
    initialEntries: [path],
  })
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('locale changes after module load', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    )
    useConnection.getState().setState('disconnected')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    setLocale(DEFAULT_LOCALE)
  })

  it('re-resolves the route title table', () => {
    const titleFor = (path: string) => getViews().find((view) => view.path === path)?.title
    expect(titleFor('overview')).toBe('Overview')

    setLocale('zz')

    expect(titleFor('overview')).toBe('ZZ Overview')
  })

  it('follows the change in the nav group labels and nav item titles', async () => {
    renderShellAt('/overview')
    const groupLabels = () =>
      Array.from(document.querySelectorAll('.shell-nav-group__label')).map((el) => el.textContent)

    expect(await screen.findByRole('link', { name: 'Overview' })).toBeInTheDocument()
    expect(groupLabels()).toEqual(['Chat', 'Control', 'Settings'])

    act(() => {
      setLocale('zz')
    })

    expect(screen.getByRole('link', { name: 'ZZ Overview' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ZZ Health' })).toBeInTheDocument()
    // An untranslated key still falls back to English rather than the raw key.
    expect(groupLabels()).toEqual(['Chat', 'ZZ Control', 'Settings'])
  })

  it('follows the change in the truth badge', async () => {
    renderShellAt('/overview')
    const pill = await screen.findByTestId('nav-foot')
    expect(pill).toHaveTextContent('OFFLINE')

    act(() => {
      setLocale('zz')
    })

    expect(pill).toHaveTextContent('ZZ OFFLINE')
  })

  it('follows the change inside the routed view, not just the shell chrome', async () => {
    renderShellAt('/overview')
    await waitFor(() => expect(document.title).toBe('Overview - AgentOS Control'))

    act(() => {
      setLocale('zz')
    })

    // The view is remounted by the container key, so its own copy and the
    // document title it owns both re-resolve.
    await waitFor(() => expect(document.title).toBe('ZZ Overview - AgentOS Control'))
  })

  it('mirrors the active locale onto <html lang>', () => {
    renderShellAt('/overview')

    act(() => {
      setLocale('zz')
    })

    expect(document.documentElement.lang).toBe('zz')
  })
})
