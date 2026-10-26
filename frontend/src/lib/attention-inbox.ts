/**
 * The attention inbox: everything that is waiting on the operator, in one
 * list. Approvals of every namespace (from the approval monitor's store),
 * pending `ask_user` questions (pending-questions.ts) and typed runtime
 * issues (runtime-issues.ts).
 *
 * Pure. Items carry stable ids so a refresh never reshuffles what the
 * operator is reading, and duplicates are grouped: the same command waiting
 * for approval in three sessions is one item "seen in 3 sessions", with each
 * occurrence still answerable on its own.
 */
import { approvalCommand, type Approval } from '@/services/approval-monitor'
import type { AskQuestion } from '@/lib/ask-questions'
import type { PendingQuestion } from '@/lib/pending-questions'
import type { IssueSeverity, RuntimeIssue } from '@/lib/runtime-issues'

export type AttentionKind = 'approval' | 'question' | 'issue'

export interface AttentionMember {
  /** The member's own stable id (`approval:<ns>:<id>`, `question:<session>`, issue id). */
  id: string
  sessionKey: string | null
  createdAt: number | null
  approval?: Approval
  question?: PendingQuestion
}

export interface AttentionItem {
  /** Stable group id: kind plus a fingerprint of what is being asked. */
  id: string
  kind: AttentionKind
  severity: IssueSeverity
  title: string
  detail: string
  members: AttentionMember[]
  /** Distinct sessions the members come from. */
  sessions: string[]
  createdAt: number | null
  /** Approval items: the first member's approval (title / command source). */
  approval?: Approval
  /** Question items: the questions, identical across members. */
  questions?: AskQuestion[]
  issue?: RuntimeIssue
}

export interface AttentionInput {
  approvals: readonly Approval[]
  questions: readonly PendingQuestion[]
  issues: readonly RuntimeIssue[]
  /** Dismissed issue ids (session-scoped). Approvals and questions cannot be dismissed. */
  dismissed?: readonly string[]
}

/** FNV-1a, 32-bit, hex. Enough to keep ids short and stable. */
export function fingerprint(text: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(16).padStart(8, '0')
}

function approvalCreatedAt(item: Approval): number | null {
  const raw = item.created_at
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  // The queue stamps seconds; tolerate milliseconds too.
  return raw < 1e12 ? raw * 1000 : raw
}

function approvalTitle(item: Approval): string {
  return item.toolName || item.actionKind || item.namespace || 'approval'
}

/** One line per question, options included: two asks group only when identical. */
function questionsFingerprint(questions: readonly AskQuestion[]): string {
  return questions
    .map(
      (q) =>
        `${q.question}|${q.multiSelect ? 'm' : 's'}|${q.options.map((o) => o.label).join(',')}`,
    )
    .join('||')
}

function distinct(values: Array<string | null>): string[] {
  return [...new Set(values.filter((v): v is string => !!v))]
}

export function buildAttentionItems(input: AttentionInput): AttentionItem[] {
  const dismissed = new Set(input.dismissed ?? [])
  const groups = new Map<string, AttentionItem>()
  const order: string[] = []

  const add = (item: AttentionItem): void => {
    const existing = groups.get(item.id)
    if (existing) {
      existing.members.push(...item.members)
      existing.sessions = distinct([...existing.sessions, ...item.sessions])
      if (
        item.createdAt !== null &&
        (existing.createdAt === null || item.createdAt < existing.createdAt)
      ) {
        existing.createdAt = item.createdAt
      }
      return
    }
    groups.set(item.id, item)
    order.push(item.id)
  }

  for (const approval of input.approvals) {
    const namespace = approval.namespace || 'exec'
    const command = approvalCommand(approval)
    // No command means nothing to compare on: never group those.
    const key = command
      ? `${namespace}|${approvalTitle(approval)}|${command}`
      : `${namespace}|${approval.id}`
    const createdAt = approvalCreatedAt(approval)
    add({
      id: `approval:${fingerprint(key)}`,
      kind: 'approval',
      severity: 'warn',
      title: approvalTitle(approval),
      detail: command,
      members: [
        {
          id: `approval:${namespace}:${approval.id}`,
          sessionKey: approval.sessionKey || null,
          createdAt,
          approval,
        },
      ],
      sessions: distinct([approval.sessionKey || null]),
      createdAt,
      approval,
    })
  }

  for (const question of input.questions) {
    const first = question.questions[0]
    add({
      id: `question:${fingerprint(questionsFingerprint(question.questions))}`,
      kind: 'question',
      severity: 'warn',
      title: first?.header || first?.question || '',
      detail:
        question.questions.length > 1 ? question.questions.map((q) => q.question).join(' · ') : '',
      members: [
        {
          id: question.id,
          sessionKey: question.sessionKey,
          createdAt: question.presentedAt,
          question,
        },
      ],
      sessions: [question.sessionKey],
      createdAt: question.presentedAt,
      questions: question.questions,
    })
  }

  for (const issue of input.issues) {
    if (dismissed.has(issue.id)) continue
    add({
      id: issue.id,
      kind: 'issue',
      severity: issue.severity,
      title: issue.title,
      detail: issue.detail,
      members: [{ id: issue.id, sessionKey: null, createdAt: null }],
      sessions: [],
      createdAt: null,
      issue,
    })
  }

  // Approvals block a running agent, so they lead; then questions; then
  // issues, errors before warnings. Oldest first within approvals/questions.
  const rank = (item: AttentionItem): number =>
    item.kind === 'approval' ? 0 : item.kind === 'question' ? 1 : item.severity === 'error' ? 2 : 3
  return order
    .map((id, index) => ({ item: groups.get(id)!, index }))
    .sort((a, b) => {
      const byRank = rank(a.item) - rank(b.item)
      if (byRank) return byRank
      const ta = a.item.createdAt ?? Number.POSITIVE_INFINITY
      const tb = b.item.createdAt ?? Number.POSITIVE_INFINITY
      if (ta !== tb) return ta - tb
      return a.index - b.index
    })
    .map(({ item }) => item)
}

/** How many answers the inbox is waiting for: every member counts. */
export function attentionCount(items: readonly AttentionItem[]): number {
  return items.reduce((sum, item) => sum + item.members.length, 0)
}
