// Chat transcript — ask_user interactive question card.
//
// AgentOS-native (no legacy chat.js counterpart). The ask_user tool follows
// an end-turn-and-resume contract: the backend terminates the turn after the
// tool runs and the user's answer arrives as the next user message. This
// module renders the presented questions as a clickable card in the live
// stream bubble; clicking sends the composed answer through the same
// `chat.send` path the composer uses (injected as `sendAnswer`). Typing a
// free-text reply in the composer always works too — the card is a
// convenience, not the mechanism.
//
// Split mirrors tools.ts: pure parse/compose helpers (unit-tested in
// ask.test.ts) + an imperative DOM builder composed by the tool renderer.

import { t } from '@/i18n'
import '@/i18n/en/chat'

import { composeAskAnswer, type AskQuestion } from '@/lib/ask-questions'

/* ── Pure helpers (unit-tested; they live in @/lib/ask-questions) ───────── */

export {
  composeAskAnswer,
  parseAskQuestions,
  type AskOption,
  type AskQuestion,
} from '@/lib/ask-questions'

/**
 * Mark ask cards that already have a user message after them as answered and
 * disable their controls. History reconstruction rebuilds bubbles from
 * persisted segments, so a card that was answered in a previous turn would
 * otherwise come back active.
 */
export function lockAnsweredAskCards(container: HTMLElement): void {
  container
    .querySelectorAll<HTMLElement>('.chat-ask-card:not(.chat-ask-card--answered)')
    .forEach((card) => {
      const row = card.closest<HTMLElement>('.msg')
      if (!row) return
      let el = row.nextElementSibling
      while (el) {
        const isUserRow =
          el.classList.contains('user') || el.getAttribute('data-history-role') === 'user'
        if (isUserRow) {
          card.classList.add('chat-ask-card--answered')
          card
            .querySelectorAll<HTMLButtonElement | HTMLInputElement>('button, input')
            .forEach((c) => {
              c.disabled = true
            })
          return
        }
        el = el.nextElementSibling
      }
    })
}

/* ── SVG helpers (Lucide paths; stroke = currentColor, no emoji icons) ──── */

const SVG_NS = 'http://www.w3.org/2000/svg'

/** Build a stroke-based icon svg from raw path/shape markup. */
export function buildCardIconSVG(inner: string, viewBox = '0 0 24 24'): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('viewBox', viewBox)
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')
  svg.setAttribute('aria-hidden', 'true')
  svg.innerHTML = inner
  return svg
}

export const CARD_ICON_QUESTION =
  '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'
export const CARD_ICON_CHECK = '<path d="M20 6 9 17l-5-5"/>'

/* ── Imperative DOM builder (composed by the tool renderer) ─────────────── */

