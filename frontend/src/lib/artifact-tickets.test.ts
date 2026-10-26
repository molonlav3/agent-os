import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RpcError } from './ws-rpc'
import {
  bindTicketResolver,
  createTicketResolver,
  hydrateTicketSources,
  parseDownloadUrl,
  thumbnailUrl,
  TICKET_REFRESH_MARGIN_MS,
  TICKET_STATE_ATTR,
} from './artifact-tickets'

const SHA = 'ab'.repeat(32)
const SESSION = 'agent:main:webchat:test'
const ORIGIN = window.location.origin

function rpcError(code: string): RpcError {
  const error = new RpcError(code)
  error.code = code
  return error
}

function ticket(url: string, expiresAt = Date.now() + 300_000) {
  return { url, kind: 'artifact', name: 'x', mime: '', size: 1, expiresAt, ttlSeconds: 300 }
}

/** Let queued promise callbacks run. */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
}

describe('parseDownloadUrl', () => {
  it('reads an artifact URL, relative or on the gateway origin', () => {
    expect(parseDownloadUrl('/api/v1/artifacts/art%201?sessionKey=s1&token=t')).toEqual({
      kind: 'artifact',
      id: 'art 1',
      sessionKey: 's1',
    })
    expect(parseDownloadUrl(`${ORIGIN}/api/v1/artifacts/a-2`)).toEqual({
      kind: 'artifact',
      id: 'a-2',
    })
  })

  it('reads an attachment URL with its display name and mime', () => {
    expect(
      parseDownloadUrl(
        `/api/v1/attachments/${SHA.toUpperCase()}?session_key=s2&name=r%C3%A9sum%C3%A9.pdf&mime=application%2Fpdf`,
      ),
    ).toEqual({
      kind: 'attachment',
      sha256: SHA,
      name: 'résumé.pdf',
      mime: 'application/pdf',
      sessionKey: 's2',
    })
  })

  it('is null for anything that is not a gateway file URL', () => {
    expect(parseDownloadUrl('')).toBeNull()
    expect(parseDownloadUrl('data:image/png;base64,AAAA')).toBeNull()
    expect(parseDownloadUrl('blob:http://localhost:3000/1234')).toBeNull()
    expect(parseDownloadUrl('https://files.example/api/v1/artifacts/a')).toBeNull()
    // Already a ticket.
    expect(parseDownloadUrl('/api/v1/artifacts/download/conn/tkt')).toBeNull()
    expect(parseDownloadUrl('/api/v1/artifacts/download')).toBeNull()
    expect(parseDownloadUrl('/api/v1/attachments/not-a-sha')).toBeNull()
    expect(parseDownloadUrl('/api/v1/artifacts/%E0%A4%A')).toBeNull()
  })

  it('resolves against the gateway origin off gateway', () => {
    const base = 'http://127.0.0.1:18791'
    expect(parseDownloadUrl('/api/v1/artifacts/a', base)).toEqual({ kind: 'artifact', id: 'a' })
    expect(parseDownloadUrl(`${base}/api/v1/artifacts/a`, base)).toEqual({
      kind: 'artifact',
      id: 'a',
    })
    expect(parseDownloadUrl('http://127.0.0.1:9999/api/v1/artifacts/a', base)).toBeNull()
  })
})

describe('thumbnailUrl', () => {
  it('appends the preview variant', () => {
    expect(thumbnailUrl('/api/v1/artifacts/download/c/t')).toBe(
      '/api/v1/artifacts/download/c/t?variant=thumbnail',
    )
    expect(thumbnailUrl('/x?a=1')).toBe('/x?a=1&variant=thumbnail')
  })
})

