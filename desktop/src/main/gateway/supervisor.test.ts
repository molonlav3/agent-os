// @vitest-environment node
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'
import type { GatewaySettings } from '@shared/settings'
import { GatewaySupervisor, STOP_DRAIN_MS, STOP_FORCE_MS } from './supervisor'

const external: GatewaySettings = {
  mode: 'external',
  host: '127.0.0.1',
  port: 18791,
  token: null,
  cliPath: null,
}

describe('GatewaySupervisor (external mode)', () => {
  it('reports running once the endpoint answers /health', async () => {
    const probe = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const sup = new GatewaySupervisor(() => external, { probe })
    const seen: string[] = []
    sup.subscribe((s) => seen.push(s.state))
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', url: 'http://127.0.0.1:18791', pid: null })
    expect(seen).toEqual(['starting', 'running'])
    expect(probe).toHaveBeenCalledWith('http://127.0.0.1:18791')
  })

  it('reports an error when nothing answers', async () => {
    vi.useFakeTimers()
    const sup = new GatewaySupervisor(() => external, { probe: async () => false })
    const pending = sup.start()
    await vi.advanceTimersByTimeAsync(6_000)
    const status = await pending
    vi.useRealTimers()
    expect(status.state).toBe('error')
    expect(status.error).toContain('No gateway answering')
  })

  it('stop() never spawns and resets to stopped', async () => {
    const sup = new GatewaySupervisor(() => external, { probe: async () => true })
    await sup.start()
    expect((await sup.stop()).state).toBe('stopped')
  })
})

describe('GatewaySupervisor (managed mode)', () => {
  it('adopts a gateway that is already up instead of spawning', async () => {
    const managed: GatewaySettings = { ...external, mode: 'managed' }
    const sup = new GatewaySupervisor(() => managed, { probe: async () => true })
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', pid: null })
  })

  it('errors out when the CLI cannot be found', async () => {
    const managed: GatewaySettings = { ...external, mode: 'managed' }
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => false,
      locate: () => null,
    })
    const status = await sup.start()
    expect(status.state).toBe('error')
    expect(status.error).toContain('agentos CLI not found')
  })
})

describe('GatewaySupervisor.adopt', () => {
  const managed: GatewaySettings = { ...external, mode: 'managed' }

  it('reports a listening gateway without spawning', async () => {
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => true,
      locate: () => null,
    })
    const status = await sup.adopt()
    expect(status).toMatchObject({ state: 'running', pid: null, url: 'http://127.0.0.1:18791' })
    expect(sup.current().state).toBe('running')
  })

  it('returns null and stays stopped when nothing answers', async () => {
    const sup = new GatewaySupervisor(() => managed, {
      probe: async () => false,
      locate: () => null,
    })
    expect(await sup.adopt()).toBeNull()
    expect(sup.current().state).toBe('stopped')
  })
})

/** A spawn stand-in that records each call and hands back a quiet child. */
function fakeSpawn() {
  const spawnCalls: { command: string; args: string[]; env: NodeJS.ProcessEnv }[] = []
  const spawn = vi.fn((command: string, args: string[], options: { env: NodeJS.ProcessEnv }) => {
    spawnCalls.push({ command, args, env: options.env })
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: () => boolean
    }
    child.pid = 4242
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = () => true
    return child
  })
  return { spawn: spawn as unknown as typeof import('node:child_process').spawn, spawnCalls }
}

