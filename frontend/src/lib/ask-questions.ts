// The ask_user tool's presented-question payload: pure parse/compose helpers.
//
// The ask_user tool follows an end-turn-and-resume contract: presenting the
// questions ends the turn, and the answer arrives as the next user message.
// These helpers read a `tool_result` frame and compose that answer. They live
// here (not in the chat transcript module) because the attention inbox reads
// the same frames from the shell, which must not carry the chat view's copy.

export interface AskOption {
  label: string
  description?: string
}

export interface AskQuestion {
  question: string
  header?: string
  options: AskOption[]
  multiSelect: boolean
}

const ASK_TOOL_NAME = 'ask_user'
const ASK_STATUS_PRESENTED = 'question_presented'

/**
 * Parse presented questions from an ask_user `tool_result` stream frame.
 * Returns null for other tools, error results, or malformed payloads —
 * rendering keys off the RESULT (not the arguments) so a validation
 * failure never draws a half-formed card.
 */
export function parseAskQuestions(payload: unknown): AskQuestion[] | null {
  const p = payload as {
    name?: string
    tool_name?: string
    is_error?: boolean
    isError?: boolean
    result?: unknown
  }
  const toolName = p.name || p.tool_name || ''
  if (toolName !== ASK_TOOL_NAME || p.is_error || p.isError) return null
  let parsed: unknown = p.result
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch {
      return null
    }
  }
  if (!parsed || typeof parsed !== 'object') return null
  const body = parsed as { status?: unknown; questions?: unknown }
  if (body.status !== ASK_STATUS_PRESENTED || !Array.isArray(body.questions)) return null

  const questions: AskQuestion[] = []
  for (const raw of body.questions) {
    if (!raw || typeof raw !== 'object') return null
    const q = raw as {
      question?: unknown
      header?: unknown
      options?: unknown
      multi_select?: unknown
    }
    const question = typeof q.question === 'string' ? q.question.trim() : ''
    if (!question || !Array.isArray(q.options)) return null
    const options: AskOption[] = []
    for (const rawOpt of q.options) {
      if (!rawOpt || typeof rawOpt !== 'object') return null
      const opt = rawOpt as { label?: unknown; description?: unknown }
      const label = typeof opt.label === 'string' ? opt.label.trim() : ''
      if (!label) return null
      const option: AskOption = { label }
      if (typeof opt.description === 'string' && opt.description.trim()) {
        option.description = opt.description.trim()
      }
      options.push(option)
    }
    if (options.length < 2) return null
    const entry: AskQuestion = {
      question,
      options,
      multiSelect: q.multi_select === true,
    }
    if (typeof q.header === 'string' && q.header.trim()) entry.header = q.header.trim()
    questions.push(entry)
  }
  return questions.length > 0 ? questions : null
}

/**
 * Compose the outgoing user message from per-question selections plus an
 * optional free-text addition. Single question → just the answer text;
 * multiple questions → one `Header: answer` line per answered question.
 */
export function composeAskAnswer(
  questions: AskQuestion[],
  selections: string[][],
  freeText = '',
): string {
  const lines: string[] = []
  const multi = questions.length > 1
  questions.forEach((q, i) => {
    const chosen = (selections[i] || []).filter(Boolean)
    if (chosen.length === 0) return
    const answer = chosen.join(', ')
    lines.push(multi ? `${q.header || q.question}: ${answer}` : answer)
  })
  const extra = freeText.trim()
  if (extra) lines.push(extra)
  return lines.join('\n')
}
