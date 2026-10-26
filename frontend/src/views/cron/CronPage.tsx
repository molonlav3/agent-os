import './cron.css'
import { Fragment, useEffect, useId, useState } from 'react'
import { useNavigate } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence } from 'motion/react'
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CalendarClockIcon,
  CopyIcon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SendIcon,
  SquareIcon,
  Trash2Icon,
} from 'lucide-react'
import { toast } from 'sonner'
import { DisabledReason } from '@/components/DisabledReason'
import { ModalShell } from '@/components/ModalShell'
import { Button } from '@/components/ui/button'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/cron'
import { copyWithFallback } from '@/lib/clipboard'
import { gate, requireConnection, requireScheduler, type Gate } from '@/lib/disabled-reason'
import { RpcError } from '@/lib/ws-rpc'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useConnection } from '@/stores/connection'
import { MotionListItem } from '@/lib/motion'
import { useRpc } from '@/app/providers'
import { relTime } from '@/views/overview/logic'
import { CronPanel } from './CronPanel'
import {
  activeChatSessionKey,
  CONCURRENCY_POLICIES,
  concurrencyLabel,
  explainCron,
  filterJobs,
  humanCountdownPast,
  isOkStatus,
  isUpcomingRun,
  jobDotState,
  jobConcurrency,
  jobCreatedFrom,
  jobElevated,
  jobRuntimeHealth,
  jobKindClass,
  jobKindLabel,
  jobSchedule,
  jobTarget,
  nextRunAbs,
  nextRunText,
  runRow,
  shortCronId,
  sortJobs,
  type ConcurrencyPolicy,
  type CronDot,
  type DeliveryTargetMap,
  type RawJob,
  type RawRun,
  type SaveBuild,
  type SortCol,
} from './logic'

// cron.js:1491 — the localStorage key that holds the active chat session.
const ACTIVE_SESSION_KEY = 'agentos_active_session'

// cron.js:1485-1494 — read the active chat session from the URL + localStorage
// (both reads tolerate access errors, like legacy). Kept impure-at-the-edge so
// activeChatSessionKey() in logic.ts stays a pure, tested transform.
function readActiveSessionKey(): string {
  let urlSession = ''
  try {
    urlSession = new URLSearchParams(window.location.search).get('session') || ''
  } catch {
    /* location unavailable */
  }
  let stored = ''
  try {
    stored = localStorage.getItem(ACTIVE_SESSION_KEY) || ''
  } catch {
    /* storage unavailable */
  }
  return activeChatSessionKey(urlSession, stored)
}

// The create/edit panel state: closed, creating (optional template seed), or
// editing an existing job.
type PanelState =
  | { kind: 'closed' }
  | { kind: 'create'; template: Partial<RawJob> | null }
  | { kind: 'edit'; job: RawJob }

// cron.js:832-843 — the three empty-state quick-start presets (seed the create
// panel). `hint` is UI-only; the rest is the template seed for seedForm.
function emptyTemplates(): Array<Partial<RawJob> & { expression: string; hint: string }> {
  return [
    {
      name: t('cron.templateStandupName'),
      expression: '0 9 * * 1-5',
      payloadKind: 'reminder',
      message: t('cron.templateStandupMessage'),
      hint: t('cron.templateStandupHint'),
    },
    {
      name: t('cron.templateHealthName'),
      expression: '0 * * * *',
      payloadKind: 'agent_turn',
      message: t('cron.templateHealthMessage'),
      hint: t('cron.templateHealthHint'),
    },
    {
      name: t('cron.templateWrapName'),
      expression: '0 17 * * 5',
      payloadKind: 'agent_turn',
      message: t('cron.templateWrapMessage'),
      hint: t('cron.templateWrapHint'),
    },
  ]
}

// cron.js:341-342 — cron.list may return a bare array or {jobs:[…]}.
interface CronListResult {
  jobs?: RawJob[]
}
interface CronRunsResult {
  runs?: RawRun[]
}
/** channels.deliveryTargets — recipients we can offer per channel. */
interface DeliveryTargetsResult {
  targets?: DeliveryTargetMap
}
/** cron.runOutput — the full stored output for a single run. */
interface CronRunOutput {
  output?: string
}

// dot state → --tone bucket (status color ONLY via --tone; never hardcoded).
function dotTone(state: CronDot): string {
  return state === 'error' ? 'tone-danger' : state === 'off' ? 'tone-dim' : 'tone-ok'
}

