import { LoaderCircle, X } from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { t } from '@/i18n'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useRpc } from './providers'

// The preflight dialog (and the modal/motion code under it) loads only when
// the operator asks to update, never in the entry bundle.
const UpdatePreflightDialog = lazy(async () => ({
  default: (await import('./UpdatePreflightDialog')).UpdatePreflightDialog,
}))

export interface UpdateCheck {
  current: string
  latest: string | null
  status: 'up-to-date' | 'outdated' | 'offline'
}

/** What `updates.status` reports about the last `updates.apply` job. */
export interface UpdateJob {
  status: 'idle' | 'running' | 'done' | 'failed'
  exitCode?: number | null
  logTail?: string[]
  result?: { old?: string; new?: string; verified?: boolean } | null
}

const POLL_MS = 2000

/**
 * The "new release" strip under the header. Beyond telling the operator, it
 * runs the upgrade for them: `updates.apply` spawns `agentos upgrade` as a
 * detached job, the gateway restarts under us, and `updates.status` keeps
 * answering from whichever gateway process is up — so polling simply rides
 * through the reconnect.
 *
 * "Update now" never applies directly: it opens the preflight
 * (UpdatePreflightDialog), which lists what the restart would interrupt and
 * blocks while an approval is pending. Only its confirm calls `updates.apply`.
 */
export function UpdateBanner({ check, onDismiss }: { check: UpdateCheck; onDismiss: () => void }) {
  const rpc = useRpc()
  const [job, setJob] = useState<UpdateJob | null>(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const timer = useRef<number | null>(null)
  const noteUpdateFailure = useRuntimeSignals((s) => s.noteUpdateFailure)

  const running = starting || job?.status === 'running'

  useEffect(() => {
    if (job?.status !== 'running') return
    let active = true
    const tick = async () => {
      try {
        const next = await rpc.call<UpdateJob>('updates.status')
        if (active && next) setJob(next)
      } catch {
        /* the gateway is restarting under us: keep polling */
      }
      if (active) timer.current = window.setTimeout(() => void tick(), POLL_MS)
    }
    timer.current = window.setTimeout(() => void tick(), POLL_MS)
    return () => {
      active = false
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [rpc, job?.status])

  const apply = async () => {
    setStarting(true)
    setError(null)
    try {
      const started = await rpc.call<UpdateJob & { started: boolean }>('updates.apply', {
        source: 'auto',
      })
      setJob(started)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }

  const lastLine = job?.logTail?.length ? job.logTail[job.logTail.length - 1] : null

  // A failed update is a runtime issue too (attention inbox); a finished one
  // clears it.
  const failure = job?.status === 'failed' ? lastLine || t('shell.issueUpdateFailedTitle') : error
  useEffect(() => {
    if (failure) noteUpdateFailure(failure)
    else if (job?.status === 'done') noteUpdateFailure(null)
  }, [failure, job?.status, noteUpdateFailure])

  let message: string
  if (job?.status === 'done') {
    message = t('shell.updateDone', { latest: job.result?.new ?? check.latest ?? '' })
  } else if (job?.status === 'failed') {
    message = t('shell.updateFailed', { detail: lastLine ?? '' })
  } else if (running) {
    message = t('shell.updateRunning')
  } else if (error) {
    message = t('shell.updateFailed', { detail: error })
  } else {
    message = t('shell.updateAvailable', { current: check.current, latest: check.latest ?? '' })
  }

  return (
    <div
      role="status"
      className="flex items-center justify-between gap-4 border-b border-warn/20 bg-warn/10 px-4 py-2 text-sm text-warn shrink-0"
      data-testid="update-banner"
      data-state={job?.status ?? (running ? 'running' : 'idle')}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 font-semibold uppercase tracking-wider text-[10px] bg-warn/25 px-1.5 py-0.5 rounded-sm">
          {t('shell.updateLabel')}
        </span>
        <span className="truncate font-medium" title={lastLine ?? undefined}>
          {message}
        </span>
        {running && lastLine ? (
          <span className="hidden truncate text-xs opacity-70 md:inline">{lastLine}</span>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {job?.status === 'done' ? (
          <Button variant="outline" size="xs" onClick={() => window.location.reload()}>
            {t('shell.updateReload')}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="xs"
            disabled={running || confirming}
            aria-haspopup="dialog"
            onClick={() => setConfirming(true)}
          >
            {running ? (
              <>
                <LoaderCircle className="animate-spin" aria-hidden />
                {t('shell.updateRunningShort')}
              </>
            ) : job?.status === 'failed' || error ? (
              t('shell.updateRetry')
            ) : (
              t('shell.updateNow')
            )}
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={running}
          onClick={onDismiss}
          title={t('shell.updateDismiss')}
          aria-label={t('shell.updateDismiss')}
        >
          <X className="size-4" />
        </Button>
      </div>
      {confirming ? (
        <Suspense fallback={null}>
          <UpdatePreflightDialog
            latest={check.latest ?? ''}
            onCancel={() => setConfirming(false)}
            onConfirm={() => {
              setConfirming(false)
              void apply()
            }}
          />
        </Suspense>
      ) : null}
    </div>
  )
}
