import './inbox.css'
import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import {
  AlertTriangleIcon,
  CheckIcon,
  InboxIcon,
  MessageCircleQuestionIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  XIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { CommandLine } from '@/components/CommandLine'
import { DisabledReason } from '@/components/DisabledReason'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
// Registers this view's copy; it ships in this chunk, not the entry bundle.
import '@/i18n/en/inbox'
import { getLocale, t, tPlural } from '@/i18n'
import { composeAskAnswer, type AskQuestion } from '@/lib/ask-questions'
import type { AttentionItem, AttentionMember } from '@/lib/attention-inbox'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { usePendingQuestions } from '@/lib/pending-questions'
import { useDismissedIssues, type IssueAction, type RuntimeIssue } from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { approvalMonitor, useApprovals, type ApprovalAction } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { chatPath, webSettingsPath } from './logic'

/**
 * When an item started waiting, as a wall-clock time ("since 11:32"): it
 * needs no clock to stay true, so the inbox runs no timer of its own.
 */
function Age({ at }: { at: number | null }) {
  if (at === null) return null
  const time = new Intl.DateTimeFormat(getLocale(), { hour: '2-digit', minute: '2-digit' }).format(
    new Date(at),
  )
  return <span className="inbox-age t-data">{t('inbox.since', { time })}</span>
}

function SessionLink({ sessionKey }: { sessionKey: string | null }) {
  const navigate = useNavigate()
  if (!sessionKey) return <span className="inbox-member__session">{t('inbox.noSession')}</span>
  return (
    <button
      type="button"
      className="inbox-member__session inbox-link"
      title={t('inbox.openSessionTitle', { session: sessionKey })}
      onClick={() => void navigate(chatPath(sessionKey))}
    >
      <code>{sessionKey}</code>
    </button>
  )
}

function ItemHead({
  item,
  icon,
  kindLabel,
}: {
  item: AttentionItem
  icon: ReactNode
  kindLabel: string
}) {
  return (
    <header className="inbox-item__head">
      <span className="inbox-item__icon" aria-hidden="true">
        {icon}
      </span>
      <div className="inbox-item__titles">
        <span className="t-label">{kindLabel}</span>
        <strong className="inbox-item__title">{item.title}</strong>
      </div>
      <div className="inbox-item__meta">
        {item.sessions.length > 1 ? (
          <span className="inbox-chip" data-testid="inbox-seen-in">
            {tPlural('inbox.seenInSessions', item.sessions.length)}
          </span>
        ) : null}
        <Age at={item.createdAt} />
      </div>
    </header>
  )
}

function ApprovalItem({ item, answer }: { item: AttentionItem; answer: Gate }) {
  const navigate = useNavigate()
  const [busy, setBusy] = useState<string | null>(null)
  const namespace = item.approval?.namespace || 'exec'

  async function resolve(member: AttentionMember, action: ApprovalAction): Promise<void> {
    if (!member.approval || busy) return
    setBusy(member.id)
    try {
      await approvalMonitor.resolve(member.approval, action)
    } catch {
      // resolve() already toasted; the item stays so it can be retried.
    } finally {
      setBusy(null)
    }
  }

  return (
    <article
      className="inbox-item tone-warn"
      data-kind="approval"
      data-item-id={item.id}
      aria-label={`${t('inbox.kindApproval')}: ${item.title}`}
    >
      <ItemHead
        item={item}
        icon={<ShieldAlertIcon />}
        kindLabel={`${t('inbox.kindApproval')} · ${namespace}`}
      />
      {item.detail ? <CommandLine command={item.detail} toastIdPrefix="inbox-copy" /> : null}
      <ul className="inbox-members">
        {item.members.map((member) => (
          <li key={member.id} className="inbox-member" data-member-id={member.id}>
            <SessionLink sessionKey={member.sessionKey} />
            <DisabledReason gate={answer}>
              <Button
                type="button"
                size="sm"
                disabled={answer.disabled || busy !== null}
                onClick={() => void resolve(member, 'once')}
              >
                <CheckIcon />
                <span>{t('inbox.approve')}</span>
              </Button>
              <Button
                type="button"
                size="sm"
                variant="destructive"
                disabled={answer.disabled || busy !== null}
                onClick={() => void resolve(member, 'deny')}
              >
                <XIcon />
                <span>{t('inbox.deny')}</span>
              </Button>
            </DisabledReason>
          </li>
        ))}
      </ul>
      <div className="inbox-item__foot">
        <button
          type="button"
          className="inbox-link"
          title={t('inbox.moreOptionsTitle')}
          onClick={() => void navigate('/approvals')}
        >
          {t('inbox.moreOptions')}
        </button>
      </div>
    </article>
  )
}

function QuestionItem({ item, answer }: { item: AttentionItem; answer: Gate }) {
  const rpc = useRpc()
  const questions: AskQuestion[] = item.questions ?? []
  const [selections, setSelections] = useState<string[][]>(() => questions.map(() => []))
  const [freeText, setFreeText] = useState('')
  const [sending, setSending] = useState(false)
  const composed = composeAskAnswer(questions, selections, freeText)
  const sessions = item.members.map((m) => m.sessionKey).filter((k): k is string => !!k)

  function toggle(qIndex: number, label: string, multi: boolean): void {
    setSelections((prev) =>
      prev.map((chosen, i) => {
        if (i !== qIndex) return chosen
        if (chosen.includes(label)) return chosen.filter((l) => l !== label)
        return multi ? [...chosen, label] : [label]
      }),
    )
  }

  async function send(): Promise<void> {
    if (!composed || sending || sessions.length === 0) return
    setSending(true)
    const elevated = useApprovals.getState().elevatedMode
    try {
      for (const sessionKey of sessions) {
        const params: Record<string, unknown> = { message: composed, sessionKey }
        if (elevated) params._source = { elevated }
        await rpc.call('chat.send', params)
        usePendingQuestions.getState().clearSession(sessionKey)
      }
      toast.success(t('inbox.sent'), { id: 'inbox-answer', duration: 2000 })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error(t('inbox.sendFailed', { message }), { id: 'inbox-answer-err', duration: 4000 })
    } finally {
      setSending(false)
    }
  }

  return (
    <article
      className="inbox-item tone-info"
      data-kind="question"
      data-item-id={item.id}
      aria-label={`${t('inbox.kindQuestion')}: ${item.title}`}
    >
      <ItemHead
        item={item}
        icon={<MessageCircleQuestionIcon />}
        kindLabel={t('inbox.kindQuestion')}
      />
      {questions.map((q, qIndex) => (
        <fieldset
          key={`${qIndex}:${q.question}`}
          className="inbox-question"
          aria-label={t('inbox.questionAnswerLandmark', { question: q.question })}
        >
          {questions.length > 1 || q.header ? (
            <legend className="inbox-question__text">{q.question}</legend>
          ) : null}
          <div className="inbox-chips">
            {q.options.map((opt) => {
              const on = selections[qIndex]?.includes(opt.label) ?? false
              return (
                <button
                  key={opt.label}
                  type="button"
                  className={`inbox-option${on ? ' is-on' : ''}`}
                  aria-pressed={on}
                  title={opt.description}
                  disabled={answer.disabled || sending}
                  onClick={() => toggle(qIndex, opt.label, q.multiSelect)}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>
        </fieldset>
      ))}
      <label className="inbox-question__free">
        <span className="t-label">{t('inbox.questionFreeText')}</span>
        <input
          type="text"
          value={freeText}
          placeholder={t('inbox.questionFreeTextPlaceholder')}
          disabled={answer.disabled || sending}
          onChange={(e) => setFreeText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void send()
            }
          }}
        />
      </label>
      <div className="inbox-item__foot">
        <div className="inbox-member__sessions">
          {item.members.map((m) => (
            <SessionLink key={m.id} sessionKey={m.sessionKey} />
          ))}
        </div>
        <DisabledReason gate={answer}>
          <Button
            type="button"
            size="sm"
            disabled={answer.disabled || sending || !composed}
            onClick={() => void send()}
          >
            {sending
              ? t('inbox.sending')
              : sessions.length > 1
                ? tPlural('inbox.sendToMany', sessions.length)
                : t('inbox.send')}
          </Button>
        </DisabledReason>
      </div>
    </article>
  )
}

function IssueItem({ item, issue }: { item: AttentionItem; issue: RuntimeIssue }) {
  const navigate = useNavigate()
  const retry = useRuntimeSignals((s) => s.retry)
  const dismiss = useDismissedIssues((s) => s.dismiss)
  const [stepsOpen, setStepsOpen] = useState(false)

  function run(action: IssueAction): void {
    switch (action.kind) {
      case 'retry_connection':
        retry()
        return
      case 'open_fix_steps':
        if (issue.fixSteps.length > 0) setStepsOpen((v) => !v)
        else void navigate('/health')
        return
      case 'open_settings': {
        const path = webSettingsPath(action.settings)
        if (path) void navigate(path)
        return
      }
      case 'dismiss':
        dismiss(issue.id)
        return
      default:
        // restart_gateway is only offered to a host that can run it.
        return
    }
  }

  const actions = issue.actions.filter(
    (a) =>
      a.kind !== 'restart_gateway' &&
      (a.kind !== 'open_settings' || webSettingsPath(a.settings) !== null),
  )
  const tone = issue.severity === 'error' ? 'danger' : 'warn'

  return (
    <article
      className={`inbox-item tone-${tone}`}
      data-kind="issue"
      data-issue-kind={issue.kind}
      data-item-id={item.id}
      aria-label={`${t('inbox.kindIssue')}: ${item.title}`}
    >
      <header className="inbox-item__head">
        <span className="inbox-item__icon" aria-hidden="true">
          <AlertTriangleIcon />
        </span>
        <div className="inbox-item__titles">
          <span className="t-label">
            {t('inbox.kindIssue')} ·{' '}
            {issue.severity === 'error' ? t('inbox.severityError') : t('inbox.severityWarn')}
          </span>
          <strong className="inbox-item__title">{issue.title}</strong>
        </div>
        {issue.findingId ? (
          <code className="inbox-item__code t-data">{issue.findingId}</code>
        ) : null}
      </header>
      {issue.detail ? <p className="inbox-item__detail">{issue.detail}</p> : null}
      {stepsOpen && issue.fixSteps.length > 0 ? (
        <ol className="inbox-steps" aria-label={t('inbox.fixSteps')}>
          {issue.fixSteps.map((step, i) => (
            <li key={`${i}:${step.label ?? ''}`}>
              {step.label ? <span className="inbox-steps__label">{step.label}</span> : null}
              {step.command ? (
                <CommandLine command={step.command} toastIdPrefix="inbox-fix-copy" />
              ) : null}
              {step.detail ? <span className="inbox-steps__detail">{step.detail}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
      <div className="inbox-item__foot inbox-item__actions">
        {actions.map((a) => (
          <Button
            key={a.kind}
            type="button"
            size="sm"
            variant={a.kind === 'dismiss' ? 'ghost' : 'outline'}
            aria-expanded={
              a.kind === 'open_fix_steps' && issue.fixSteps.length > 0 ? stepsOpen : undefined
            }
            data-action={a.kind}
            onClick={() => run(a)}
          >
            {a.label}
          </Button>
        ))}
        {issue.fixSteps.length > 0 ? (
          <button type="button" className="inbox-link" onClick={() => void navigate('/health')}>
            {t('inbox.openHealth')}
          </button>
        ) : null}
      </div>
    </article>
  )
}

/**
 * Whether the list is live: connected now (a silent socket is dropped by the
 * client within 2.5 tick intervals, so connected means frames arrive). The
 * age of the last frame is the status bar's job; the inbox shows the source
 * only, and so needs no clock.
 */
function useInboxSource(): 'live' | 'snapshot' | 'unavailable' {
  const rpcState = useConnection((s) => s.state)
  const everConnected = useRuntimeSignals((s) => s.everConnected)
  if (rpcState === 'connected') return 'live'
  return everConnected ? 'snapshot' : 'unavailable'
}

function SourceStat() {
  const source = useInboxSource()
  const label =
    source === 'live'
      ? t('inbox.sourceLive')
      : source === 'snapshot'
        ? t('inbox.sourceSnapshot')
        : t('inbox.sourceUnavailable')
  return (
    <div className="inbox-stat">
      <span className="t-label">{t('inbox.statSource')}</span>
      <strong className="t-data" data-source={source} title={t('inbox.sourceTitle')}>
        {label}
      </strong>
    </div>
  )
}

/** An empty list only means "nothing waits" while the view is live. */
function EmptyInbox() {
  const live = useInboxSource() === 'live'
  return (
    <section className="inbox-empty" data-live={live ? 'true' : 'false'}>
      <div className="inbox-empty__signal" aria-hidden="true">
        <InboxIcon />
      </div>
      <div className="inbox-empty__title">
        {live ? t('inbox.emptyTitle') : t('inbox.emptyUnknownTitle')}
      </div>
      <p className="inbox-empty__text">
        {live ? t('inbox.emptyText') : t('inbox.emptyUnknownText')}
      </p>
    </section>
  )
}

export function InboxPage() {
  const { items, issues } = useAttentionInbox()
  const rpcState = useConnection((s) => s.state)
  const refresh = useRuntimeSignals((s) => s.refresh)
  const dismissed = useDismissedIssues((s) => s.ids)
  const restoreAll = useDismissedIssues((s) => s.restoreAll)
  const [rechecking, setRechecking] = useState(false)
  const answer = gate(requireConnection(rpcState))

  useEffect(() => {
    document.title = t('inbox.documentTitle')
  }, [])

  // The approval list is pushed by the always-running monitor; read it once
  // on entry like the Approvals page does, so a stale list is not shown.
  useEffect(() => {
    void approvalMonitor.pollNow()
  }, [])

  async function recheck(): Promise<void> {
    if (rechecking) return
    setRechecking(true)
    try {
      refresh()
      await approvalMonitor.pollNow()
    } finally {
      setRechecking(false)
    }
  }

  const approvals = items.filter((i) => i.kind === 'approval')
  const questions = items.filter((i) => i.kind === 'question')
  const issueItems = items.filter((i) => i.kind === 'issue')
  const memberCount = (list: AttentionItem[]) => list.reduce((n, i) => n + i.members.length, 0)
  const hiddenIssues = issues.filter((i) => dismissed.includes(i.id)).length

  return (
    <div className="inbox-stage">
      <header className="inbox-stage__header">
        <div className="inbox-stage__title-block">
          <span className="t-label">{t('inbox.eyebrow')}</span>
          <h1 className="t-display">{t('inbox.title')}</h1>
          <p className="inbox-stage__subtitle">{t('inbox.subtitle')}</p>
        </div>
        <Button
          variant="outline"
          title={t('inbox.recheckTitle')}
          className="inbox-stage__recheck text-xs uppercase tracking-[0.14em]"
          disabled={rechecking}
          onClick={() => void recheck()}
        >
          <RefreshCwIcon />
          <span>{t('inbox.recheck')}</span>
        </Button>
      </header>

      <section className="inbox-summary" aria-label={t('inbox.summaryLandmark')}>
        <div className="inbox-stat">
          <span className="t-label">{t('inbox.statApprovals')}</span>
          <strong className="t-data" data-testid="inbox-stat-approvals">
            {memberCount(approvals)}
          </strong>
        </div>
        <div className="inbox-stat">
          <span className="t-label">{t('inbox.statQuestions')}</span>
          <strong className="t-data" data-testid="inbox-stat-questions">
            {memberCount(questions)}
          </strong>
        </div>
        <div className="inbox-stat">
          <span className="t-label">{t('inbox.statIssues')}</span>
          <strong className="t-data" data-testid="inbox-stat-issues">
            {memberCount(issueItems)}
          </strong>
        </div>
        <SourceStat />
      </section>

      {items.length === 0 ? (
        <EmptyInbox />
      ) : (
        <section className="inbox-list" aria-label={t('inbox.listLandmark')}>
          {items.map((item) =>
            item.kind === 'approval' ? (
              <ApprovalItem key={item.id} item={item} answer={answer} />
            ) : item.kind === 'question' ? (
              <QuestionItem key={item.id} item={item} answer={answer} />
            ) : item.issue ? (
              <IssueItem key={item.id} item={item} issue={item.issue} />
            ) : null,
          )}
        </section>
      )}

      <footer className="inbox-notes">
        <p>{t('inbox.questionsGap')}</p>
        {hiddenIssues > 0 ? (
          <p>
            {tPlural('inbox.dismissedNote', hiddenIssues)}{' '}
            <button type="button" className="inbox-link" onClick={restoreAll}>
              {t('inbox.restoreDismissed')}
            </button>
          </p>
        ) : null}
      </footer>
    </div>
  )
}
