import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WsRpcClient } from './ws-rpc'

/** Minimal scripted fake for the browser WebSocket API. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1
  static CONNECTING = 0
  readyState = FakeWebSocket.CONNECTING
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.readyState = 3
    this.onclose?.()
  }
  // test helpers
  serverOpen() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }
  serverSend(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

function newClient() {
  const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
  client.connect('ws://test/ws', 'tok-1')
  const ws = FakeWebSocket.instances.at(-1)!
  ws.serverOpen()
  return { client, ws }
}

function handshake(ws: FakeWebSocket) {
  ws.serverSend({ type: 'event', event: 'connect.challenge' })
  ws.serverSend({ protocol: 3, policy: { tick_interval_ms: 30000 } })
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.instances = []
})
afterEach(() => vi.useRealTimers())

describe('handshake', () => {
  it('answers connect.challenge with a protocol-3 connect request incl. auth token', () => {
    const { ws } = newClient()
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req.method).toBe('connect')
    expect(req.params.minProtocol).toBe(3)
    expect(req.params.maxProtocol).toBe(3)
    expect(req.params.client).toEqual({
      id: 'agentos-web',
      version: 'bundled',
      platform: 'web',
      mode: 'webui',
    })
    expect(req.params.auth).toEqual({ token: 'tok-1' })
  })

  it('merges extra auth (the desktop operator secret) into the handshake', () => {
    const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    client.connect('ws://test/ws', 'tok-1', { operatorSecret: 'op-secret' })
    const ws = FakeWebSocket.instances.at(-1)!
    ws.serverOpen()
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req.params.auth).toEqual({ token: 'tok-1', operatorSecret: 'op-secret' })
    // Without a token the extra auth still goes; with nothing at all, no auth key.
    const bare = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    bare.connect('ws://test/ws', null, {})
    const ws2 = FakeWebSocket.instances.at(-1)!
    ws2.serverOpen()
    ws2.serverSend({ type: 'event', event: 'connect.challenge' })
    expect(JSON.parse(ws2.sent.at(-1)!).params.auth).toBeUndefined()
  })

  it('sends a caller-supplied client block, updatable before the next handshake', () => {
    const client = new WsRpcClient({
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      clientInfo: { id: 'agentos-desktop', platform: 'darwin', mode: 'desktop' },
    })
    client.setClientInfo({ version: '2026.10.25' })
    client.connect('ws://test/ws', null)
    const ws = FakeWebSocket.instances.at(-1)!
    ws.serverOpen()
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n1', ts: 1 } })
    expect(JSON.parse(ws.sent.at(-1)!).params.client).toEqual({
      id: 'agentos-desktop',
      version: '2026.10.25',
      platform: 'darwin',
      mode: 'desktop',
    })
  })

  it('exposes hello-ok capabilities', () => {
    const { client, ws } = newClient()
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n1' } })
    ws.serverSend({
      protocol: 3,
      auth: { nonce: 'n1' },
      features: { methods: [], events: [], capabilities: ['rpc-http', 'typed-errors'] },
      policy: {},
    })
    expect(client.state).toBe('connected')
    expect(client.capabilities).toEqual(['rpc-http', 'typed-errors'])
    expect(client.hasCapability('typed-errors')).toBe(true)
    expect(client.hasCapability('nope')).toBe(false)
  })

  it('refuses a hello-ok that echoes another challenge nonce', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'mine' } })
    ws.serverSend({ protocol: 3, auth: { nonce: 'theirs' }, policy: {} })
    expect(client.state).not.toBe('connected')
    expect(errors).toHaveLength(1)
  })

  it('stops on a refused handshake and surfaces the typed error', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } })
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: {
        code: 'INVALID_REQUEST',
        message: 'Unsupported protocol version range',
        details: { code: 'PROTOCOL_MISMATCH', reload_required: true },
      },
    })
    expect(client.state).toBe('disconnected')
    expect(errors).toHaveLength(1)
    expect(errors[0]).toMatchObject({
      code: 'INVALID_REQUEST',
      details: { code: 'PROTOCOL_MISMATCH', reload_required: true },
    })
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1) // no reconnect loop
  })

  it('reconnects when the socket drops mid-handshake (not a refusal)', () => {
    const { client, ws } = newClient()
    const errors: unknown[] = []
    client.on('_handshakeError', (e) => errors.push(e))
    ws.serverSend({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } })
    ws.close()
    expect(errors).toHaveLength(0)
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('enters connected state and stores policy on HelloOk', () => {
    const { client, ws } = newClient()
    handshake(ws)
    expect(client.state).toBe('connected')
    expect(client.policy).toEqual({ tick_interval_ms: 30000 })
  })
})

describe('call correlation', () => {
  it('resolves with payload on ok res, matching by id', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call<{ n: number }>('doctor.status', { deep: true })
    const req = JSON.parse(ws.sent.at(-1)!)
    expect(req).toMatchObject({ type: 'req', method: 'doctor.status', params: { deep: true } })
    ws.serverSend({ type: 'res', id: req.id, ok: true, payload: { n: 7 } })
    await expect(p).resolves.toEqual({ n: 7 })
  })

  it('rejects with RpcError carrying code and details', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call('x.y')
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: { code: 'FORBIDDEN', message: 'no', details: { k: 1 } },
    })
    await expect(p).rejects.toMatchObject({ message: 'no', code: 'FORBIDDEN', details: { k: 1 } })
  })

  it('rejects immediately when not connected', async () => {
    const client = new WsRpcClient({ WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket })
    await expect(client.call('a.b')).rejects.toThrow('Not connected')
  })

  it('rejects all pending calls when the socket closes', async () => {
    const { client, ws } = newClient()
    handshake(ws)
    const p = client.call('a.b')
    ws.close()
    await expect(p).rejects.toThrow('Connection closed')
  })
})

describe('events', () => {
  it('fans out to named and wildcard listeners with meta', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const named = vi.fn()
    const wild = vi.fn()
    client.on('sessions.changed', named)
    client.on('*', wild)
    ws.serverSend({
      type: 'event',
      event: 'sessions.changed',
      payload: { a: 1 },
      meta: { m: 2 },
      seq: 1,
    })
    expect(named).toHaveBeenCalledWith({ a: 1 }, { m: 2 })
    expect(wild).toHaveBeenCalledWith('sessions.changed', { a: 1 }, { m: 2 })
  })

  it('detects a seq gap, emits _gap, and closes the socket', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const gap = vi.fn()
    const named = vi.fn()
    client.on('_gap', gap)
    client.on('e', named)
    ws.serverSend({ type: 'event', event: 'e', payload: {}, seq: 1 })
    ws.serverSend({ type: 'event', event: 'e', payload: {}, seq: 3 })
    expect(gap).toHaveBeenCalledWith({ expected: 2, actual: 3, event: 'e' })
    expect(named).toHaveBeenCalledTimes(1) // gapped frame not delivered
  })
})

describe('keepalive and reconnect', () => {
  it('sends a ping every 55s while open', () => {
    const { ws } = newClient()
    handshake(ws)
    vi.advanceTimersByTime(55_000)
    expect(ws.sent).toContain('{"type":"ping"}')
  })

  it('reconnects with backoff after close (800ms first retry)', () => {
    const { ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.close()
    vi.advanceTimersByTime(799)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })

  it('closes the socket when no frame arrives within the tick timeout', () => {
    const { client, ws } = newClient()
    handshake(ws) // tick_interval_ms 30000 -> timeout 75s, checked every 10s
    const gap = vi.fn()
    client.on('_gap', gap)
    // Idle exceeds the 75s timeout at the next 10s check tick (80s), so advance past it.
    vi.advanceTimersByTime(81_000)
    expect(gap).toHaveBeenCalledWith(expect.objectContaining({ reason: 'tick_timeout' }))
  })
})

describe('supervisor contract', () => {
  function refuseStartup(ws: FakeWebSocket, retryAfterMs = 1000) {
    ws.serverSend({ type: 'event', event: 'connect.challenge' })
    const req = JSON.parse(ws.sent.at(-1)!)
    ws.serverSend({
      type: 'res',
      id: req.id,
      ok: false,
      error: {
        code: 'UNAVAILABLE',
        message: 'Gateway is starting; retry shortly',
        retryable: true,
        details: { code: 'STARTUP_SIDECARS', retry_after_ms: retryAfterMs },
      },
    })
  }

  it('retries a handshake refused while the gateway starts, staying connecting', () => {
    const { client, ws } = newClient()
    const states: unknown[] = []
    client.on('_state', (s) => states.push(s))
    const count = FakeWebSocket.instances.length
    refuseStartup(ws, 1200)
    expect(client.state).toBe('connecting')
    expect(states).not.toContain('disconnected')
    vi.advanceTimersByTime(1199)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
    const next = FakeWebSocket.instances.at(-1)!
    next.serverOpen()
    handshake(next)
    expect(client.state).toBe('connected')
  })

  it('reconnects when an in-place restart said it would be back', () => {
    const { client, ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.serverSend({
      type: 'event',
      event: 'shutdown',
      payload: { reason: 'restart:safe', restart_expected_ms: 4000 },
    })
    ws.close()
    expect(client.state).toBe('disconnected')
    vi.advanceTimersByTime(3999)
    expect(FakeWebSocket.instances.length).toBe(count)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })

  it('keeps the normal backoff after a terminal stop', () => {
    const { ws } = newClient()
    handshake(ws)
    const count = FakeWebSocket.instances.length
    ws.serverSend({
      type: 'event',
      event: 'shutdown',
      payload: { reason: 'shutdown', restart_expected_ms: null },
    })
    ws.close()
    vi.advanceTimersByTime(800)
    expect(FakeWebSocket.instances.length).toBe(count + 1)
  })
})
