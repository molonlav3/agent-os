import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  bindRuntimeSignals,
  classifyHandshake,
  doctorErrorCount,
  normalizeDoctor,
  normalizeReady,
  useRuntimeSignals,
  type SignalsRpc,
} from './runtime-signals'
import type { RpcState } from './ws-rpc'

type Handler = (...args: unknown[]) => void

/** A scripted stand-in for WsRpcClient: emit() plays a frame, call() answers. */
class FakeRpc implements SignalsRpc {
  state: RpcState = 'disconnected'
  handlers = new Map<string, Set<Handler>>()
  calls: Array<[string, Record<string, unknown> | undefined]> = []
  answer: (method: string) => Promise<unknown> = () => Promise.resolve({})
  reconnect = vi.fn()

  on(event: string, handler: Handler): () => void {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set())
    this.handlers.get(event)!.add(handler)
    return () => this.handlers.get(event)?.delete(handler)
  }

  call<T>(method: string, params?: Record<string, unknown>): Promise<T> {
    this.calls.push([method, params])
    return this.answer(method) as Promise<T>
  }

  emit(event: string, ...args: unknown[]): void {
    this.handlers.get(event)?.forEach((h) => h(...args))
  }

  /** What ws-rpc emits for one server event frame. */
  event(name: string, payload: unknown = {}): void {
    this.emit(name, payload, {})
    this.emit('*', name, payload, {})
  }

  connect(hello: Record<string, unknown> = { protocol: 3 }): void {
    this.state = 'connected'
    this.emit('_state', 'connected')
    this.emit('_hello', hello)
  }

  doctorCalls(): number {
    return this.calls.filter(([m]) => m === 'doctor.status').length
  }
}

const flush = () => new Promise((r) => setTimeout(r, 0))

let clock = 1_000_000
const now = () => clock

beforeEach(() => {
  clock = 1_000_000
  useRuntimeSignals.getState().reset()
})
afterEach(() => {
  useRuntimeSignals.getState().reset()
})

