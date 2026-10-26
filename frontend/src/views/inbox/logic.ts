import type { SettingsTarget } from '@/lib/runtime-issues'

/**
 * Where the web console sends a runtime issue's "Open settings". Null when
 * the web console has no such screen (it cannot drive the engine updater):
 * the action is then not offered rather than offered and inert.
 */
export function webSettingsPath(target: SettingsTarget | undefined): string | null {
  switch (target) {
    case 'connection':
      return '/overview'
    case 'providers':
      return '/settings'
    case 'channels':
      return '/channels'
    default:
      return null
  }
}

/** The chat deep link for a session key. */
export function chatPath(sessionKey: string): string {
  return '/chat?session=' + encodeURIComponent(sessionKey)
}