describe('GatewaySupervisor (spawn arguments)', () => {
  it('binds host and port on the command line and passes the token through AuthConfig env', async () => {
    const managed: GatewaySettings = {
      mode: 'managed',
      host: '127.0.0.1',
      port: 18999,
      token: 'secret',
      cliPath: '/opt/agentos',
    }
    const { spawn, spawnCalls } = fakeSpawn()
    let probes = 0
    const secretDir = mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-'))
    let seenSecret: string | null = null
    let seenMode: number | null = null
    const sup = new GatewaySupervisor(() => managed, {
      // Not running before the spawn; healthy right after it. The probe is
      // where the "gateway" reads its secret, as the real one does at boot.
      probe: async (url) => {
        const file = spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET_FILE
        if (file && seenSecret === null) {
          seenSecret = readFileSync(file, 'utf8').trim()
          seenMode = statSync(file).mode
        }
        return probes++ > 0 && url.length > 0
      },
      locate: (override) => override,
      secretDir,
      spawn,
    })
    const status = await sup.start()
    expect(status).toMatchObject({ state: 'running', pid: 4242, url: 'http://127.0.0.1:18999' })
    expect(spawnCalls[0]?.command).toBe('/opt/agentos')
    expect(spawnCalls[0]?.args).toEqual([
      'gateway',
      'run',
      '--bind',
      '127.0.0.1',
      '--port',
      '18999',
    ])
    expect(spawnCalls[0]?.env.AGENTOS_AUTH_TOKEN).toBe('secret')
    expect(spawnCalls[0]?.env.AGENTOS_AUTH_MODE).toBe('token')
    // The operator secret travelled by file, 0600, and is what the renderer
    // will present; the file is gone once the gateway is up.
    const secretFile = spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET_FILE
    expect(secretFile).toBeTruthy()
    expect(secretFile).toContain(secretDir)
    expect(seenSecret).toMatch(/^[0-9a-f]{64}$/)
    expect(seenMode! & 0o777).toBe(0o600)
    expect(sup.operatorSecret()).toBe(seenSecret)
    expect(existsSync(secretFile!)).toBe(false)
    expect(spawnCalls[0]?.env.AGENTOS_OPERATOR_SECRET).toBeUndefined()
    // The old double-underscore names never reached the gateway's settings.
    expect(spawnCalls[0]?.env.AGENTOS_GATEWAY__PORT).toBeUndefined()
    expect(spawnCalls[0]?.env.AGENTOS_AUTH__TOKEN).toBeUndefined()
  })

  it('spawns the gateway with the login-shell dirs on PATH, not the bare GUI PATH', async () => {
    // Finder/Dock launches get PATH=/usr/bin:/bin:/usr/sbin:/sbin; the gateway
    // resolves ACP harness CLIs (codex, npx) with which() against this PATH.
    vi.stubEnv('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
    const { spawn, spawnCalls } = fakeSpawn()
    let probes = 0
    const sup = new GatewaySupervisor(
      () => ({ ...external, mode: 'managed', cliPath: '/opt/agentos' }),
      {
        probe: async () => probes++ > 0,
        locate: (override) => override,
        secretDir: mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-')),
        spawn,
      },
    )
    try {
      await sup.start()
    } finally {
      vi.unstubAllEnvs()
    }
    const entries = (spawnCalls[0]?.env.PATH ?? '').split(path.delimiter)
    expect(entries.slice(0, 4)).toEqual(['/usr/bin', '/bin', '/usr/sbin', '/sbin'])
    expect(entries).toContain('/opt/homebrew/bin')
    expect(entries).toContain('/usr/local/bin')
    expect(entries).toContain(path.join(os.homedir(), '.local', 'bin'))
  })

  it('has no operator secret to offer for an adopted or external gateway', async () => {
    const sup = new GatewaySupervisor(() => external, { probe: async () => true })
    await sup.start()
    expect(sup.operatorSecret()).toBeNull()
  })
})

describe('GatewaySupervisor.stop escalation', () => {
  async function startedWithChild(onKill: (signal: string) => void) {
    const child = new EventEmitter() as EventEmitter & {
      pid: number
      stdout: PassThrough
      stderr: PassThrough
      kill: (signal: string) => boolean
    }
    child.pid = 4343
    child.stdout = new PassThrough()
    child.stderr = new PassThrough()
    child.kill = (signal: string) => {
      onKill(signal)
      return true
    }
    const spawn = vi.fn(() => child) as unknown as typeof import('node:child_process').spawn
    let probes = 0
    const sup = new GatewaySupervisor(
      () => ({ ...external, mode: 'managed', cliPath: '/opt/agentos' }),
      {
        probe: async () => probes++ > 0,
        locate: (override) => override,
        secretDir: mkdtempSync(path.join(os.tmpdir(), 'agentos-sup-')),
        spawn,
      },
    )
    await sup.start()
    return { sup, child }
  }

  it('lets the gateway drain, then forces with a second SIGTERM, then SIGKILL', async () => {
    const signals: string[] = []
    const { sup } = await startedWithChild((signal) => signals.push(signal))
    vi.useFakeTimers()
    try {
      const stopping = sup.stop()
      expect(signals).toEqual(['SIGTERM'])
      await vi.advanceTimersByTimeAsync(STOP_DRAIN_MS)
      expect(signals).toEqual(['SIGTERM', 'SIGTERM'])
      await vi.advanceTimersByTimeAsync(STOP_FORCE_MS)
      expect(signals).toEqual(['SIGTERM', 'SIGTERM', 'SIGKILL'])
      await stopping
    } finally {
      vi.useRealTimers()
    }
  })

  it('sends nothing more once the gateway exits on the first SIGTERM', async () => {
    const signals: string[] = []
    let exit: (() => void) | null = null
    const { sup, child } = await startedWithChild((signal) => {
      signals.push(signal)
      exit?.()
    })
    exit = () => queueMicrotask(() => child.emit('exit', 0, null))
    vi.useFakeTimers()
    try {
      const stopping = sup.stop()
      await vi.advanceTimersByTimeAsync(STOP_DRAIN_MS + STOP_FORCE_MS)
      await stopping
      expect(signals).toEqual(['SIGTERM'])
    } finally {
      vi.useRealTimers()
    }
  })
})
