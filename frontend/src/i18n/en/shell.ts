import { defineNamespace } from '../registry'

/**
 * The app shell: navigation, connection footer, route errors, the global
 * approval prompt, and the shared chrome components. View bodies live in their
 * own namespaces.
 */
export const shell = defineNamespace('shell', {
  // Brand. Not translated in practice, but routed through the catalog so the
  // shell has no literal copy left for a translator to miss.
  brandName: 'AgentOS',
  brandSuffix: 'Control',

  // View titles. One definition, read by both the router and the sidebar.
  viewAgents: 'Agents',
  viewApprovals: 'Approvals',
  viewChannels: 'Channels',
  viewChat: 'Chat',
  viewConfig: 'Config',
  viewCron: 'Cron',
  viewEnv: 'Environment',
  viewHealth: 'Health',
  viewInbox: 'Inbox',
  viewLogs: 'Logs',
  viewMcp: 'MCP Servers',
  viewMemory: 'Memory',
  viewOverview: 'Overview',
  viewProjects: 'Projects',
  viewSessions: 'Sessions',
  viewSettings: 'Agent Setup',
  viewSetup: 'Setup',
  viewSkills: 'Skills',
  viewUsage: 'Usage',

  // Sidebar groups.
  navGroupChat: 'Chat',
  navGroupControl: 'Control',
  navGroupSettings: 'Settings',
  navLandmark: 'Main',
  navDrawerLandmark: 'Workspace navigation',
  navCollapse: 'Collapse navigation',
  navExpand: 'Expand navigation',
  navClose: 'Close navigation',
  navToggleMenu: 'Toggle menu',
  navSkipToContent: 'Skip to main content',
  navApprovalBadge_one: '{count} pending approval',
  navApprovalBadge_other: '{count} pending approvals',
  navInboxBadge_one: '{count} item needs attention',
  navInboxBadge_other: '{count} items need attention',

  // Connection footer (the truth badge's tooltip).
  connWithVersion: '{state}, version {version}',
  connecting: 'Connecting…',

  // Sidebar utilities.
  shortcutsTitle: 'Keyboard shortcuts ({combo})',
  shortcutsLabel: 'Keyboard shortcuts',
  shortcutClose: 'Close dialog',
  shortcutDrawerEscape: 'Close the navigation drawer (mobile)',
  shortcutCategoryGlobal: 'Global',
  shortcutSeparator: 'then',
  shortcutCategoryNavigation: 'Navigation',
  shortcutCategoryChat: 'Chat',
  shortcutNewChat: 'Start a new chat',
  shortcutAbortTurn: 'Abort the streaming turn, else recover the queue into the composer',
  shortcutNavChat: 'Go to Chat',
  shortcutNavOverview: 'Go to Overview',
  shortcutNavHealth: 'Go to Health',
  shortcutNavChannels: 'Go to Channels',
  shortcutNavMcp: 'Go to MCP Servers',
  shortcutNavSkills: 'Go to Skills',
  shortcutNavProjects: 'Go to Projects',
  shortcutNavSessions: 'Go to Sessions',
  shortcutNavAgents: 'Go to Agents',
  shortcutNavUsage: 'Go to Usage',
  shortcutNavCron: 'Go to Cron',
  shortcutNavSettings: 'Go to Agent Setup',
  shortcutNavEnv: 'Go to Environment',
  shortcutNavLogs: 'Go to Logs',
  shortcutNavMemory: 'Go to Memory',
  shortcutNavApprovals: 'Go to Approvals',
  shortcutNavInbox: 'Go to the attention inbox',
  themeDark: 'dark',
  themeLight: 'light',
  themeTitle: 'Theme: {mode}',
  themeToggleLabel: 'Theme: {mode}. Toggle theme',

  // Chat surface header.
  chatToolbar: 'Chat toolbar',

  // Routing.
  routeNotFoundTitle: 'Not Found - AgentOS Control',
  routeNotFoundBody: 'Page not found: {path}',
  routePending: 'Opening view…',

  // Route error boundary.
  errorDocumentTitle: 'Recovery - AgentOS Control',
  errorHttpCode: 'HTTP {status}',
  errorViewCode: 'VIEW ERROR',
  errorUnavailableTitle: 'This view is unavailable',
  errorSnagTitle: 'This view hit a snag',
  errorMessage:
    'AgentOS kept the rest of your workspace intact. Reload this view or return to Overview.',
  errorEyebrow: 'Workspace recovery',
  errorStatus: 'View paused safely',
  errorDeveloperDetail: 'Developer detail',
  errorActions: 'Recovery actions',
  errorReload: 'Reload view',
  errorGoOverview: 'Go to Overview',

  // Global approval prompt.
  approvalTitle: 'Approval Required',
  approvalFallbackTool: 'Tool execution',
  approvalOnceTitle: 'Approve only this pending tool call',
  approvalOnce: 'Approve This Time',
  approvalAlwaysTitle: 'Remember this operation type for future matching intents',
  approvalAlways: 'Always Allow This Type',
  approvalBypassTitle:
    'Enable approval bypass in this browser session and approve this pending tool call',
  approvalBypass: 'Bypass Approvals',
  approvalDeny: 'Deny',

  // Command line with copy action.
  commandCopyLabel: 'Copy command',
  commandCopied: 'Copied command',
  commandCopyFailed: 'Copy failed: {message}',
  commandCopyError: 'Copy command failed',

  // Update banner.
  updateAvailable:
    'A new version of use-agent-os is available: {current} → {latest}. Run agentos upgrade.',
  updateDismiss: 'Dismiss',
  updateLabel: 'Update',
  updateNow: 'Update now',
  updateRetry: 'Retry',
  updateRunning: 'Updating AgentOS… the gateway restarts when the install finishes.',
  updateRunningShort: 'Updating…',
  updateDone: 'Updated to {latest}. Reload to load the new console.',
  updateFailed: 'Update failed: {detail}',
  updateReload: 'Reload',

  // Truth badge: the footer's runtime status (state, source + freshness, and
  // one recovery action).
  truthReady: 'Ready',
  truthDegraded: 'Degraded',
  truthConnecting: 'Connecting',
  truthBlocked: 'Blocked',
  truthOffline: 'Offline',
  truthUnknown: 'Unknown',
  truthLive: 'Live · {age}',
  truthSnapshot: 'Snapshot · {age}',
  truthSnapshotAgeless: 'Snapshot',
  truthUnavailable: 'Unavailable',
  truthBadgeLabel: 'Runtime {state}, {freshness}. Show details',
  truthPanelTitle: 'Runtime status',
  truthPanelSource: 'Source',
  truthPanelAttention_one: '{count} item needs you',
  truthPanelAttention_other: '{count} items need you',
  truthPanelAttentionNone: 'Nothing is waiting on you',
  truthReasonOk: 'Connected, and the doctor reports no errors.',
  truthReasonConnecting: 'Opening the gateway connection.',
  truthReasonGatewayStarting: 'The gateway process is starting.',
  truthReasonGatewayStopped: 'The gateway process is not running.',
  truthReasonDisconnected: 'Lost the gateway connection. What you see is the last snapshot.',
  truthReasonNeverConnected: 'The gateway has not answered yet.',
  truthReasonAuthFailed: 'The gateway refused this console’s credentials.',
  truthReasonProtocolMismatch: 'The gateway speaks a different protocol version than this console.',
  truthReasonHandshakeRefused: 'The gateway refused the connection.',
  truthReasonStale: 'No frame from the gateway for {age}. The view may be stale.',
  truthReasonDoctorErrors_one: '{count} doctor finding is at error level.',
  truthReasonDoctorErrors_other: '{count} doctor findings are at error level.',
  truthReasonSafeMode: 'The gateway is running in safe mode.',
  truthReasonDraining: 'The gateway is draining before a restart.',
  truthReasonStarting: 'The gateway is still starting up.',
  truthReasonChecking: 'Connected. Waiting for the first doctor report.',
  truthReasonDoctorUnavailable: 'Connected, but the doctor report could not be loaded.',
  truthActionRetry: 'Retry connection',
  truthActionStart: 'Start gateway',
  truthActionInbox: 'Open attention inbox',
  truthActionHealth: 'Open Health',
  ageSeconds: '{seconds} s',
  ageMinutes: '{minutes} min',
  ageHours: '{hours} h',

  // Typed runtime issues (attention inbox, desktop attention panel).
  issueGatewayUnreachableTitle: 'Gateway unreachable',
  issueGatewayUnreachableDetail:
    'The console lost its connection to the gateway. Live updates and actions are paused until it reconnects.',
  issueAuthFailedTitle: 'Gateway refused the credentials',
  issueAuthFailedDetail:
    'The handshake was refused ({message}). Check the gateway token, then retry.',
  issueProtocolMismatchTitle: 'Console and gateway protocol differ',
  issueProtocolMismatchDetail:
    'The gateway answered “{message}”. Upgrade whichever side is older, then retry.',
  issueSafeModeTitle: 'Gateway in safe mode',
  issueSafeModeDetail: 'The gateway started in safe mode. Failing: {failing}.',
  issueSafeModeDetailBare: 'The gateway started in safe mode; some subsystems are off.',
  issueUpdateFailedTitle: 'Update failed',
  issueFindingUntitled: 'Doctor finding {id}',
  issueActionRestart: 'Restart gateway',
  issueActionRetry: 'Retry connection',
  issueActionFixSteps: 'Open doctor fix steps',
  issueActionSettings: 'Open settings',
  issueActionDismiss: 'Dismiss',
  issueRestartConfirm:
    'Restart the gateway? Running turns are interrupted and pick up again once it is back.',

  // Disabled-with-reason: why a visible control cannot be used right now.
  reasonGatewayOffline: 'The gateway is offline. Reconnect to use this.',
  reasonGatewayConnecting: 'Waiting for the gateway connection.',
  reasonSchedulerUnavailable: 'The scheduler is not running on this gateway.',
  reasonApprovalsPending_one: '{count} approval is waiting. Answer it before updating.',
  reasonApprovalsPending_other: '{count} approvals are waiting. Answer them before updating.',
  reasonPreflightChecking: 'Checking what an update would interrupt.',

  // Update preflight.
  preflightTitle: 'Before updating AgentOS',
  preflightLead: 'The gateway restarts during the update. This is what it would interrupt:',
  preflightSessions_one: '{count} session is running',
  preflightSessions_other: '{count} sessions are running',
  preflightJobs_one: '{count} scheduled job is due in the next 10 minutes',
  preflightJobs_other: '{count} scheduled jobs are due in the next 10 minutes',
  preflightApprovals_one: '{count} approval is pending',
  preflightApprovals_other: '{count} approvals are pending',
  preflightNothing: 'Nothing is running, due, or waiting on you.',
  preflightChecking: 'Checking what is running…',
  preflightCheckFailed: 'Could not check what is running: {message}',
  preflightConfirm: 'Update now',
  preflightConfirmAnyway: 'Update anyway',
  preflightCancel: 'Cancel',
  preflightOpenInbox: 'Open attention inbox',
} as const)
