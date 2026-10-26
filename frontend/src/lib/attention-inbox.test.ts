import { describe, expect, it } from 'vitest'
import { attentionCount, buildAttentionItems, fingerprint } from './attention-inbox'
import type { PendingQuestion } from './pending-questions'
import type { RuntimeIssue } from './runtime-issues'
import type { Approval } from '@/services/approval-monitor'

const approval = (
  id: string,
  sessionKey: string,
  command = 'rm -rf build',
  created_at = 100,
): Approval => ({
  id,
  namespace: 'exec',
  toolName: 'bash',
  command,
  sessionKey,
  created_at,
})

const question = (
  sessionKey: string,
  presentedAt = 500,
  text = 'Which branch?',
): PendingQuestion => ({
  id: `question:${sessionKey}`,
  sessionKey,
  toolUseId: null,
  presentedAt,
  questions: [
    { question: text, options: [{ label: 'main' }, { label: 'dev' }], multiSelect: false },
  ],
})

const issue = (id: string, severity: 'error' | 'warn'): RuntimeIssue => ({
  id,
  kind: 'doctor_warning',
  severity,
  title: id,
  detail: '',
  findingId: null,
  surface: null,
  fixSteps: [],
  actions: [],
})

describe('buildAttentionItems', () => {
  it('merges approvals, questions and issues into one ordered list', () => {
    const items = buildAttentionItems({
      approvals: [approval('a1', 's1')],
      questions: [question('s2')],
      issues: [issue('issue:w', 'warn'), issue('issue:e', 'error')],
    })
    expect(items.map((i) => i.kind)).toEqual(['approval', 'question', 'issue', 'issue'])
    expect(items.slice(2).map((i) => i.id)).toEqual(['issue:e', 'issue:w'])
    expect(attentionCount(items)).toBe(4)
  })

  it('groups the same command across sessions and keeps each one answerable', () => {
    const items = buildAttentionItems({
      approvals: [
        approval('a1', 's1', 'rm -rf build', 300),
        approval('a2', 's2', 'rm -rf build', 200),
        approval('a3', 's3', 'ls'),
      ],
      questions: [],
      issues: [],
    })
    expect(items).toHaveLength(2)
    const grouped = items.find((i) => i.members.length === 2)!
    expect(grouped.sessions).toEqual(['s1', 's2'])
    expect(grouped.members.map((m) => m.id)).toEqual(['approval:exec:a1', 'approval:exec:a2'])
    // The group dates from its oldest occurrence (seconds -> ms).
    expect(grouped.createdAt).toBe(200_000)
    expect(attentionCount(items)).toBe(3)
  })

  it('keeps ids stable across rebuilds and independent of order', () => {
    const a = buildAttentionItems({
      approvals: [approval('a1', 's1')],
      questions: [question('s9')],
      issues: [],
    })
    const b = buildAttentionItems({
      approvals: [approval('a1', 's1')],
      questions: [question('s9')],
      issues: [],
    })
    expect(a.map((i) => i.id)).toEqual(b.map((i) => i.id))
    expect(a[0]!.id).toBe(`approval:${fingerprint('exec|bash|rm -rf build')}`)
  })

  it('never groups approvals that have no command to compare', () => {
    const items = buildAttentionItems({
      approvals: [approval('a1', 's1', ''), approval('a2', 's2', '')],
      questions: [],
      issues: [],
    })
    expect(items).toHaveLength(2)
  })

  it('groups an identical question asked in several sessions', () => {
    const items = buildAttentionItems({
      approvals: [],
      questions: [question('s1', 10), question('s2', 20), question('s3', 30, 'Ship it?')],
      issues: [],
    })
    expect(items).toHaveLength(2)
    expect(items[0]!.sessions).toEqual(['s1', 's2'])
    expect(items[0]!.title).toBe('Which branch?')
  })

  it('leaves out issues dismissed for this session', () => {
    const items = buildAttentionItems({
      approvals: [],
      questions: [],
      issues: [issue('issue:a', 'warn'), issue('issue:b', 'warn')],
      dismissed: ['issue:a'],
    })
    expect(items.map((i) => i.id)).toEqual(['issue:b'])
  })
})
