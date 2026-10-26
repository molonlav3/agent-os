/** Runtime view of the gateway process as seen by the desktop shell. */
export type GatewayState = 'stopped' | 'starting' | 'running' | 'stopping' | 'error'

export interface GatewayStatus {
  state: GatewayState
  /** Present while the app manages the process. */
  pid: number | null
  /** Base HTTP URL, e.g. http://127.0.0.1:18791. */
  url: string | null
  /** Last error message, if `state === 'error'`. */
  error: string | null
  /**
   * What the supervisor is doing right now, e.g. "Retrying start" after a
   * configuration error. The supervisor always sets it; it is optional only
   * so a status literal written before it existed still type-checks.
   */
  detail?: string | null
  /** Exit code of the last gateway child the app spawned, if it exited. */
  exitCode?: number | null
}

export const STOPPED_GATEWAY: GatewayStatus = {
  state: 'stopped',
  pid: null,
  url: null,
  error: null,
  detail: null,
  exitCode: null,
}

/** `agentos gateway run` exits with this (EX_CONFIG) when only a config fix can help. */
export const GATEWAY_EXIT_CONFIG = 78
