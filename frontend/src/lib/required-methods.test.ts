import { readFileSync, readdirSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { REQUIRED_METHODS } from './required-methods'

/**
 * Keeps REQUIRED_METHODS honest for every literal call site: a new
 * `rpc.call('x.y')` anywhere in the console fails here until it is listed,
 * and the list is what the Python contract test checks against the
 * gateway's registry. Names built at runtime are listed by hand.
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

/** `call('a.b'` / `call<T>(\n 'a.b'` -> `a.b`. Template literals are skipped (runtime names). */
const CALL = /\bcall(?:<(?:[^<>]|<[^<>]*>)*>)?\(\s*(['"])([A-Za-z][A-Za-z0-9_.]*)\1/g

function literalCalls(): Map<string, string[]> {
  const calls = new Map<string, string[]>()
  for (const path of sourceFiles(srcDir)) {
    const text = readFileSync(path, 'utf8')
    for (const match of text.matchAll(CALL)) {
      const method = match[2]!
      const where = calls.get(method) ?? []
      where.push(relative(srcDir, path))
      calls.set(method, where)
    }
  }
  return calls
}

describe('REQUIRED_METHODS', () => {
  it('lists every RPC method the console calls by name', () => {
    const listed = new Set(REQUIRED_METHODS)
    const missing = [...literalCalls()]
      .filter(([method]) => !listed.has(method))
      .map(([method, where]) => `${method} (${[...new Set(where)].join(', ')})`)
    expect(missing).toEqual([])
  })

  it('is sorted and free of duplicates', () => {
    expect([...REQUIRED_METHODS]).toEqual([...new Set(REQUIRED_METHODS)].sort())
  })
})
