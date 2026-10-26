import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DisabledReason } from '@/components/DisabledReason'
import {
  blockedBecause,
  gate,
  OPEN_GATE,
  requireConnection,
  requireNoPendingApprovals,
  requireScheduler,
} from './disabled-reason'

describe('gate', () => {
  it('is open when every check passes', () => {
    expect(gate(requireConnection('connected'), requireScheduler(false), null, false)).toEqual(
      OPEN_GATE,
    )
  })

  it('takes the first block, with its code and words', () => {
    expect(gate(requireConnection('disconnected'), requireScheduler(true))).toEqual({
      disabled: true,
      code: 'gateway_offline',
      reason: 'The gateway is offline. Reconnect to use this.',
    })
    expect(gate(requireConnection('connected'), requireScheduler(true)).code).toBe(
      'scheduler_unavailable',
    )
    expect(gate(requireConnection('connecting')).reason).toBe('Waiting for the gateway connection.')
  })

  it('pluralises the approvals reason and accepts a custom one', () => {
    expect(requireNoPendingApprovals(0)).toBeNull()
    expect(requireNoPendingApprovals(1)?.reason).toBe(
      '1 approval is waiting. Answer it before updating.',
    )
    expect(requireNoPendingApprovals(3)?.reason).toBe(
      '3 approvals are waiting. Answer them before updating.',
    )
    expect(gate(blockedBecause('Not here'))).toMatchObject({ code: 'custom', reason: 'Not here' })
  })
})

describe('DisabledReason', () => {
  it('prints the reason, links it to the group and keeps the controls in place', () => {
    const closed = gate(requireConnection('disconnected'))
    const { rerender } = render(
      <DisabledReason gate={closed}>
        <button type="button" disabled={closed.disabled}>
          Run
        </button>
      </DisabledReason>,
    )
    const button = screen.getByRole('button', { name: 'Run' })
    expect(button).toBeDisabled()
    const group = screen.getByRole('group')
    const text = screen.getByText('The gateway is offline. Reconnect to use this.')
    expect(group).toHaveAttribute('aria-describedby', text.id)
    expect(group).toHaveAttribute('title', 'The gateway is offline. Reconnect to use this.')
    expect(group).toHaveAttribute('data-disabled-reason', 'gateway_offline')

    rerender(
      <DisabledReason gate={OPEN_GATE}>
        <button type="button" disabled={false}>
          Run
        </button>
      </DisabledReason>,
    )
    // Same element: opening the gate never remounts the control.
    expect(screen.getByRole('button', { name: 'Run' })).toBe(button)
    expect(button).toBeEnabled()
    expect(screen.queryByRole('group')).toBeNull()
    expect(screen.queryByText(/gateway is offline/)).toBeNull()
  })
})
