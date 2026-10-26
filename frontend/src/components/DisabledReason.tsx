import './disabled-reason.css'
import { useId, type ReactNode } from 'react'
import type { Gate } from '@/lib/disabled-reason'

/**
 * The web console's rendering of the disabled-with-reason convention
 * (`@/lib/disabled-reason`). Wrap the controls a `Gate` disables: while the
 * gate is closed the reason is printed next to them and announced with the
 * group, and the wrapper carries it as a tooltip (a disabled button swallows
 * its own hover).
 *
 * The wrapper is rendered whether the gate is open or closed, so opening it
 * never remounts the controls (focus stays where it was). The caller still
 * passes `disabled={gate.disabled}` to each control: the wrapper never clones
 * children, so what is disabled stays explicit at the call site.
 */
export function DisabledReason({
  gate,
  children,
  className,
}: {
  gate: Gate
  children: ReactNode
  className?: string
}) {
  const id = useId()
  const closed = gate.disabled && !!gate.reason
  return (
    <div
      className={`disabled-reason${className ? ` ${className}` : ''}`}
      role={closed ? 'group' : undefined}
      aria-describedby={closed ? id : undefined}
      title={closed ? (gate.reason ?? undefined) : undefined}
      data-disabled-reason={closed ? (gate.code ?? '') : undefined}
    >
      <div className="disabled-reason__controls">{children}</div>
      {closed ? (
        <p id={id} className="disabled-reason__text">
          {gate.reason}
        </p>
      ) : null}
    </div>
  )
}