describe('bindRuntimeSignals', () => {
  it('records the hello: protocol, version, methods, capabilities, tick policy', async () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect({
      protocol: 3,
      server: { version: '2026.10.25' },
      features: { methods: ['status', 'doctor.status'], capabilities: ['cron'] },
      snapshot: { state_version: { presence: 1, health: 2 } },
      policy: { tick_interval_ms: 20_000 },
    })
    const s = useRuntimeSignals.getState()
    expect(s.everConnected).toBe(true)
    expect(s.protocol).toBe(3)
    expect(s.serverVersion).toBe('2026.10.25')
    expect(s.methods).toEqual(['status', 'doctor.status'])
    expect(s.capabilities).toEqual(['cron'])
    expect(s.stateVersion).toEqual({ presence: 1, health: 2 })
    expect(s.tickIntervalMs).toBe(20_000)
    expect(s.lastFrameAt).toBe(clock)
    off()
  })

  it('tolerates a hello without capabilities', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect({ protocol: 3, features: { methods: [] } })
    expect(useRuntimeSignals.getState().capabilities).toBeNull()
    off()
  })

  it('reads doctor.status (shallow) on connect and stores the summary', async () => {
    const rpc = new FakeRpc()
    rpc.answer = (m) =>
      m === 'doctor.status'
        ? Promise.resolve({ status: 'degraded', ready: true, counts: { error: 1 }, findings: [] })
        : Promise.resolve({})
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect()
    await flush()
    expect(rpc.calls).toContainEqual(['doctor.status', { agentId: 'main', deep: false }])
    expect(useRuntimeSignals.getState().doctor).toMatchObject({
      status: 'degraded',
      counts: { error: 1 },
    })
    off()
  })

  it('refreshes on the tick revision, never on its own timer', async () => {
    vi.useFakeTimers()
    try {
      const rpc = new FakeRpc()
      const off = bindRuntimeSignals(rpc, { now, fetchReady: null, doctorEveryTicks: 2 })
      rpc.connect()
      await vi.advanceTimersByTimeAsync(0)
      expect(rpc.doctorCalls()).toBe(1)
      // Time alone does nothing.
      await vi.advanceTimersByTimeAsync(10 * 60_000)
      expect(rpc.doctorCalls()).toBe(1)
      rpc.event('tick')
      await vi.advanceTimersByTimeAsync(0)
      expect(useRuntimeSignals.getState().tickRevision).toBe(1)
      expect(rpc.doctorCalls()).toBe(1)
      rpc.event('tick')
      await vi.advanceTimersByTimeAsync(0)
      expect(rpc.doctorCalls()).toBe(2)
      off()
    } finally {
      vi.useRealTimers()
    }
  })

  it('refreshes when the health revision moves or a health event arrives', async () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect({ protocol: 3, snapshot: { state_version: { presence: 0, health: 1 } } })
    await flush()
    expect(rpc.doctorCalls()).toBe(1)
    rpc.emit('_state_version', { presence: 3, health: 1 })
    await flush()
    expect(rpc.doctorCalls()).toBe(1)
    rpc.emit('_state_version', { health: 2 })
    await flush()
    expect(rpc.doctorCalls()).toBe(2)
    expect(useRuntimeSignals.getState().stateVersion).toEqual({ presence: 3, health: 2 })
    rpc.event('health')
    await flush()
    expect(rpc.doctorCalls()).toBe(3)
    off()
  })

  it('ages the last frame from event frames, throttled to one write a second', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect()
    clock += 400
    rpc.event('session.event.text_delta')
    expect(useRuntimeSignals.getState().lastFrameAt).toBe(1_000_000)
    clock += 700
    rpc.event('session.event.text_delta')
    expect(useRuntimeSignals.getState().lastFrameAt).toBe(1_001_100)
    off()
  })

  it('counts failed attempts and resets them on a connection', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.emit('_state', 'connecting')
    rpc.emit('_state', 'disconnected')
    rpc.emit('_state', 'connecting')
    rpc.emit('_state', 'disconnected')
    expect(useRuntimeSignals.getState().failedAttempts).toBe(2)
    rpc.state = 'connected'
    rpc.emit('_state', 'connecting')
    rpc.emit('_state', 'connected')
    expect(useRuntimeSignals.getState().failedAttempts).toBe(0)
    // A drop from a live connection is not a failed attempt.
    rpc.emit('_state', 'disconnected')
    expect(useRuntimeSignals.getState().failedAttempts).toBe(0)
    off()
  })

  it('names a refused handshake and clears it on the next hello', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.emit('_handshake_error', { code: 'UNAUTHORIZED', message: 'Authentication failed' })
    expect(useRuntimeSignals.getState().handshake).toMatchObject({
      kind: 'auth_failed',
      code: 'UNAUTHORIZED',
    })
    rpc.connect()
    expect(useRuntimeSignals.getState().handshake).toBeNull()
    off()
  })

  it('records a doctor failure without inventing a report', async () => {
    const rpc = new FakeRpc()
    rpc.answer = (m) =>
      m === 'doctor.status' ? Promise.reject(new Error('METHOD_NOT_FOUND')) : Promise.resolve({})
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect()
    await flush()
    expect(useRuntimeSignals.getState().doctor).toBeNull()
    expect(useRuntimeSignals.getState().doctorError).toBe('METHOD_NOT_FOUND')
    off()
  })

  it('reads /ready in both the old and the new shape', async () => {
    const rpc = new FakeRpc()
    const fetchReady = vi
      .fn<() => Promise<unknown>>()
      .mockResolvedValueOnce({ ready: true, status: 'ready' })
      .mockResolvedValueOnce({ ready: false, state: 'safe_mode', failing: ['memory'] })
    const off = bindRuntimeSignals(rpc, { now, fetchReady })
    rpc.connect()
    await flush()
    expect(useRuntimeSignals.getState().ready).toMatchObject({ ready: true, state: 'ready' })
    useRuntimeSignals.getState().refresh()
    await flush()
    expect(useRuntimeSignals.getState().ready).toMatchObject({
      ready: false,
      state: 'safe_mode',
      failing: ['memory'],
    })
    off()
  })

  it('forces a new socket from Retry only when the connected one has gone silent', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    rpc.connect({ protocol: 3, policy: { tick_interval_ms: 30_000 } })
    clock += 10_000
    useRuntimeSignals.getState().retry()
    expect(rpc.reconnect).toHaveBeenLastCalledWith({ force: false })
    // 2.5 tick intervals without a frame: stale.
    clock += 70_000
    useRuntimeSignals.getState().retry()
    expect(rpc.reconnect).toHaveBeenLastCalledWith({ force: true })
    // Not connected: a plain reconnect.
    rpc.state = 'disconnected'
    useRuntimeSignals.getState().retry()
    expect(rpc.reconnect).toHaveBeenLastCalledWith({ force: false })
    off()
  })

  it('wires retry to reconnect and unwires on dispose', () => {
    const rpc = new FakeRpc()
    const off = bindRuntimeSignals(rpc, { now, fetchReady: null })
    useRuntimeSignals.getState().retry()
    expect(rpc.reconnect).toHaveBeenCalledTimes(1)
    off()
    useRuntimeSignals.getState().retry()
    expect(rpc.reconnect).toHaveBeenCalledTimes(1)
    expect(rpc.handlers.get('*')?.size ?? 0).toBe(0)
  })
})

describe('pure helpers', () => {
  it('classifies refusals', () => {
    expect(classifyHandshake({ code: 'UNAUTHORIZED', message: '' })).toBe('auth_failed')
    expect(
      classifyHandshake({ code: 'INVALID_REQUEST', message: 'Unsupported protocol version range' }),
    ).toBe('protocol_mismatch')
    expect(classifyHandshake({ code: 'INVALID_REQUEST', message: 'nope' })).toBe('refused')
  })

  it('normalises doctor and ready payloads', () => {
    expect(normalizeDoctor(null, 5)).toEqual({
      status: '',
      ready: false,
      counts: {},
      findings: [],
      fetchedAt: 5,
    })
    expect(normalizeReady({ ready: false }, 5)).toEqual({
      ready: false,
      state: 'starting',
      failing: [],
      fetchedAt: 5,
    })
    expect(normalizeReady('x', 5)).toBeNull()
  })

  it('counts error findings from counts, else from the findings', () => {
    expect(doctorErrorCount(null)).toBe(0)
    expect(doctorErrorCount(normalizeDoctor({ counts: { error: 2 } }, 0))).toBe(2)
    expect(
      doctorErrorCount(
        normalizeDoctor({ findings: [{ severity: 'error' }, { severity: 'warn' }] }, 0),
      ),
    ).toBe(1)
  })
})
