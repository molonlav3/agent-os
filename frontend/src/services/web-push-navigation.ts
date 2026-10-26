/**
 * Notification clicks for Web Push (services/web-push.ts, public/sw.js).
 *
 * The service worker's scope is the build directory, so it controls no
 * console page and cannot navigate one itself: it posts the deep link to an
 * open console instead, and the shell routes it here. Kept apart from the
 * subscribe flow because the shell loads it on every page.
 */
import { controlBasePath } from '@/lib/control-base'

/** What sw.js posts to an open console when a notification is clicked. */
export const PUSH_NAVIGATE_MESSAGE = 'agentos.push.navigate'

/**
 * The router path (basename-relative) for a deep link on this origin, or null
 * when it leaves the console.
 */
export function pushRouterPath(url: string, basePath: string, origin: string): string | null {
  let target: URL
  try {
    target = new URL(url, origin)
  } catch {
    return null
  }
  if (target.origin !== origin) return null
  const base = basePath === '/' ? '' : basePath.replace(/\/+$/, '')
  if (base && target.pathname !== base && !target.pathname.startsWith(`${base}/`)) return null
  return `${target.pathname.slice(base.length) || '/'}${target.search}${target.hash}`
}

/** Route the console when its service worker relays a notification click. */
export function listenForPushNavigation(
  navigate: (path: string) => void,
  container: Pick<
    ServiceWorkerContainer,
    'addEventListener' | 'removeEventListener' | 'startMessages'
  > | null = typeof navigator !== 'undefined' && 'serviceWorker' in navigator
    ? navigator.serviceWorker
    : null,
): () => void {
  if (!container) return () => {}
  const onMessage = (event: MessageEvent): void => {
    const data = event.data as { type?: unknown; url?: unknown } | null
    if (data?.type !== PUSH_NAVIGATE_MESSAGE || typeof data.url !== 'string') return
    const path = pushRouterPath(data.url, controlBasePath(), window.location.origin)
    if (path) navigate(path)
  }
  container.addEventListener('message', onMessage)
  container.startMessages()
  return () => container.removeEventListener('message', onMessage)
}
