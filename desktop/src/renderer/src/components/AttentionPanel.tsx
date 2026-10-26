import './attention-panel.css'
import { AlertTriangle, Inbox, MessageCircleQuestion, ShieldAlert } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { composeAskAnswer } from '@/lib/ask-questions'
import type { AttentionItem, AttentionMember } from '@/lib/attention-inbox'
import { gate, requireConnection, type Gate } from '@/lib/disabled-reason'
import { usePendingQuestions } from '@/lib/pending-questions'
import {
  useDismissedIssues,
  type IssueAction,
  type RuntimeIssue,
  type SettingsTarget,
} from '@/lib/runtime-issues'
import { useRuntimeSignals } from '@/lib/runtime-signals'
import { useAttentionInbox } from '@/lib/useAttentionInbox'
import { useRuntimeTruth } from '@/lib/useRuntimeTruth'
import { approvalMonitor, useApprovals, type ApprovalAction } from '@/services/approval-monitor'
import { useConnection } from '@/stores/connection'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { Button } from '~/components/ui/button'
import { t } from '~/i18n'
import { useRuntimeHost } from '~/lib/use-runtime-host'
import { cn } from '~/lib/utils'
import { useAttentionPanel } from '~/stores/attention'
import { useGateway } from '~/stores/gateway'
import { useSessions } from '~/stores/sessions'
import { useUi } from '~/stores/ui'
import type { SettingsSection } from '~/views/settings/sections'

/** Where an issue's "Open settings" lands in the desktop's Settings sheet. */
const SETTINGS_FOR: Record<SettingsTarget, SettingsSection> = {
  connection: 'gateway',
  providers: 'providers',
  channels: 'channels',
  updates: 'about',
}

/**
 * The toolbar's "needs you" panel: the desktop's attention inbox. Same list
 * as the web console's Inbox (`useAttentionInbox`: approvals of every
 * namespace, `ask_user` questions, typed runtime issues), its own compact
 * popover. Everything is answerable in place; the desktop can also restart
 * its own gateway, after a confirmation.
 */
export function AttentionPanel() {
  const host = useRuntimeHost()
  const { count } = useAttentionInbox(host)
  const open = useAttentionPanel((s) => s.open)
  const setOpen = useAttentionPanel((s) => s.setOpen)
  const popId = useId()
  const label = t('attention.button')

  return (
    <div className="attn">
      <Button
        variant={open ? 'secondary' : 'ghost'}
        size="icon"
        aria-label={count > 0 ? `${label} (${count})` : label}
        title={label}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        onClick={() => setOpen(!open)}
      >
        <span className="relative flex">
          <Inbox className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
          {count > 0 ? (
            <span className="attn__count" aria-hidden data-testid="attention-count">
              {count > 9 ? '9+' : count}
            </span>
          ) : null}
        </span>
      </Button>
      {open ? <AttentionPopover id={popId} onClose={() => setOpen(false)} /> : null}
    </div>
  )
}

