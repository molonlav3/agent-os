// @vitest-environment node
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { REQUIRED_METHODS } from '@/lib/required-methods'
import { DESKTOP_REQUIRED_METHODS } from './required-methods'

/**
 * Keeps DESKTOP_REQUIRED_METHODS honest for every literal call site in the
 * renderer: a new `rpc.call('x.y')` fails here until it is listed (in the
 * console's list when the web console calls it too, else here). The Python
 * contract test then checks both lists against the gateway's registry.
 */

const srcDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function sourceFiles(directory: string): string[] {
  const found: string[] = []
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name)
    if (entry.isDirectory()) found.push(...sourceFiles(path))
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) found.push(path)
  }
  return found
}

const CALL = /\bcall(?:<(?:[^<>]|<[^<>]*>)*>)?\(\s*(['"])([A-Za-z][A-Za-z0-9_.]*)\1/g

describe('DESKTOP_REQUIRED_METHODS', () => {
  it('lists every RPC method the desktop calls by name', () => {
    const listed = new Set([...REQUIRED_METHODS, ...DESKTOP_REQUIRED_METHODS])
    const missing: string[] = []
    for (const path of sourceFiles(srcDir)) {
      for (const match of readFileSync(path, 'utf8').matchAll(CALL)) {
        if (!listed.has(match[2]!)) missing.push(`${match[2]} (${relative(srcDir, path)})`)
      }
    }
    expect(missing).toEqual([])
  })

  it('holds only what the console list does not, sorted and unique', () => {
    const shared = DESKTOP_REQUIRED_METHODS.filter((m) => REQUIRED_METHODS.includes(m))
    expect(shared).toEqual([])
    expect([...DESKTOP_REQUIRED_METHODS]).toEqual([...new Set(DESKTOP_REQUIRED_METHODS)].sort())
  })
})