describe('createTicketResolver', () => {
  afterEach(() => {
    delete window.__AGENTOS_ENV__
  })

  it('mints a ticket with the contract params', async () => {
    const call = vi.fn().mockResolvedValue(ticket('/api/v1/artifacts/download/c1/t1'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })

    await expect(resolver.resolveUrl('/api/v1/artifacts/art-1')).resolves.toBe(
      '/api/v1/artifacts/download/c1/t1',
    )
    expect(call).toHaveBeenCalledWith('artifacts.download', {
      session_key: SESSION,
      artifact_id: 'art-1',
      transport: 'http',
    })

    await resolver.resolveUrl(
      `/api/v1/attachments/${SHA}?sessionKey=other&name=a.txt&mime=text%2Fplain`,
    )
    expect(call).toHaveBeenLastCalledWith('artifacts.download', {
      session_key: 'other',
      attachment_sha256: SHA,
      name: 'a.txt',
      mime: 'text/plain',
      transport: 'http',
    })
  })

  it('prefers an explicit session over the URL and the transcript', async () => {
    const call = vi.fn().mockResolvedValue(ticket('/t'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    await resolver.resolveUrl('/api/v1/artifacts/a?sessionKey=url-session', { sessionKey: 'own' })
    expect(call.mock.calls[0]?.[1]).toMatchObject({ session_key: 'own' })
  })

  it('makes the ticket absolute off gateway', async () => {
    window.__AGENTOS_ENV__ = { apiOrigin: 'http://127.0.0.1:18791' }
    const call = vi.fn().mockResolvedValue(ticket('/api/v1/artifacts/download/c/t'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    await expect(
      resolver.resolveUrl('http://127.0.0.1:18791/api/v1/artifacts/a', { thumbnail: true }),
    ).resolves.toBe('http://127.0.0.1:18791/api/v1/artifacts/download/c/t?variant=thumbnail')
  })

  it('caches a ticket until shortly before it expires', async () => {
    let now = 1_000_000
    const call = vi
      .fn()
      .mockResolvedValueOnce(ticket('/t1', now + 300_000))
      .mockResolvedValueOnce(ticket('/t2', now + 600_000))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION, now: () => now })

    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t1')
    // The thumbnail is a variant of the same ticket, not a second one.
    expect(await resolver.resolveUrl('/api/v1/artifacts/a', { thumbnail: true })).toBe(
      '/t1?variant=thumbnail',
    )
    now += 300_000 - TICKET_REFRESH_MARGIN_MS - 1
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t1')
    expect(call).toHaveBeenCalledTimes(1)

    now += 2
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t2')
    expect(call).toHaveBeenCalledTimes(2)
  })

  it('keys the cache by session, file and attachment name', async () => {
    const call = vi.fn((_method: string, params: Record<string, unknown>) =>
      Promise.resolve(ticket(`/t/${String(params.session_key)}/${String(params.name ?? '')}`)),
    )
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    await resolver.resolveUrl('/api/v1/artifacts/a?sessionKey=s1')
    await resolver.resolveUrl('/api/v1/artifacts/a?sessionKey=s2')
    await resolver.resolveUrl(`/api/v1/attachments/${SHA}?name=one.txt`)
    expect(await resolver.resolveUrl(`/api/v1/attachments/${SHA}?name=two.txt`)).toBe(
      `/t/${SESSION}/two.txt`,
    )
    expect(call).toHaveBeenCalledTimes(4)
  })

  it('shares one request between concurrent asks for the same file', async () => {
    let answer: (value: unknown) => void = () => {}
    const call = vi.fn(() => new Promise((resolve) => (answer = resolve)))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })

    const first = resolver.resolveUrl('/api/v1/artifacts/a')
    const second = resolver.resolveUrl('/api/v1/artifacts/a', { thumbnail: true })
    await flush()
    answer(ticket('/t'))
    await expect(first).resolves.toBe('/t')
    await expect(second).resolves.toBe('/t?variant=thumbnail')
    expect(call).toHaveBeenCalledTimes(1)
  })

  it('mints a fresh ticket on request', async () => {
    const call = vi.fn().mockResolvedValueOnce(ticket('/t1')).mockResolvedValueOnce(ticket('/t2'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    await resolver.resolveUrl('/api/v1/artifacts/a')
    expect(await resolver.resolveUrl('/api/v1/artifacts/a', { fresh: true })).toBe('/t2')
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t2')
  })

  it('drops every ticket on reset, including one still in flight', async () => {
    let answer: (value: unknown) => void = () => {}
    const call = vi
      .fn()
      .mockResolvedValueOnce(ticket('/t1'))
      .mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)))
      .mockResolvedValueOnce(ticket('/t3'))
      .mockResolvedValueOnce(ticket('/t4'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })

    await resolver.resolveUrl('/api/v1/artifacts/a')
    resolver.reset()
    const stale = resolver.resolveUrl('/api/v1/artifacts/b')
    await flush()
    resolver.reset()
    answer(ticket('/t2-dead'))
    await stale
    // Neither the pre-reset ticket nor the one minted on the closed socket.
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t3')
    expect(await resolver.resolveUrl('/api/v1/artifacts/b')).toBe('/t4')
  })

  it('falls back for good on a gateway without the method, until the next connection', async () => {
    const call = vi
      .fn()
      .mockRejectedValueOnce(rpcError('METHOD_NOT_FOUND'))
      .mockResolvedValue(ticket('/t'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })

    expect(resolver.available()).toBe(true)
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(resolver.available()).toBe(false)
    expect(await resolver.resolveUrl('/api/v1/artifacts/b')).toBeNull()
    expect(call).toHaveBeenCalledTimes(1)

    resolver.reset()
    expect(resolver.available()).toBe(true)
    expect(await resolver.resolveUrl('/api/v1/artifacts/b')).toBe('/t')
  })

  it('treats an unauthorized surface like a gateway without tickets', async () => {
    const call = vi.fn().mockRejectedValue(rpcError('UNAUTHORIZED'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(resolver.available()).toBe(false)
  })

  it('answers null for a socket failure or an unusable answer, without giving up', async () => {
    const call = vi
      .fn()
      .mockRejectedValueOnce(new Error('Not connected'))
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ url: '//evil.example/t' })
      .mockResolvedValueOnce(ticket('/t'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(resolver.available()).toBe(true)
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBe('/t')
  })

  it('rejects with the gateway error for a file it refused', async () => {
    const call = vi.fn().mockRejectedValue(rpcError('NOT_FOUND'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })
    await expect(resolver.resolveUrl('/api/v1/artifacts/a')).rejects.toMatchObject({
      code: 'NOT_FOUND',
    })
    expect(resolver.available()).toBe(true)
  })

  it('answers null without asking when there is no session or no gateway URL', async () => {
    const call = vi.fn()
    const resolver = createTicketResolver({ call, getSessionKey: () => '' })
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(await resolver.resolveUrl('https://files.example/x.png')).toBeNull()
    expect(call).not.toHaveBeenCalled()
  })

  it('learns from the handshake whether the gateway has tickets', async () => {
    const call = vi.fn().mockResolvedValue(ticket('/t'))
    const resolver = createTicketResolver({ call, getSessionKey: () => SESSION })

    resolver.noteHello({ features: { methods: ['chat.send'], events: [] } })
    expect(resolver.available()).toBe(false)
    expect(await resolver.resolveUrl('/api/v1/artifacts/a')).toBeNull()
    expect(call).not.toHaveBeenCalled()

    resolver.noteHello({ features: { methods: ['artifacts.download'], capabilities: [] } })
    expect(resolver.available()).toBe(true)
    resolver.noteHello({ features: { methods: [], capabilities: ['artifact-tickets'] } })
    expect(resolver.available()).toBe(true)
    // A handshake without a feature list says nothing.
    resolver.noteHello({ protocol: 3 })
    expect(resolver.available()).toBe(true)
  })
})

describe('bindTicketResolver', () => {
  it('resets on every state change and reads each handshake', () => {
    const handlers = new Map<string, (...args: unknown[]) => void>()
    const off = vi.fn()
    const rpc = {
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        handlers.set(event, handler)
        return off
      }),
    }
    const resolver = {
      resolve: vi.fn(),
      resolveUrl: vi.fn(),
      available: vi.fn(),
      noteHello: vi.fn(),
      reset: vi.fn(),
    }
    const unbind = bindTicketResolver(rpc, resolver)
    handlers.get('_state')?.('disconnected')
    handlers.get('_hello')?.({ features: {} })
    expect(resolver.reset).toHaveBeenCalledTimes(1)
    expect(resolver.noteHello).toHaveBeenCalledWith({ features: {} })
    unbind()
    expect(off).toHaveBeenCalledTimes(2)
  })
})

