/**
 * Ticketed download URLs for artifacts and attachments.
 *
 * The gateway's file routes (`/api/v1/artifacts/<id>`,
 * `/api/v1/attachments/<sha256>`) want the connection's Authorization header,
 * which an `<img>`, an `<audio>` or a plain link cannot send. The
 * `artifacts.download` RPC trades a file for a URL that needs no header at
 * all: it is bound to the WebSocket that minted it and lives five minutes, or
 * until that socket closes. This module turns the gateway URLs the transcript
 * already renders into those tickets, and fills `data-ticket-src` media with
 * them.
 *
 * An older gateway has no such method. Every resolver call then answers null
 * and the caller keeps its legacy URL (and its header-authenticated fetch).
 */
import { apiUrl, urlBase } from './api-origin'

export const TICKET_METHOD = 'artifacts.download'
const TICKET_CAPABILITY = 'artifact-tickets'
export const THUMBNAIL_VARIANT = 'thumbnail'

/** A cached ticket stops being handed out this long before it expires. */
export const TICKET_REFRESH_MARGIN_MS = 30_000
const DEFAULT_TTL_MS = 300_000

/** Answers that mean "no tickets on this connection", not "this file failed". */
const UNAVAILABLE_CODES = new Set(['METHOD_NOT_FOUND', 'UNAUTHORIZED'])

/* ── Gateway URL → download target ─────────────────────────────────────── */

export type DownloadTarget =
  | { kind: 'artifact'; id: string; sessionKey?: string }
  | { kind: 'attachment'; sha256: string; name?: string; mime?: string; sessionKey?: string }

const ARTIFACT_PATH = /^\/api\/v1\/artifacts\/([^/]+)$/
const ATTACHMENT_PATH = /^\/api\/v1\/attachments\/([0-9a-f]{64})$/i

/**
 * Read a gateway file URL (relative, or absolute on the gateway origin) into
 * the file it names. Null for anything else: another host, a `data:`/`blob:`
 * URL, or a path that is already a ticket.
 */
