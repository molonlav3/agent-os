import './truth-badge.css'
import { useEffect, useId, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router'
import { Button } from '@/components/ui/button'
import { useOverlayLayer } from '@/components/overlay-layer'
import { t, tPlural } from '@/i18n'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import {
  truthActionLabel,
  truthFreshnessLabel,
  truthReasonLabel,
  truthStateLabel,
  truthTone,
  type RuntimeTruth,
  type TruthAction,
} from '@/lib/runtime-truth'
import { useRuntimeTruth } from '@/lib/useRuntimeTruth'

// Legacy pill variants the footer CSS and tests key on.
const VARIANT: Record<ReturnType<typeof truthTone>, string> = {
  ok: 'ok',
  warn: 'warn',
  danger: 'err',
  dim: 'dim',
}

const DOT: Record<ReturnType<typeof truthTone>, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  dim: 'bg-dim',
}

const TEXT: Record<ReturnType<typeof truthTone>, string> = {
  ok: 'text-ok',
  warn: 'text-warn',
  danger: 'text-danger',
  dim: 'text-dim',
}

/**
 * The sidebar footer's runtime readout: the shell's single status indicator
 * (#conn-pill). It says the state (Ready / Degraded / Connecting / Blocked /
 * Offline / Unknown) and how fresh the view is ("Live · 12 s", "Snapshot ·
 * 3 min", "Unavailable"); pressing it opens a small panel with the reason
 * and exactly one recovery action.
 */
export function TruthBadge({ version }: { version: string }) {
  const truth = useRuntimeTruth()
  // Null while closed; the panel's viewport position while open.
  const [open, setOpen] = useState<{ left: number; bottom: number } | null>(null)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const panelId = useId()
  const tone = truthTone(truth.state)
  const stateLabel = truthStateLabel(truth.state)
  const freshness = truthFreshnessLabel(truth)
  const title = version
    ? t('shell.connWithVersion', { state: `${stateLabel} · ${freshness}`, version })
    : `${stateLabel} · ${freshness}`

  return (
    <div
      id="conn-pill"
      className="shell-sidebar__connection t-data"
      data-testid="nav-foot"
      data-variant={VARIANT[tone]}
      data-truth={truth.state}
      data-source={truth.source}
      title={title}
    >
      {/* Announce the state only, when it changes: the freshness next to it
          ages every second and would be re-read just as often. */}
      <span className="sr-only" role="status" aria-live="polite">
        {stateLabel}
      </span>
      <button
        ref={buttonRef}
        type="button"
        className="truth-badge"
        aria-label={t('shell.truthBadgeLabel', { state: stateLabel, freshness })}
        aria-expanded={open !== null}
        aria-controls={open ? panelId : undefined}
        aria-haspopup="dialog"
        data-testid="truth-badge"
        onClick={(e) => {
          if (open) {
            setOpen(null)
            return
          }
          // Anchored above the footer, in the viewport: the collapsed rail is
          // too narrow to hold it, so the panel is portalled.
          const rect = e.currentTarget.getBoundingClientRect()
          setOpen({
            left: Math.max(8, rect.left),
            bottom: Math.max(8, window.innerHeight - rect.top + 8),
          })
        }}
      >
        {/* Row one: light, state, version. Row two: the freshness, given the
            full width of the bar so "Snapshot · 25 s" is never clipped. */}
        <span className="truth-badge__row">
          <span
            aria-hidden="true"
            className={`shell-sidebar__status-dot size-1.5 shrink-0 rounded-full ${DOT[tone]}`}
          />
          <span className={`shell-sidebar__status-label ${TEXT[tone]}`}>
            {stateLabel.toUpperCase()}
          </span>
          {version ? <span className="shell-sidebar__version ml-auto">v{version}</span> : null}
        </span>
        <span className="shell-sidebar__status-label truth-badge__fresh">{freshness}</span>
      </button>
      {open ? (
        <TruthPanel
          id={panelId}
          truth={truth}
          position={open}
          anchorRef={buttonRef}
          onClose={() => {
            setOpen(null)
            buttonRef.current?.focus({ preventScroll: true })
          }}
        />
      ) : null}
    </div>
  )
}

function TruthPanel({
  id,
  truth,
  position,
  anchorRef,
  onClose,
}: {
  id: string
  truth: RuntimeTruth
  position: { left: number; bottom: number }
  anchorRef: RefObject<HTMLElement | null>
  onClose: () => void
}) {
  const navigate = useNavigate()
  const retry = useRuntimeSignals((s) => s.retry)
  const ref = useRef<HTMLDivElement | null>(null)
  const tone = truthTone(truth.state)
  useOverlayLayer()

  // The badge re-renders every second (the freshness clock), so the close
  // callback changes identity each time; read it through a ref so focus and
  // the listeners are set up once per opening.
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    ref.current?.querySelector<HTMLElement>('button')?.focus({ preventScroll: true })
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      onCloseRef.current()
    }
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (ref.current?.contains(target) || anchorRef.current?.contains(target)) return
      onCloseRef.current()
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [anchorRef])

  const run = (action: TruthAction) => {
    onClose()
    if (action === 'retry_connection') retry()
    else if (action === 'open_inbox') void navigate('/inbox')
    else if (action === 'open_health') void navigate('/health')
  }

  return createPortal(
    <div
      ref={ref}
      id={id}
      role="dialog"
      aria-label={t('shell.truthPanelTitle')}
      className={`truth-panel tone-${tone}`}
      data-testid="truth-panel"
      style={{ left: position.left, bottom: position.bottom }}
    >
      <div className="truth-panel__head">
        <span className="t-label">{t('shell.truthPanelTitle')}</span>
        <strong className="truth-panel__state">{truthStateLabel(truth.state)}</strong>
      </div>
      <p className="truth-panel__reason">{truthReasonLabel(truth)}</p>
      {truth.detail ? <code className="truth-panel__detail">{truth.detail}</code> : null}
      <dl className="truth-panel__facts t-data">
        <div>
          <dt>{t('shell.truthPanelSource')}</dt>
          <dd>{truthFreshnessLabel(truth)}</dd>
        </div>
        <div>
          <dt>{t('shell.viewInbox')}</dt>
          <dd>
            {truth.attentionCount > 0
              ? tPlural('shell.truthPanelAttention', truth.attentionCount)
              : t('shell.truthPanelAttentionNone')}
          </dd>
        </div>
      </dl>
      {/* Exactly one recovery action: the next useful step for this state. */}
      <Button
        type="button"
        size="sm"
        variant={truth.state === 'ready' ? 'outline' : 'default'}
        className="truth-panel__action"
        data-action={truth.action}
        onClick={() => run(truth.action)}
      >
        {truthActionLabel(truth.action)}
      </Button>
    </div>,
    document.body,
  )
}