describe('hydrateTicketSources', () => {
  let root: HTMLElement

  beforeEach(() => {
    root = document.createElement('div')
    document.body.appendChild(root)
  })
  afterEach(() => {
    root.remove()
  })

  it('fills each ticketed source once, with the thumbnail for a still image', async () => {
    root.innerHTML =
      '<img data-ticket-src="/api/v1/artifacts/i?sessionKey=s" data-ticket-variant="thumbnail">' +
      '<audio data-ticket-src="/api/v1/artifacts/a?sessionKey=s"></audio>'
    const resolveUrl = vi.fn((raw: string, options?: { thumbnail?: boolean }) =>
      Promise.resolve(`${raw.includes('/i') ? '/ti' : '/ta'}${options?.thumbnail ? '?thumb' : ''}`),
    )
    hydrateTicketSources(root, { resolveUrl, fallbackUrl: () => '/legacy' })
    hydrateTicketSources(root, { resolveUrl, fallbackUrl: () => '/legacy' })
    await flush()

    expect(resolveUrl).toHaveBeenCalledTimes(2)
    expect(root.querySelector('img')).toHaveAttribute('src', '/ti?thumb')
    expect(root.querySelector('audio')).toHaveAttribute('src', '/ta')
    expect(root.querySelector('img')).toHaveAttribute(TICKET_STATE_ATTR, 'ticket')
  })

  it('loads the legacy URL when no ticket can be had', async () => {
    root.innerHTML = '<img data-ticket-src="/api/v1/artifacts/i?sessionKey=s">'
    hydrateTicketSources(root, {
      resolveUrl: () => Promise.resolve(null),
      fallbackUrl: (raw) => `${raw}&token=tok`,
    })
    await flush()
    expect(root.querySelector('img')).toHaveAttribute(
      'src',
      '/api/v1/artifacts/i?sessionKey=s&token=tok',
    )
    expect(root.querySelector('img')).toHaveAttribute(TICKET_STATE_ATTR, 'legacy')
  })

  it('marks a refused file and leaves it without a source', async () => {
    root.innerHTML = '<img data-ticket-src="/api/v1/artifacts/gone">'
    hydrateTicketSources(root, {
      resolveUrl: () => Promise.reject(rpcError('NOT_FOUND')),
      fallbackUrl: () => '/legacy',
    })
    await flush()
    expect(root.querySelector('img')).not.toHaveAttribute('src')
    expect(root.querySelector('img')).toHaveAttribute(TICKET_STATE_ATTR, 'failed')
  })

  it('retries a ticket that stopped loading once, fresh and full size', async () => {
    root.innerHTML = '<img data-ticket-src="/api/v1/artifacts/i" data-ticket-variant="thumbnail">'
    const resolveUrl = vi
      .fn()
      .mockResolvedValueOnce('/t1?variant=thumbnail')
      .mockResolvedValueOnce('/t2')
      .mockResolvedValueOnce('/t3')
    hydrateTicketSources(root, { resolveUrl, fallbackUrl: () => '/legacy' })
    await flush()
    const img = root.querySelector('img') as HTMLImageElement

    img.dispatchEvent(new Event('error'))
    await flush()
    expect(resolveUrl).toHaveBeenLastCalledWith('/api/v1/artifacts/i', { fresh: true })
    expect(img).toHaveAttribute('src', '/t2')

    // A second failure in a row is final...
    img.dispatchEvent(new Event('error'))
    await flush()
    expect(resolveUrl).toHaveBeenCalledTimes(2)

    // ...until the element loads again.
    img.dispatchEvent(new Event('load'))
    img.dispatchEvent(new Event('error'))
    await flush()
    expect(img).toHaveAttribute('src', '/t3')
  })

  it('does not retry a legacy source', async () => {
    root.innerHTML = '<audio data-ticket-src="/api/v1/artifacts/a"></audio>'
    const resolveUrl = vi.fn().mockResolvedValue(null)
    hydrateTicketSources(root, { resolveUrl, fallbackUrl: () => '/legacy' })
    await flush()
    root.querySelector('audio')?.dispatchEvent(new Event('error'))
    await flush()
    expect(resolveUrl).toHaveBeenCalledTimes(1)
  })
})