export function parseDownloadUrl(raw: string, base: string = urlBase()): DownloadTarget | null {
  const value = String(raw || '').trim()
  if (!value) return null
  let url: URL
  let origin: string
  try {
    origin = new URL(base).origin
    url = new URL(value, base)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (url.origin !== origin) return null
  const sessionKey =
    url.searchParams.get('sessionKey') || url.searchParams.get('session_key') || undefined

  const artifact = ARTIFACT_PATH.exec(url.pathname)
  if (artifact) {
    let id = ''
    try {
      id = decodeURIComponent(artifact[1] ?? '')
    } catch {
      return null
    }
    if (!id || id === 'download') return null
    return { kind: 'artifact', id, sessionKey }
  }

  const attachment = ATTACHMENT_PATH.exec(url.pathname)
  if (attachment) {
    return {
      kind: 'attachment',
      sha256: (attachment[1] ?? '').toLowerCase(),
      name: url.searchParams.get('name') || undefined,
      mime: url.searchParams.get('mime') || undefined,
      sessionKey,
    }
  }
  return null
}

/** A ticket URL with the image-preview variant asked for. */
export function thumbnailUrl(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}variant=${THUMBNAIL_VARIANT}`
}

/* ── Resolver ──────────────────────────────────────────────────────────── */

export interface TicketOptions {
  /** The session that owns the file. Default: the URL's own, then the resolver's. */
  sessionKey?: string
  /** A still image's PNG preview (at most 1200 px) instead of the file itself. */
  thumbnail?: boolean
  /** Mint a new ticket even when one is cached (the cached one stopped loading). */
  fresh?: boolean
}

export interface TicketResolverDeps {
  call: (method: string, params: Record<string, unknown>) => Promise<unknown>
  /** The transcript's session, for a URL that does not name one. */
  getSessionKey: () => string
  now?: () => number
}

export interface TicketResolver {
  /**
   * A ticket URL for `target` (absolute off gateway, relative on it). Null when
   * no ticket can be had: the gateway has none, the socket is down, or the
   * answer was unusable. Rejects with the gateway's error for a file it
   * refused (`NOT_FOUND`, …), which the legacy URL would refuse as well.
   */
  resolve(target: DownloadTarget, options?: TicketOptions): Promise<string | null>
  /** `resolve` for a gateway file URL; null too when `raw` is not one. */
  resolveUrl(raw: string, options?: TicketOptions): Promise<string | null>
  /** False once this connection is known to have no tickets. */
  available(): boolean
  /** Read `hello-ok.features` of a fresh connection. */
  noteHello(hello: unknown): void
  /** Forget every ticket: the connection that minted them is gone. */
  reset(): void
}

function errorCode(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code
  return typeof code === 'string' ? code : ''
}

function targetKey(sessionKey: string, target: DownloadTarget): string {
  return target.kind === 'artifact'
    ? [sessionKey, 'artifact', target.id].join('\u0000')
    : // The display name and mime ride on the ticket (its Content-Disposition),
      // so two chips naming one blob differently get a ticket each.
      [sessionKey, 'attachment', target.sha256, target.name ?? '', target.mime ?? ''].join('\u0000')
}

function ticketParams(sessionKey: string, target: DownloadTarget): Record<string, unknown> {
  if (target.kind === 'artifact') {
    return { session_key: sessionKey, artifact_id: target.id, transport: 'http' }
  }
  const params: Record<string, unknown> = {
    session_key: sessionKey,
    attachment_sha256: target.sha256,
    transport: 'http',
  }
  if (target.name) params.name = target.name
  if (target.mime) params.mime = target.mime
  return params
}

interface CachedTicket {
  url: string
  expiresAt: number
}

/** The usable part of an `artifacts.download` answer, or null. */
function readTicket(response: unknown, now: number): CachedTicket | null {
  const r = (response ?? {}) as { url?: unknown; expiresAt?: unknown; ttlSeconds?: unknown }
  const path = typeof r.url === 'string' ? r.url : ''
  // Gateway-relative only: a ticket never points at another host.
  if (!path.startsWith('/') || path.startsWith('//')) return null
  const ttlMs =
    typeof r.ttlSeconds === 'number' && Number.isFinite(r.ttlSeconds)
      ? r.ttlSeconds * 1000
      : DEFAULT_TTL_MS
  const expiresAt =
    typeof r.expiresAt === 'number' && Number.isFinite(r.expiresAt) ? r.expiresAt : now + ttlMs
  return { url: apiUrl(path), expiresAt }
}

export function createTicketResolver(deps: TicketResolverDeps): TicketResolver {
  const now = deps.now ?? (() => Date.now())
  const cache = new Map<string, CachedTicket>()
  const inflight = new Map<string, Promise<string | null>>()
  // Bumped on reset, so an answer minted on a closed socket is never cached.
  let generation = 0
  // undefined = not known yet: try, and learn from the answer.
  let supported: boolean | undefined

  function mint(key: string, sessionKey: string, target: DownloadTarget): Promise<string | null> {
    const pending = inflight.get(key)
    if (pending) return pending
    const minted = generation
    const request: Promise<string | null> = Promise.resolve()
      .then(() => deps.call(TICKET_METHOD, ticketParams(sessionKey, target)))
      .then(
        (response) => {
          const ticket = readTicket(response, now())
          if (ticket && minted === generation) cache.set(key, ticket)
          return ticket?.url ?? null
        },
        (error: unknown) => {
          const code = errorCode(error)
          if (UNAVAILABLE_CODES.has(code)) {
            if (minted === generation) supported = false
            return null
          }
          // No code: the socket, not the gateway, failed. The legacy URL may
          // still work over plain HTTP.
          if (!code) return null
          throw error
        },
      )
      .finally(() => {
        if (inflight.get(key) === request) inflight.delete(key)
      })
    inflight.set(key, request)
    return request
  }

  async function resolve(
    target: DownloadTarget,
    options: TicketOptions = {},
  ): Promise<string | null> {
    if (supported === false) return null
    const sessionKey = options.sessionKey || target.sessionKey || deps.getSessionKey() || ''
    if (!sessionKey) return null
    const key = targetKey(sessionKey, target)
    let url: string | null
    const cached = options.fresh ? undefined : cache.get(key)
    if (cached && cached.expiresAt - TICKET_REFRESH_MARGIN_MS > now()) {
      url = cached.url
    } else {
      cache.delete(key)
      url = await mint(key, sessionKey, target)
    }
    return url && options.thumbnail ? thumbnailUrl(url) : url
  }

  return {
    resolve,
    resolveUrl(raw, options) {
      const target = parseDownloadUrl(raw)
      return target ? resolve(target, options) : Promise.resolve(null)
    },
    available: () => supported !== false,
    noteHello(hello) {
      const features = (hello as { features?: { methods?: unknown; capabilities?: unknown } })
        ?.features
      const methods = Array.isArray(features?.methods) ? features.methods : null
      const capabilities = Array.isArray(features?.capabilities) ? features.capabilities : []
      if (!methods) return
      supported = methods.includes(TICKET_METHOD) || capabilities.includes(TICKET_CAPABILITY)
    },
    reset() {
      generation += 1
      cache.clear()
      inflight.clear()
      supported = undefined
    },
  }
}

/**
 * Keep a resolver in step with its socket: every connection state change
 * drops the cached tickets (they die with the connection that minted them),
 * and each handshake says whether the new connection has tickets at all.
 */
export function bindTicketResolver(
  rpc: { on: (event: string, handler: (...args: unknown[]) => void) => () => void },
  resolver: TicketResolver,
): () => void {
  const offState = rpc.on('_state', () => resolver.reset())
  const offHello = rpc.on('_hello', (hello) => resolver.noteHello(hello))
  return () => {
    offState()
    offHello()
  }
}

/* ── Media hydration ───────────────────────────────────────────────────── */

/** The gateway URL an `<img>` / `<audio>` loads once it has a ticket. */
export const TICKET_SRC_ATTR = 'data-ticket-src'
/** `thumbnail` on a still image: load the PNG preview, not the full file. */
export const TICKET_VARIANT_ATTR = 'data-ticket-variant'
/** pending | ticket | legacy | failed — also marks an element as picked up. */
export const TICKET_STATE_ATTR = 'data-ticket-state'

export interface TicketHydrationDeps {
  resolveUrl: (raw: string, options?: TicketOptions) => Promise<string | null>
  /** What to load when no ticket can be had: the legacy URL. */
  fallbackUrl: (raw: string) => string
}

function setSource(el: HTMLElement, url: string): void {
  if (!(el instanceof HTMLMediaElement)) {
    el.setAttribute('src', url)
    return
  }
  // A player whose ticket ran out mid-listen picks up where it was.
  const position = el.currentTime
  el.setAttribute('src', url)
  if (position > 0) {
    el.addEventListener(
      'loadedmetadata',
      () => {
        el.currentTime = position
      },
      { once: true },
    )
  }
}

function hydrateElement(el: HTMLElement, deps: TicketHydrationDeps): void {
  const raw = el.getAttribute(TICKET_SRC_ATTR) || ''
  const thumbnail = el.getAttribute(TICKET_VARIANT_ATTR) === THUMBNAIL_VARIANT
  let retried = false

  const load = (options: TicketOptions): void => {
    el.setAttribute(TICKET_STATE_ATTR, 'pending')
    deps.resolveUrl(raw, options).then(
      (url) => {
        setSource(el, url || deps.fallbackUrl(raw))
        el.setAttribute(TICKET_STATE_ATTR, url ? 'ticket' : 'legacy')
      },
      () => el.setAttribute(TICKET_STATE_ATTR, 'failed'),
    )
  }

  // A ticket that stopped loading has expired, lost its socket, or (for a
  // thumbnail) names a file with no preview: mint a fresh one for the full
  // file, once. A successful load re-arms that, since an audio player can
  // outlive several tickets.
  el.addEventListener('error', () => {
    if (retried || el.getAttribute(TICKET_STATE_ATTR) !== 'ticket') return
    retried = true
    load({ fresh: true })
  })
  el.addEventListener(el instanceof HTMLMediaElement ? 'loadedmetadata' : 'load', () => {
    retried = false
  })
  load({ thumbnail })
}

/**
 * Give every `data-ticket-src` element under `root` that has not been picked
 * up yet its source. Idempotent, like the card mounters it runs next to.
 */
export function hydrateTicketSources(root: ParentNode, deps: TicketHydrationDeps): void {
  root
    .querySelectorAll<HTMLElement>(`[${TICKET_SRC_ATTR}]:not([${TICKET_STATE_ATTR}])`)
    .forEach((el) => hydrateElement(el, deps))
}
