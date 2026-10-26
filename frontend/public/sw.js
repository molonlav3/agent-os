// AgentOS Control: the service worker behind "Push notifications for
// approvals" (Settings). It is registered only when the user turns that on,
// and only shows and clears approval notifications: it caches nothing and
// answers no fetches, so the console always loads from the gateway.
//
// It lives at {controlBase}/static/dist/sw.js, so its scope is the build
// directory and it controls no console page. Push delivery does not need
// that; a click reaches an open console with postMessage instead.

'use strict'

const NAVIGATE_MESSAGE = 'agentos.push.navigate'

// An updated worker takes over at once; there is no page state to hand over.
self.addEventListener('install', () => self.skipWaiting())

// The console root, derived from this worker's own location
// ({controlBase}/static/dist/ -> {controlBase}/).
function consoleRoot() {
  return new URL('../../', self.registration.scope).href
}

// A deep link from the payload, kept on this origin.
function targetUrl(raw) {
  try {
    const url = new URL(String(raw || ''), self.location.origin)
    if (url.origin === self.location.origin) return url.href
  } catch (_error) {
    // Fall through to the console root.
  }
  return consoleRoot()
}

async function onPush(payload) {
  if (!payload || typeof payload !== 'object') return
  const tag = String(payload.tag || `agentos-approval-${payload.id || ''}`)
  const body = String(payload.summary || '')
  const data = { url: targetUrl(payload.url) }

  if (payload.type === 'approval.resolved' || payload.state === 'resolved') {
    // A decided approval must not leave its prompt behind. Replace one that
    // is still on screen with a quiet "no longer pending" note; when it is
    // already gone there is nothing to correct.
    const open = await self.registration.getNotifications({ tag })
    if (open.length === 0) return
    open.forEach((notification) => notification.close())
    await self.registration.showNotification(
      String(payload.title || 'Approval no longer pending'),
      {
        body,
        tag,
        data,
        silent: true,
      },
    )
    return
  }

  await self.registration.showNotification(String(payload.title || 'Approval needed'), {
    body,
    tag,
    data,
    // Stays until acted on where the platform allows it, and a repeat for the
    // same approval alerts again instead of updating silently.
    requireInteraction: true,
    renotify: true,
  })
}

self.addEventListener('push', (event) => {
  let payload = null
  try {
    payload = event.data ? event.data.json() : null
  } catch (_error) {
    payload = null
  }
  event.waitUntil(onPush(payload))
})

async function openConsole(url) {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  const client = windows.find((candidate) => candidate.focused) || windows[0]
  if (client) {
    // Not controlled by this worker, so `client.navigate()` is refused: the
    // console routes itself when told (services/web-push-navigation.ts).
    client.postMessage({ type: NAVIGATE_MESSAGE, url })
    try {
      await client.focus()
      return
    } catch (_error) {
      // Not allowed to take focus: open a window instead.
    }
  }
  await self.clients.openWindow(url)
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  event.waitUntil(openConsole(targetUrl(data.url)))
})
