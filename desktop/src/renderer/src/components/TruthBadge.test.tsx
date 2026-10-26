import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STOPPED_GATEWAY, type GatewayState } from '@shared/gateway'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useConnection } from '@/stores/connection'
import { useAttentionPanel } from '~/stores/attention'
import { useGateway } from '~/stores/gateway'
import { useSettings } from '~/stores/settings'
import { TruthBadge } from './TruthBadge'

const start = vi.fn(async () => {})

function gateway(state: GatewayState) {
  useGateway.setState({ status: { ...STOPPED_GATEWAY, state }, busy: false, start })
}

beforeEach(() => {
  start.mockClear()
  useRuntimeSignals.getState().reset()
  useAttentionPanel.setState({ open: false })
  useSettings.setState((s) => ({
    settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'managed' } },
  }))
})
afterEach(() => useRuntimeSignals.getState().reset())

describe('TruthBadge (desktop)', () => {
  it('says Offline and offers Start gateway when the managed gateway is stopped', () => {
    gateway('stopped')
    useConnection.getState().setState('disconnected')
    render(<TruthBadge />)
    const badge = screen.getByRole('button', { name: /Runtime status: Offline/ })
    expect(badge).toHaveAttribute('data-tone', 'danger')
    fireEvent.click(badge)
    const pop = screen.getByRole('dialog', { name: 'Runtime status' })
    expect(pop).toHaveTextContent('The gateway process is not running.')
    const actions = within(pop).getAllByRole('button')
    expect(actions).toHaveLength(1)
    fireEvent.click(actions[0]!)
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('retries instead for an external gateway it cannot start', () => {
    useSettings.setState((s) => ({
      settings: { ...s.settings, gateway: { ...s.settings.gateway, mode: 'external' } },
    }))
    gateway('error')
    useConnection.getState().setState('disconnected')
    render(<TruthBadge />)
    fireEvent.click(screen.getByRole('button', { name: /Runtime status/ }))
    expect(screen.getByRole('button', { name: 'Retry connection' })).toBeInTheDocument()
  })

  it('is Ready and live over a running gateway with a clean report', () => {
    gateway('running')
    act(() => {
      useRuntimeSignals.setState({
        everConnected: true,
        lastFrameAt: Date.now(),
        doctor: { status: 'ready', ready: true, counts: { error: 0 }, findings: [], fetchedAt: 1 },
      })
      useConnection.getState().setState('connected')
    })
    render(<TruthBadge />)
    const badge = screen.getByRole('button', { name: /Runtime status: Ready, Live · \d+ s/ })
    expect(badge).toHaveAttribute('data-state', 'ready')
    fireEvent.click(badge)
    fireEvent.click(screen.getByRole('button', { name: 'Show what needs you' }))
    expect(useAttentionPanel.getState().open).toBe(true)
  })
})