function AttentionPopover({ id, onClose }: { id: string; onClose: () => void }) {
  const host = useRuntimeHost()
  const { items, issues } = useAttentionInbox(host)
  const rpcState = useConnection((s) => s.state)
  const dismissed = useDismissedIssues((s) => s.ids)
  const restoreAll = useDismissedIssues((s) => s.restoreAll)
  const ref = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  const answer = gate(requireConnection(rpcState))
  const hidden = issues.filter((i) => dismissed.includes(i.id)).length

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCloseRef.current()
      }
    }
    const onDown = (e: MouseEvent) => {
      const anchor = ref.current?.parentElement
      if (anchor && !anchor.contains(e.target as Node)) onCloseRef.current()
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [])

  return (
    <div
      ref={ref}
      id={id}
      className="attn__pop app-no-drag"
      role="dialog"
      aria-label={t('attention.title')}
    >
      <div className="attn__head">
        <span className="attn__title">{t('attention.title')}</span>
      </div>
      {items.length === 0 ? (
        <EmptyPanel />
      ) : (
        <ul className="attn__list">
          {items.map((item) => (
            <li key={item.id}>
              {item.kind === 'approval' ? (
                <ApprovalRow item={item} answer={answer} onNavigate={onClose} />
              ) : item.kind === 'question' ? (
                <QuestionRow item={item} answer={answer} onNavigate={onClose} />
              ) : item.issue ? (
                <IssueRow
                  issue={item.issue}
                  canRestart={!!host.canRestartGateway}
                  onNavigate={onClose}
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <div className="attn__foot">
        <span>{t('attention.gap')}</span>
        {hidden > 0 ? (
          <button type="button" className="attn__link" onClick={restoreAll}>
            {t('attention.dismissed').replace('{count}', String(hidden))} · {t('attention.restore')}
          </button>
        ) : null}
      </div>
    </div>
  )
}

/**
 * Empty only means "nothing waits" while the view is live. Its own component:
 * the truth hook ages every second and only this needs to follow it.
 */
function EmptyPanel() {
  const live = useRuntimeTruth(useRuntimeHost()).source === 'live'
  return (
    <div className="attn__empty">
      <span className="attn__empty-title">
        {live ? t('attention.empty') : t('attention.empty.unknown')}
      </span>
      <span>{live ? t('attention.empty.help') : t('attention.empty.unknown.help')}</span>
    </div>
  )
}

/** The reason under a disabled row of buttons (the disabled-with-reason convention). */
function Reason({ gate: g }: { gate: Gate }) {
  if (!g.disabled || !g.reason) return null
  return (
    <p className="attn__reason" data-disabled-reason={g.code ?? ''}>
      {g.reason}
    </p>
  )
}

function SessionButton({
  member,
  onNavigate,
}: {
  member: AttentionMember
  onNavigate: () => void
}) {
  const navigate = useNavigate()
  const { rows } = useSessions()
  if (!member.sessionKey) return <span className="attn__session">{t('attention.noSession')}</span>
  const key = member.sessionKey
  const title = rows.find((r) => r.key === key)?.title || key
  return (
    <button
      type="button"
      className="attn__session attn__link"
      title={key}
      onClick={() => {
        onNavigate()
        void navigate(sessionPath(key))
      }}
    >
      {title}
    </button>
  )
}

function SeenIn({ item }: { item: AttentionItem }) {
  if (item.sessions.length < 2) return null
  return (
    <span className="attn__seen" data-testid="attention-seen-in">
      {t('attention.seenIn').replace('{count}', String(item.sessions.length))}
    </span>
  )
}

function ApprovalRow({
  item,
  answer,
  onNavigate,
}: {
  item: AttentionItem
  answer: Gate
  onNavigate: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  async function resolve(member: AttentionMember, action: ApprovalAction) {
    if (!member.approval || busy) return
    setBusy(member.id)
    try {
      await approvalMonitor.resolve(member.approval, action)
    } catch {
      // resolve() already toasted; the row stays for a retry.
    } finally {
      setBusy(null)
    }
  }
  return (
    <article className="attn__item" data-tone="warn" data-kind="approval" aria-label={item.title}>
      <div className="attn__item-head">
        <ShieldAlert className="size-3.5" strokeWidth={1.75} aria-hidden />
        <span className="attn__kind">{t('attention.kind.approval')}</span>
        <span className="attn__item-title">{item.title}</span>
        <SeenIn item={item} />
      </div>
      {item.detail ? <pre className="attn__command">{item.detail}</pre> : null}
      {item.members.map((member) => (
        <div key={member.id} className="attn__member" data-member-id={member.id}>
          <SessionButton member={member} onNavigate={onNavigate} />
          <div className="attn__buttons">
            <Button
              disabled={answer.disabled || busy !== null}
              onClick={() => void resolve(member, 'deny')}
            >
              {t('attention.deny')}
            </Button>
            <Button
              variant="primary"
              disabled={answer.disabled || busy !== null}
              onClick={() => void resolve(member, 'once')}
            >
              {t('attention.approve')}
            </Button>
          </div>
        </div>
      ))}
      <Reason gate={answer} />
    </article>
  )
}

function QuestionRow({
  item,
  answer,
  onNavigate,
}: {
  item: AttentionItem
  answer: Gate
  onNavigate: () => void
}) {
  const rpc = useRpc()
  const questions = item.questions ?? []
  const [selections, setSelections] = useState<string[][]>(() => questions.map(() => []))
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const composed = composeAskAnswer(questions, selections, text)
  const sessions = item.members.map((m) => m.sessionKey).filter((k): k is string => !!k)

  function pick(qIndex: number, label: string, multi: boolean) {
    setSelections((prev) =>
      prev.map((chosen, i) =>
        i !== qIndex
          ? chosen
          : chosen.includes(label)
            ? chosen.filter((l) => l !== label)
            : multi
              ? [...chosen, label]
              : [label],
      ),
    )
  }

  async function send() {
    if (!composed || sending) return
    setSending(true)
    const elevated = useApprovals.getState().elevatedMode
    try {
      for (const sessionKey of sessions) {
        const params: Record<string, unknown> = { message: composed, sessionKey }
        if (elevated) params._source = { elevated }
        await rpc.call('chat.send', params)
        usePendingQuestions.getState().clearSession(sessionKey)
      }
      toast.success(t('attention.sent'), { id: 'attention-answer', duration: 2000 })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast.error(`${t('attention.sendFailed')}: ${message}`, { id: 'attention-answer-err' })
    } finally {
      setSending(false)
    }
  }

  return (
    <article className="attn__item" data-tone="info" data-kind="question" aria-label={item.title}>
      <div className="attn__item-head">
        <MessageCircleQuestion className="size-3.5" strokeWidth={1.75} aria-hidden />
        <span className="attn__kind">{t('attention.kind.question')}</span>
        <SeenIn item={item} />
      </div>
      {questions.map((q, qIndex) => (
        <div key={`${qIndex}:${q.question}`} className="attn__question">
          <span className="attn__question-text">{q.question}</span>
          <div className="attn__chips">
            {q.options.map((opt) => {
              const on = selections[qIndex]?.includes(opt.label) ?? false
              return (
                <button
                  key={opt.label}
                  type="button"
                  className={cn('attn__chip', on && 'attn__chip--on')}
                  aria-pressed={on}
                  title={opt.description}
                  disabled={answer.disabled || sending}
                  onClick={() => pick(qIndex, opt.label, q.multiSelect)}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>
        </div>
      ))}
      <div className="attn__reply">
        <input
          className="mac-input"
          value={text}
          placeholder={t('attention.reply')}
          aria-label={t('attention.reply')}
          disabled={answer.disabled || sending}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void send()
            }
          }}
        />
        <Button
          variant="primary"
          disabled={answer.disabled || sending || !composed}
          onClick={() => void send()}
        >
          {sessions.length > 1
            ? t('attention.sendMany').replace('{count}', String(sessions.length))
            : t('attention.send')}
        </Button>
      </div>
      <div className="attn__members">
        {item.members.map((m) => (
          <SessionButton key={m.id} member={m} onNavigate={onNavigate} />
        ))}
      </div>
      <Reason gate={answer} />
    </article>
  )
}

function IssueRow({
  issue,
  canRestart,
  onNavigate,
}: {
  issue: RuntimeIssue
  canRestart: boolean
  onNavigate: () => void
}) {
  const retry = useRuntimeSignals((s) => s.retry)
  const dismiss = useDismissedIssues((s) => s.dismiss)
  const restart = useGateway((s) => s.restart)
  const restarting = useGateway((s) => s.busy)
  const openSettings = useUi((s) => s.openSettings)
  const [steps, setSteps] = useState(false)
  const [confirming, setConfirming] = useState<IssueAction | null>(null)

  function run(action: IssueAction) {
    switch (action.kind) {
      case 'restart_gateway':
        setConfirming(action)
        return
      case 'retry_connection':
        retry()
        return
      case 'open_fix_steps':
        setSteps((v) => !v)
        return
      case 'open_settings':
        onNavigate()
        openSettings(SETTINGS_FOR[action.settings ?? 'connection'])
        return
      case 'dismiss':
        dismiss(issue.id)
    }
  }

  const actions = issue.actions.filter(
    (a) =>
      (a.kind !== 'restart_gateway' || canRestart) &&
      (a.kind !== 'open_fix_steps' || issue.fixSteps.length > 0),
  )

  return (
    <article
      className="attn__item"
      data-tone={issue.severity === 'error' ? 'danger' : 'warn'}
      data-kind="issue"
      data-issue-kind={issue.kind}
      aria-label={issue.title}
    >
      <div className="attn__item-head">
        <AlertTriangle className="size-3.5" strokeWidth={1.75} aria-hidden />
        <span className="attn__kind">{t('attention.kind.issue')}</span>
        <span className="attn__item-title">{issue.title}</span>
      </div>
      {issue.detail ? <p className="attn__detail">{issue.detail}</p> : null}
      {steps ? (
        <ol className="attn__steps" aria-label={t('attention.fixSteps')}>
          {issue.fixSteps.map((step, i) => (
            <li key={`${i}:${step.label ?? ''}`}>
              {step.label ? <span>{step.label}</span> : null}
              {step.command ? <code>{step.command}</code> : null}
              {step.detail ? <span className="attn__step-detail">{step.detail}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}
      {confirming ? (
        <div className="attn__confirm" role="alert">
          <span>{confirming.confirm}</span>
          <div className="attn__buttons">
            <Button onClick={() => setConfirming(null)}>{t('attention.restart.cancel')}</Button>
            <Button
              variant="danger"
              disabled={restarting}
              onClick={() => {
                setConfirming(null)
                toast(t('attention.restarted'), { id: 'attention-restart', duration: 2500 })
                void restart()
              }}
            >
              {t('attention.restart.go')}
            </Button>
          </div>
        </div>
      ) : (
        <div className="attn__buttons attn__buttons--start">
          {actions.map((a) => (
            <Button
              key={a.kind}
              variant={a.kind === 'dismiss' ? 'ghost' : 'secondary'}
              data-action={a.kind}
              aria-expanded={a.kind === 'open_fix_steps' ? steps : undefined}
              onClick={() => run(a)}
            >
              {a.label}
            </Button>
          ))}
        </div>
      )}
    </article>
  )
}
