import './truth-badge.css'
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import {
  truthFreshnessLabel,
  truthReasonLabel,
  truthStateLabel,
  truthTone,
  type RuntimeTruth,
  type TruthAction,
} from '@/lib/runtime-truth'
import { useRuntimeTruth } from '@/lib/useRuntimeTruth'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useRuntimeHost } from '~/lib/use-runtime-host'
import { useAttentionPanel } from '~/stores/attention'
import { useGateway } from '~/stores/gateway'

/** The desktop's words for the shared actions (it has no Health screen). */
function actionLabel(action: TruthAction): string {
  switch (action) {
    case 'retry_connection':
      return t('truth.action.retry')
    case 'start_gateway':
      return t('truth.action.start')
    case 'open_inbox':
      return t('truth.action.inbox')
    default:
      return t('truth.action.issues')
  }
}

/**
 * The toolbar's runtime readout: a dot, the state and the freshness ("Live ·
 * 12 s"). Pressing it shows why, and exactly one way forward. Same
 * derivation as the web console's footer badge (`useRuntimeTruth`), the
 * desktop's own markup; the desktop also knows the process, so a stopped
 * gateway offers "Start gateway".
 */
export function TruthBadge() {
  const host = useRuntimeHost()
  const truth = useRuntimeTruth(host)
  const [open, setOpen] = useState(false)
  const popId = useId()
  const tone = truthTone(truth.state)
  const state = truthStateLabel(truth.state)
  const fresh = truthFreshnessLabel(truth)
  const close = useCallback(() => setOpen(false), [])

  return (
    <div className="truth">
      <button
        type="button"
        className="truth__badge app-no-drag"
        data-tone={tone}
        data-state={truth.state}
        aria-label={`${t('truth.label')}: ${state}, ${fresh}`}
        title={`${state} · ${fresh}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="truth__dot" aria-hidden />
        <span className="truth__state">{state}</span>
        <span className="truth__fresh">{fresh}</span>
      </button>
      {open ? <TruthPopover id={popId} truth={truth} onClose={close} /> : null}
    </div>
  )
}

function TruthPopover({
  id,
  truth,
  onClose,
}: {
  id: string
  truth: RuntimeTruth
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const retry = useRuntimeSignals((s) => s.retry)
  const start = useGateway((s) => s.start)
  const busy = useGateway((s) => s.busy)
  const openAttention = useAttentionPanel((s) => s.setOpen)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    const onDown = (e: MouseEvent) => {
      const anchor = ref.current?.parentElement
      if (anchor && !anchor.contains(e.target as Node)) onClose()
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [onClose])

  const run = () => {
    onClose()
    if (truth.action === 'retry_connection') retry()
    else if (truth.action === 'start_gateway') void start()
    else openAttention(true)
  }

  return (
    <div
      ref={ref}
      id={id}
      className="truth__pop app-no-drag"
      role="dialog"
      aria-label={t('truth.label')}
      data-tone={truthTone(truth.state)}
    >
      <div className="truth__pop-head">
        <span className="truth__pop-title">{t('truth.label')}</span>
        <span className="truth__pop-state">{truthStateLabel(truth.state)}</span>
      </div>
      <p className="truth__pop-reason">{truthReasonLabel(truth)}</p>
      {truth.detail ? <code className="truth__pop-detail">{truth.detail}</code> : null}
      <div className="truth__pop-facts">
        <span>{truthFreshnessLabel(truth)}</span>
        <span>
          {truth.attentionCount > 0
            ? t('truth.attention.some').replace('{count}', String(truth.attentionCount))
            : t('truth.attention.none')}
        </span>
      </div>
      <div className="truth__pop-action">
        <Button
          variant={truth.state === 'ready' ? 'secondary' : 'primary'}
          disabled={truth.action === 'start_gateway' && busy}
          data-action={truth.action}
          onClick={run}
        >
          {actionLabel(truth.action)}
        </Button>
      </div>
    </div>
  )
}