export function buildAskCardDOM(
  questions: AskQuestion[],
  sendAnswer: ((text: string) => boolean) | undefined,
): HTMLElement {
  const card = document.createElement('div')
  card.className = 'chat-ask-card'
  card.setAttribute('role', 'group')
  card.setAttribute('aria-label', t('chat.askCardLabel'))

  // selections[qi] = list of selected labels for question qi.
  const selections: string[][] = questions.map(() => [])
  let answered = false

  const eyebrow = document.createElement('div')
  eyebrow.className = 'chat-ask-eyebrow'
  eyebrow.appendChild(buildCardIconSVG(CARD_ICON_QUESTION))
  eyebrow.appendChild(document.createTextNode(t('chat.askCardLabel')))
  card.appendChild(eyebrow)

  // One-question single-select cards send on click; everything else
  // toggles selections and submits through the Send button.
  const instantSend = questions.length === 1 && !questions[0]?.multiSelect && !!sendAnswer

  // Free-text alternative, created in the footer below; declared ahead of
  // `submit` so the closure reads the assigned input at click time.
  let otherInput: HTMLInputElement | null = null

  const submit = (): void => {
    if (answered || !sendAnswer) return
    const text = composeAskAnswer(questions, selections, otherInput ? otherInput.value : '')
    if (!text) return
    // The sender reports whether the message actually went out (false while
    // the stream is still settling); only then does the card lock itself.
    if (!sendAnswer(text)) return
    answered = true
    card.classList.add('chat-ask-card--answered')
    card.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button, input').forEach((el) => {
      el.disabled = true
    })
    const done = document.createElement('div')
    done.className = 'chat-ask-answered'
    done.appendChild(buildCardIconSVG(CARD_ICON_CHECK))
    done.appendChild(document.createTextNode(`${t('chat.askAnswered')}: ${text}`))
    card.appendChild(done)
  }

  questions.forEach((q, qi) => {
    const block = document.createElement('div')
    block.className = 'chat-ask-question'

    const title = document.createElement('div')
    title.className = 'chat-ask-title'
    if (q.header) {
      const chip = document.createElement('span')
      chip.className = 'chat-ask-header-chip'
      chip.textContent = q.header
      title.appendChild(chip)
    }
    title.appendChild(document.createTextNode(q.question))
    block.appendChild(title)

    if (q.multiSelect) {
      const hint = document.createElement('div')
      hint.className = 'chat-ask-multi-hint'
      hint.textContent = t('chat.askMultiHint')
      block.appendChild(hint)
    }

    const optionRow = document.createElement('div')
    optionRow.className = 'chat-ask-options'
    q.options.forEach((opt) => {
      const btn = document.createElement('button')
      btn.type = 'button'
      btn.className = 'chat-ask-option' + (q.multiSelect ? ' chat-ask-option--multi' : '')
      btn.setAttribute('aria-pressed', 'false')
      if (!sendAnswer) btn.disabled = true
      const check = document.createElement('span')
      check.className = 'chat-ask-option-check'
      check.setAttribute('aria-hidden', 'true')
      check.appendChild(buildCardIconSVG(CARD_ICON_CHECK))
      btn.appendChild(check)
      const textCol = document.createElement('span')
      textCol.className = 'chat-ask-option-text'
      const label = document.createElement('span')
      label.className = 'chat-ask-option-label'
      label.textContent = opt.label
      textCol.appendChild(label)
      if (opt.description) {
        const desc = document.createElement('span')
        desc.className = 'chat-ask-option-desc'
        desc.textContent = opt.description
        textCol.appendChild(desc)
      }
      btn.appendChild(textCol)
      btn.addEventListener('click', () => {
        if (answered) return
        if (instantSend) {
          selections[qi] = [opt.label]
          submit()
          return
        }
        let current = selections[qi]
        if (!current) {
          current = []
          selections[qi] = current
        }
        const at = current.indexOf(opt.label)
        if (q.multiSelect) {
          if (at >= 0) current.splice(at, 1)
          else current.push(opt.label)
          btn.classList.toggle('chat-ask-option--selected', at < 0)
          btn.setAttribute('aria-pressed', at < 0 ? 'true' : 'false')
        } else {
          selections[qi] = [opt.label]
          optionRow.querySelectorAll('.chat-ask-option--selected').forEach((el) => {
            el.classList.remove('chat-ask-option--selected')
            el.setAttribute('aria-pressed', 'false')
          })
          btn.classList.add('chat-ask-option--selected')
          btn.setAttribute('aria-pressed', 'true')
        }
      })
      optionRow.appendChild(btn)
    })
    block.appendChild(optionRow)
    card.appendChild(block)
  })

  // Free-text alternative: always offered, so the card never traps the user
  // in the listed options.
  const footer = document.createElement('div')
  footer.className = 'chat-ask-footer'
  otherInput = document.createElement('input')
  otherInput.type = 'text'
  otherInput.className = 'chat-ask-other'
  otherInput.placeholder = t('chat.askOtherPlaceholder')
  if (!sendAnswer) otherInput.disabled = true
  otherInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      submit()
    }
  })
  footer.appendChild(otherInput)
  if (!instantSend) {
    const sendBtn = document.createElement('button')
    sendBtn.type = 'button'
    sendBtn.className = 'chat-ask-send'
    sendBtn.textContent = t('chat.askSend')
    if (!sendAnswer) sendBtn.disabled = true
    sendBtn.addEventListener('click', submit)
    footer.appendChild(sendBtn)
  }
  card.appendChild(footer)

  return card
}