function StatTile({
  label,
  value,
  hint,
  hero,
}: {
  label: string
  value: React.ReactNode
  hint: React.ReactNode
  hero?: boolean
}) {
  return (
    <div className={`cron-stat${hero ? ' cron-stat--hero' : ''}`} aria-label={label}>
      <span className="cron-stat__label t-label">{label}</span>
      <strong className="cron-stat__value t-data">{value}</strong>
      <span className="cron-stat__hint">{hint}</span>
    </div>
  )
}

// ── Run-history drawer (cron.js:863-923) ─────────────────────────────────────
function RunsDrawer({
  jobId,
  jobName,
  onClose,
}: {
  jobId: string
  jobName: string
  onClose: () => void
}) {
  const rpc = useRpc()
  const navigate = useNavigate()

  // cron.js:883 — cron.runs {id, limit:10}; a failure surfaces an inline note.
  const runsQuery = useQuery<RawRun[]>({
    queryKey: ['cron', 'runs', jobId],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawRun[] | CronRunsResult>('cron.runs', { id: jobId, limit: 10 })
      return Array.isArray(data) ? data : (data.runs ?? [])
    },
    refetchOnWindowFocus: false,
  })

  const runs = runsQuery.data ?? []
  // A script job's output is its only trace — the row preview is one clipped
  // line, so the full text has to be reachable without leaving the page.
  const [expanded, setExpanded] = useState<number | null>(null)

  // cron.runs sends previews so a 20-row list stays small; the full output is
  // fetched for the one run that is open, and cached per run id.
  const openRunId = expanded != null ? (runs[expanded]?.id ?? '') : ''
  const outputQuery = useQuery<CronRunOutput>({
    queryKey: ['cron', 'runOutput', jobId, openRunId],
    enabled: Boolean(openRunId),
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<CronRunOutput>('cron.runOutput', { id: jobId, runId: openRunId })
    },
    refetchOnWindowFocus: false,
  })

  return (
    <div className="cron-detail panel" aria-label={t('cron.runsLandmark', { name: jobName })}>
      <div className="cron-detail__head">
        <div>
          <span className="cron-detail__eyebrow t-label">{t('cron.runsEyebrow')}</span>
          <strong className="cron-detail__name">{jobName}</strong>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label={t('cron.runsClose')}
        >
          {t('common.close')}
        </Button>
      </div>

      {runsQuery.isError ? (
        <p className="cron-muted">{t('cron.runsLoadFailed')}</p>
      ) : runsQuery.isLoading ? (
        <p className="cron-muted">{t('cron.runsLoading')}</p>
      ) : runs.length === 0 ? (
        <p className="cron-muted">{t('cron.runsEmpty')}</p>
      ) : (
        <div
          className="cron-runs-scroll"
          role="region"
          aria-label={t('cron.runsTableLandmark')}
          tabIndex={0}
        >
          <table className="cron-runs">
            <thead>
              <tr>
                <th>{t('cron.runsColTime')}</th>
                <th>{t('cron.runsColStatus')}</th>
                <th>{t('cron.runsColDuration')}</th>
                <th>{t('cron.runsColDelivery')}</th>
                <th>{t('cron.runsColOutput')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((r, i) => {
                const row = runRow(r, relTime)
                const isOpen = expanded === i
                return (
                  <Fragment key={i}>
                    <tr>
                      <td className="cron-mono">{row.timeLabel}</td>
                      <td>
                        <span className={`cron-status ${row.statusOk ? 'tone-ok' : 'tone-danger'}`}>
                          {row.status}
                        </span>
                      </td>
                      <td className="cron-mono">{row.duration}</td>
                      <td>{row.delivery}</td>
                      <td className="cron-runs__reply">
                        {row.replyFull ? (
                          <button
                            type="button"
                            className="cron-runs__reply-toggle"
                            aria-expanded={isOpen}
                            title={isOpen ? t('cron.runsHideOutput') : t('cron.runsShowOutput')}
                            onClick={() => setExpanded(isOpen ? null : i)}
                          >
                            {row.reply}
                          </button>
                        ) : (
                          row.reply
                        )}
                      </td>
                      <td>
                        {row.sessionKey && row.chatAvailable ? (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              navigate('/chat?session=' + encodeURIComponent(row.sessionKey))
                            }
                          >
                            {t('cron.runsToChat')}
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                    {isOpen ? (
                      <tr className="cron-runs__output-row">
                        <td colSpan={6}>
                          <pre className="cron-runs__output">
                            {outputQuery.data?.output ?? row.replyFull}
                          </pre>
                          {outputQuery.isLoading ? (
                            <p className="cron-muted">{t('cron.runsLoadingOutput')}</p>
                          ) : outputQuery.isError ? (
                            <p className="cron-muted">{t('cron.runsOutputFailed')}</p>
                          ) : null}
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Job card (cron.js:612-650) ───────────────────────────────────────────────
function JobCard({
  job,
  busy,
  control,
  selected,
  onOpen,
  onToggle,
  onRun,
  onEdit,
  onDelete,
  onCopyId,
  onConcurrency,
}: {
  job: RawJob
  busy: boolean
  /** Run / pause / overlap need a connected gateway with a running scheduler. */
  control: Gate
  selected: boolean
  onOpen: (id: string) => void
  onToggle: (job: RawJob) => void
  onRun: (id: string) => void
  onEdit: (job: RawJob) => void
  onDelete: (job: RawJob) => void
  onCopyId: (id: string) => void
  onConcurrency: (job: RawJob, policy: ConcurrencyPolicy) => void
}) {
  const id = String(job.id ?? '')
  const name = String(job.name || job.id || '')
  const enabled = !!job.enabled
  const dot = jobDotState(job)
  const lastStatus = job.last_status || (job.last_run ? 'ok' : null)
  const lastRun = job.last_run ? humanCountdownPast(new Date(job.last_run as string | number)) : '—'
  const nextRun = nextRunText(job)
  const nextAbs = nextRunAbs(job)
  const schedule = jobSchedule(job)
  const human = explainCron(job.expression || '') || ''
  const kind = jobKindLabel(job)
  const kindClass = jobKindClass(job)
  const target = jobTarget(job)
  const createdFrom = jobCreatedFrom(job)
  const elevated = jobElevated(job)
  const message = String(job.message || job.prompt || '').trim()
  // Present only when the gateway reports them; absent means nothing is shown.
  const health = jobRuntimeHealth(job)
  const concurrency = jobConcurrency(job)
  const failures = health ? tPlural('cron.cardFailures', health.consecutiveFailures) : ''

  return (
    <article
      className={`panel cron-card ${health?.degraded ? 'tone-danger' : dotTone(dot)}${selected ? ' is-selected' : ''}`}
      aria-label={t('cron.cardLandmark', { name })}
      data-degraded={health?.degraded ? 'true' : undefined}
    >
      <header className="cron-card__head">
        <span
          className={`cron-card__dot tone-${dot === 'error' ? 'danger' : dot === 'off' ? 'dim' : 'ok'}`}
          aria-hidden="true"
        />
        <button
          type="button"
          className="cron-card__name"
          title={t('cron.cardRunHistoryTitle')}
          onClick={() => onOpen(id)}
        >
          {name}
        </button>
        <span className={`cron-pill cron-pill--${kindClass}`}>{kind}</span>
        {elevated ? (
          <span
            className="cron-pill cron-pill--elevated tone-danger"
            title={t('cron.cardElevatedTitle', { mode: elevated })}
          >
            {t('cron.cardElevated', { mode: elevated })}
          </span>
        ) : null}
        {health?.degraded ? (
          <span
            className="cron-pill cron-pill--degraded tone-danger"
            title={t('cron.cardDegradedTitle', { failures })}
            data-testid="cron-degraded"
          >
            {t('cron.cardDegraded')}
          </span>
        ) : null}
      </header>

      <div className="cron-card__schedule">
        <code className="cron-expr">{schedule}</code>
        {human ? <span className="cron-card__human">{human}</span> : null}
      </div>

      <dl className="cron-card__meta">
        {id ? (
          <div className="cron-card__id">
            <dt className="t-label">{t('cron.cardCronId')}</dt>
            <dd className="t-data cron-mono" title={id}>
              <span className="cron-card__id-value">{shortCronId(id)}</span>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                title={t('cron.cardCopyId')}
                aria-label={t('cron.cardCopyIdFor', { id })}
                onClick={() => onCopyId(id)}
              >
                <CopyIcon />
              </Button>
            </dd>
          </div>
        ) : null}
        <div>
          <dt className="t-label">{t('cron.cardTarget')}</dt>
          <dd className="t-data" title={target}>
            {target}
          </dd>
        </div>
        {createdFrom ? (
          <div>
            <dt className="t-label">{t('cron.cardCreatedFrom')}</dt>
            <dd className="t-data cron-mono" title={createdFrom}>
              {createdFrom}
            </dd>
          </div>
        ) : null}
        <div>
          <dt className="t-label">{t('cron.cardLastRun')}</dt>
          <dd className="t-data">
            {lastRun}
            {lastStatus ? (
              <>
                {' · '}
                <span
                  className={`cron-status ${isOkStatus(lastStatus) ? 'tone-ok' : 'tone-danger'}`}
                >
                  {lastStatus}
                </span>
              </>
            ) : null}
          </dd>
        </div>
        <div>
          <dt className="t-label">{t('cron.cardNextRun')}</dt>
          <dd className="t-data">
            {enabled ? (
              <>
                <span className="cron-mono">{nextRun}</span>
                {nextAbs ? <span className="cron-card__abs"> · {nextAbs}</span> : null}
              </>
            ) : (
              <span className="cron-muted">{t('cron.cardPaused')}</span>
            )}
          </dd>
        </div>
        {/* A health row only when there is something to say: a reported
            verdict, a failure streak, or a job that has run. */}
        {health && (health.degraded || health.consecutiveFailures > 0 || job.last_run) ? (
          <div className="cron-card__health" data-testid="cron-health">
            <dt className="t-label">{t('cron.cardHealth')}</dt>
            <dd className="t-data">
              <span
                className={`cron-status ${health.degraded || health.consecutiveFailures ? 'tone-danger' : 'tone-ok'}`}
              >
                {health.consecutiveFailures ? failures : t('cron.cardHealthy')}
              </span>
            </dd>
          </div>
        ) : null}
        {health?.lastError ? (
          <div className="cron-card__message cron-card__error">
            <dt className="t-label">{t('cron.cardLastError')}</dt>
            <dd className="t-data">
              {health.lastError.length > 240
                ? health.lastError.slice(0, 240) + '…'
                : health.lastError}
            </dd>
          </div>
        ) : null}
        {concurrency ? (
          <div className="cron-card__concurrency">
            <dt className="t-label">{t('cron.cardConcurrency')}</dt>
            <dd>
              <select
                className="cron-select"
                value={concurrency}
                disabled={busy || control.disabled}
                title={control.reason ?? concurrencyLabel(concurrency)}
                aria-label={t('cron.cardConcurrencyAria', { name })}
                onChange={(e) => onConcurrency(job, e.target.value as ConcurrencyPolicy)}
              >
                {CONCURRENCY_POLICIES.map((policy) => (
                  <option key={policy} value={policy}>
                    {concurrencyLabel(policy)}
                  </option>
                ))}
              </select>
            </dd>
          </div>
        ) : null}
        {message ? (
          <div className="cron-card__message">
            <dt className="t-label">{t('cron.cardPrompt')}</dt>
            <dd className="t-data">
              {message.length > 140 ? message.slice(0, 140) + '…' : message}
            </dd>
          </div>
        ) : null}
      </dl>

      <footer className="cron-card__actions">
        <DisabledReason gate={control} className="cron-card__gate">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || control.disabled}
            aria-label={t('cron.cardRunNow', { name })}
            onClick={() => onRun(id)}
          >
            <SendIcon />
            <span>{t('cron.cardRun')}</span>
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || control.disabled}
            aria-label={t('cron.cardToggleAria', {
              action: enabled ? t('cron.cardPause') : t('cron.cardResume'),
              name,
            })}
            onClick={() => onToggle(job)}
          >
            {enabled ? <SquareIcon /> : <SendIcon />}
            <span>{enabled ? t('cron.cardPause') : t('cron.cardResume')}</span>
          </Button>
        </DisabledReason>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label={t('cron.cardEditAria', { name })}
          onClick={() => onEdit(job)}
        >
          <PencilIcon />
          <span>{t('cron.cardEdit')}</span>
        </Button>
        <Button
          type="button"
          size="sm"
          variant="destructive"
          disabled={busy}
          aria-label={t('cron.cardDeleteAria', { name })}
          onClick={() => onDelete(job)}
        >
          <Trash2Icon />
        </Button>
      </footer>
    </article>
  )
}

// ── Delete confirmation (cron.js:773-787) ────────────────────────────────────
function DeleteConfirm({
  jobName,
  busy,
  onCancel,
  onConfirm,
}: {
  jobName: string
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const titleId = useId()

  return (
    <ModalShell
      role="alertdialog"
      labelledBy={titleId}
      onClose={onCancel}
      dismissible={!busy}
      overlayClassName="cron-modal__overlay"
      className="cron-modal panel"
    >
      <h2 id={titleId} className="cron-modal__title">
        {t('cron.deleteTitle')}
      </h2>
      <p className="cron-modal__body">
        {t('cron.deleteLead')} <strong>{jobName}</strong>
        {t('cron.deleteTail')}
      </p>
      <footer className="cron-modal__foot">
        <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>
          {t('common.cancel')}
        </Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={onConfirm}>
          {t('cron.deleteConfirm')}
        </Button>
      </footer>
    </ModalShell>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
/** How long after `cron.run.finished` the run's record (and health) is in place. */
const RUN_RECORD_SETTLE_MS = 1500

/** The scheduler refused with UNAVAILABLE: it is not running on this gateway. */
function isSchedulerUnavailable(err: unknown): boolean {
  return err instanceof RpcError && err.code === 'UNAVAILABLE'
}

export function CronPage() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const rpcState = useConnection((s) => s.state)
  // Learned from the gateway's own answer (cron.list reads as empty either
  // way) and tied to the connection it came from: the next connection may be
  // a different gateway process, so it starts unknown again.
  const connectedAt = useRuntimeSignals((s) => s.connectedAt)
  const [schedulerDownAt, setSchedulerDownAt] = useState<number | null | undefined>(undefined)
  const schedulerUnavailable = schedulerDownAt !== undefined && schedulerDownAt === connectedAt
  const control = gate(requireConnection(rpcState), requireScheduler(schedulerUnavailable))
  const noteSchedulerError = (err: unknown) => {
    if (isSchedulerUnavailable(err)) setSchedulerDownAt(useRuntimeSignals.getState().connectedAt)
  }

  const [search, setSearch] = useState('')
  const [sortCol, setSortCol] = useState<SortCol>('next_run')
  const [sortAsc, setSortAsc] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<RawJob | null>(null)
  const [panel, setPanel] = useState<PanelState>({ kind: 'closed' })

  useEffect(() => {
    document.title = 'Cron - AgentOS Control'
  }, [])

  // cron.js:339-347 — cron.list after waitForConnection (array or {jobs}).
  const jobsQuery = useQuery<RawJob[]>({
    queryKey: ['cron'],
    queryFn: async () => {
      await rpc.waitForConnection()
      const data = await rpc.call<RawJob[] | CronListResult>('cron.list', {})
      return Array.isArray(data) ? data : (data.jobs ?? [])
    },
    refetchOnWindowFocus: false,
  })

  // Recipients the gateway can name for us, so the delivery form offers a
  // choice instead of asking someone to remember a chat id. A failure here is
  // not worth a toast: the form falls back to its text input.
  const deliveryTargetsQuery = useQuery<DeliveryTargetsResult>({
    queryKey: ['cron', 'deliveryTargets'],
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<DeliveryTargetsResult>('channels.deliveryTargets', {})
    },
    refetchOnWindowFocus: false,
  })

  // cron.js:346 — load-failure toast (stable id so repeats dedupe).
  useEffect(() => {
    if (jobsQuery.isError) {
      const err = jobsQuery.error
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Failed to load cron jobs: ' + message, { id: 'cron-load-err' })
    }
  }, [jobsQuery.isError, jobsQuery.error])

  // cron.js:306-316,318-321 — the subscribe/unsubscribe lifecycle. Legacy
  // subscribes on render and unsubscribes on destroy. In React this is a mount
  // effect: cron.subscribe after the WS handshake (best-effort; a pre-connect
  // call rejects with "Not connected"), and cron.unsubscribe in the cleanup.
  // The cleanup runs on every unmount (incl. StrictMode's dev double-invoke),
  // so no subscription leaks across remounts.
  useEffect(() => {
    let cancelled = false
    rpc
      .waitForConnection()
      .then(() => {
        if (!cancelled) return rpc.call('cron.subscribe', {})
        return undefined
      })
      .catch(() => {
        /* subscription is best-effort */
      })
    return () => {
      cancelled = true
      rpc.call('cron.unsubscribe', {}).catch(() => {
        /* best-effort; ignore disconnected state */
      })
    }
  }, [rpc])

  // cron.js:313-315 — cron.run.finished → invalidate the job list AND any open
  // runs drawer (targeted refetch). Cleaned up on unmount so the listener never
  // leaks (StrictMode-safe: the unsub closure removes exactly this handler).
  //
  // The gateway emits the event while it delivers the run's output, just
  // before it records the result (the failure streak in `health`), so one
  // more read a moment later picks up the recorded health. A single settle
  // read per event, not a poll.
  useEffect(() => {
    const settles = new Set<ReturnType<typeof setTimeout>>()
    const unsub = rpc.on('cron.run.finished', () => {
      void queryClient.invalidateQueries({ queryKey: ['cron'] })
      const settle = setTimeout(() => {
        settles.delete(settle)
        void queryClient.invalidateQueries({ queryKey: ['cron'] })
      }, RUN_RECORD_SETTLE_MS)
      settles.add(settle)
    })
    return () => {
      unsub()
      for (const settle of settles) clearTimeout(settle)
    }
  }, [rpc, queryClient])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['cron'] })

  // cron.js:735-744 — enable/disable toggle → cron.update {id, enabled:!enabled}.
  const toggleMutation = useMutation({
    mutationFn: (job: RawJob) => rpc.call('cron.update', { id: job.id, enabled: !job.enabled }),
    onSuccess: (_data, job) => {
      toast.info(`Job ${job.enabled ? 'paused' : 'resumed'}`, { id: 'cron-toggle' })
      void invalidate()
    },
    onError: (err) => {
      noteSchedulerError(err)
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Update failed: ' + message, { id: 'cron-toggle-err' })
    },
  })

  // Overlap policy: only offered when the job payload carries `concurrency`.
  const concurrencyMutation = useMutation({
    mutationFn: (vars: { job: RawJob; policy: ConcurrencyPolicy }) =>
      rpc.call('cron.update', { id: vars.job.id, concurrency: vars.policy }),
    onSuccess: (_data, vars) => {
      toast.info(t('cron.toastConcurrency', { policy: vars.policy }), { id: 'cron-concurrency' })
      void invalidate()
    },
    onError: (err) => {
      noteSchedulerError(err)
      const message = err instanceof Error ? err.message : String(err)
      toast.error(t('cron.toastConcurrencyFailed', { message }), { id: 'cron-concurrency-err' })
    },
  })

  // cron.js:746-766 — run-now → cron.run {id}; surface reply/error/triggered.
  const runMutation = useMutation({
    mutationFn: (id: string) => rpc.call<{ reply?: string; error?: string }>('cron.run', { id }),
    onSuccess: (res) => {
      if (res && res.reply) {
        toast.success('Run complete: ' + res.reply.substring(0, 120), { id: 'cron-run' })
      } else if (res && res.error) {
        toast.warning('Run failed: ' + res.error, { id: 'cron-run' })
      } else {
        toast.success('Job triggered', { id: 'cron-run' })
      }
    },
    onError: (err) => {
      noteSchedulerError(err)
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Run failed: ' + message, { id: 'cron-run-err' })
    },
  })

  // cron.js:768-788 — delete after confirmation → cron.remove {id}.
  const removeMutation = useMutation({
    mutationFn: (id: string) => rpc.call('cron.remove', { id }),
    onSuccess: (_data, id) => {
      toast.info('Job deleted', { id: 'cron-remove' })
      if (selectedId === id) setSelectedId(null)
      setPendingDelete(null)
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Delete failed: ' + message, { id: 'cron-remove-err' })
    },
  })

  // cron.js:1240-1250 — save the create/edit panel → cron.create OR cron.update
  // (method + full payload assembled in logic.ts::buildSavePayload). Success
  // toast + close + reload; failure toast.
  const saveMutation = useMutation({
    mutationFn: (build: Extract<SaveBuild, { ok: true }>) => rpc.call(build.method, build.payload),
    onSuccess: (_data, build) => {
      toast.success(build.method === 'cron.update' ? 'Schedule updated' : 'Schedule created', {
        id: 'cron-save',
      })
      setPanel({ kind: 'closed' })
      void invalidate()
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : String(err)
      toast.error('Save failed: ' + message, { id: 'cron-save-err' })
    },
  })

  // The job id is the handle every `agentos cron …` command takes, so the card
  // exposes it with a one-click copy rather than making you go find it.
  async function copyId(id: string) {
    try {
      await copyWithFallback(id)
      toast.success('Copied cron ID', { id: 'cron-copy-ok', duration: 1600 })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.warning('Copy failed: ' + message, { id: 'cron-copy-err', duration: 2500 })
    }
  }

  const jobs = jobsQuery.data ?? []
  const busy =
    toggleMutation.isPending ||
    runMutation.isPending ||
    removeMutation.isPending ||
    concurrencyMutation.isPending

  // cron.js:381-405 — summary stats.
  const total = jobs.length
  const enabledCount = jobs.filter((j) => j.enabled).length
  const paused = total - enabledCount
  const upcoming = jobs.filter((j) => isUpcomingRun(j)).length
  // Reminders and scripts share a tile: both deliver without spending a token.
  const reminders = jobs.filter((j) => {
    const kind = j.payloadKind || j.payload_kind
    return kind === 'reminder' || kind === 'script'
  }).length
  const agentTasks = jobs.filter((j) => (j.payloadKind || j.payload_kind) === 'agent_turn').length

  // cron.js:562-570 — filter then sort.
  const visible = sortJobs(filterJobs(jobs, search), sortCol, sortAsc)

  const selectedJob = selectedId ? jobs.find((j) => String(j.id) === selectedId) : undefined

  return (
    <div className="cron-stage">
      <header className="cron-stage__header">
        <div className="cron-stage__title-block">
          <span className="t-label">{t('cron.eyebrow')}</span>
          <h1 className="t-display">{t('cron.title')}</h1>
          <p className="cron-stage__subtitle">{t('cron.subtitle')}</p>
        </div>
        <div className="cron-stage__actions">
          <Button
            variant="outline"
            title={t('cron.refresh')}
            className="text-xs uppercase tracking-[0.14em]"
            disabled={jobsQuery.isFetching}
            onClick={() => void invalidate()}
          >
            <RefreshCwIcon className={jobsQuery.isFetching ? 'cron-refresh-spin' : undefined} />
            <span>{jobsQuery.isFetching ? t('cron.refreshBusy') : t('cron.refresh')}</span>
          </Button>
          <Button
            className="text-xs uppercase tracking-[0.14em]"
            onClick={() => setPanel({ kind: 'create', template: null })}
          >
            <PlusIcon />
            <span>{t('cron.newJob')}</span>
          </Button>
        </div>
      </header>

      <section
        className={`cron-command${jobsQuery.isFetching ? ' is-loading' : ''}`}
        aria-label={t('cron.operationsLandmark')}
        aria-busy={jobsQuery.isFetching}
      >
        <div className="cron-command__toolbar">
          <div className="cron-command__heading">
            <span className="cron-command__icon" aria-hidden="true">
              <CalendarClockIcon />
            </span>
            <div>
              <span className="t-label">{t('cron.clockEyebrow')}</span>
              <strong>{t('cron.clockTitle')}</strong>
            </div>
          </div>
          <span className="cron-command__meta t-data">
            <span className={enabledCount ? 'tone-ok' : 'tone-dim'} aria-hidden="true" />
            {enabledCount ? t('cron.clockActive', { count: enabledCount }) : t('cron.clockIdle')}
          </span>
        </div>
        <div className="cron-stats" aria-label={t('cron.statsLandmark')}>
          <StatTile
            label="Active schedules"
            hero
            value={enabledCount}
            hint={paused ? `${paused} paused` : total ? 'all enabled' : 'none configured'}
          />
          <StatTile
            label="Upcoming runs"
            value={upcoming}
            hint={upcoming ? 'scheduled ahead' : 'no upcoming runs'}
          />
          <StatTile label="Reminders" value={reminders} hint="reminders & scripts" />
          <StatTile label="Agent tasks" value={agentTasks} hint="scheduled turns" />
        </div>
      </section>

      <section className="cron-list">
        <div className="cron-list__head">
          <div className="cron-list__heading">
            <h2 className="cron-list__title">
              {search ? t('cron.listMatching') : t('cron.listAll')}
            </h2>
            <span className="cron-list__count t-data">
              {visible.length}
              {search ? ` of ${total}` : ''}
            </span>
          </div>
          <div className="cron-list__tools">
            <div className="cron-search-wrap">
              <SearchIcon aria-hidden="true" />
              <input
                className="cron-search t-data"
                type="search"
                placeholder={t('cron.searchPlaceholder')}
                autoComplete="off"
                aria-label={t('cron.searchLabel')}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <label className="cron-sort">
              <span className="t-label">{t('cron.sortLabel')}</span>
              <select
                className="cron-sort__select t-data"
                aria-label={t('cron.sortAria')}
                value={sortCol}
                onChange={(e) => setSortCol(e.target.value as SortCol)}
              >
                <option value="next_run">{t('cron.sortNextRun')}</option>
                <option value="name">{t('cron.sortName')}</option>
                <option value="last_run">{t('cron.sortLastRun')}</option>
                <option value="payloadKind">{t('cron.sortKind')}</option>
                <option value="sessionTarget">{t('cron.sortTarget')}</option>
                <option value="expression">{t('cron.sortSchedule')}</option>
              </select>
            </label>
            <Button
              variant="outline"
              size="icon-sm"
              title={sortAsc ? 'Ascending' : 'Descending'}
              aria-label={`Sort direction: ${sortAsc ? 'ascending' : 'descending'}`}
              onClick={() => setSortAsc((v) => !v)}
            >
              {sortAsc ? <ArrowUpIcon /> : <ArrowDownIcon />}
            </Button>
          </div>
        </div>

        {jobs.length === 0 ? (
          <div className="cron-empty">
            <div className="cron-empty__title">{t('cron.emptyTitle')}</div>
            <p className="cron-empty__msg">{t('cron.emptyMsg')}</p>
            <div className="cron-empty__actions">
              <Button type="button" onClick={() => setPanel({ kind: 'create', template: null })}>
                <PlusIcon />
                <span>{t('cron.emptyAction')}</span>
              </Button>
            </div>
            <div className="cron-empty__hints">
              <span className="cron-empty__hints-label t-label">{t('cron.presetsLabel')}</span>
              {emptyTemplates().map((tpl) => (
                <button
                  key={tpl.name}
                  type="button"
                  className="cron-empty-hint"
                  onClick={() => setPanel({ kind: 'create', template: tpl })}
                >
                  <code>{tpl.expression}</code>
                  <span>{tpl.hint}</span>
                </button>
              ))}
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className="cron-empty">
            <div className="cron-empty__title">{t('cron.noMatchTitle')}</div>
            <p className="cron-empty__msg">{t('cron.noMatchMsg')}</p>
          </div>
        ) : (
          <div className="cron-cards">
            <AnimatePresence initial={false}>
              {visible.map((job, i) => (
                <MotionListItem key={String(job.id ?? i)} className="cron-cards__item">
                  <JobCard
                    job={job}
                    busy={busy}
                    control={control}
                    selected={selectedId === String(job.id)}
                    onOpen={(id) => setSelectedId((cur) => (cur === id ? null : id))}
                    onToggle={(j) => toggleMutation.mutate(j)}
                    onRun={(id) => runMutation.mutate(id)}
                    onEdit={(j) => setPanel({ kind: 'edit', job: j })}
                    onDelete={(j) => setPendingDelete(j)}
                    onCopyId={(id) => void copyId(id)}
                    onConcurrency={(j, policy) => concurrencyMutation.mutate({ job: j, policy })}
                  />
                </MotionListItem>
              ))}
            </AnimatePresence>
          </div>
        )}

        {selectedJob ? (
          <RunsDrawer
            jobId={String(selectedJob.id)}
            jobName={String(selectedJob.name || selectedJob.id)}
            onClose={() => setSelectedId(null)}
          />
        ) : null}
      </section>

      {pendingDelete ? (
        <DeleteConfirm
          jobName={String(pendingDelete.name || pendingDelete.id)}
          busy={removeMutation.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => removeMutation.mutate(String(pendingDelete.id))}
        />
      ) : null}

      {panel.kind !== 'closed' ? (
        <CronPanel
          // Remount on a new seed so form state resets between create/edit and
          // between different jobs (mirrors the agents-dialog key strategy).
          key={panel.kind === 'edit' ? 'edit:' + String(panel.job.id) : 'create'}
          job={panel.kind === 'edit' ? panel.job : null}
          template={panel.kind === 'create' ? panel.template : null}
          activeSessionKey={readActiveSessionKey()}
          saving={saveMutation.isPending}
          deliveryTargets={deliveryTargetsQuery.data?.targets}
          onCancel={() => setPanel({ kind: 'closed' })}
          onSubmit={(build) => saveMutation.mutate(build)}
        />
      ) : null}
    </div>
  )
}
