import { LayoutPanelLeft, Settings } from 'lucide-react'
import { AttentionPanel } from '~/components/AttentionPanel'
import { NotificationBell } from '~/components/NotificationBell'
import { TruthBadge } from '~/components/TruthBadge'
import { Button } from '~/components/ui/button'
import { UpdatePill } from '~/components/UpdatePill'
import { t } from '~/i18n'
import { useUi } from '~/stores/ui'
import { ThemeToggle } from '~/theme/ThemeToggle'

/**
 * Content-column toolbar. Left side holds the one sidebar toggle, next to the
 * sidebar it controls (and past the traffic lights once it is hidden); right
 * side holds window-level controls, the way Mail and Notes do: the runtime
 * truth badge, the update pill, what needs you, the bell, theme, settings.
 */
export function Toolbar() {
  const toggleSidebar = useUi((s) => s.toggleSidebar)
  const sidebarOpen = useUi((s) => s.sidebarOpen)
  const settingsOpen = useUi((s) => s.settingsOpen)
  const openSettings = useUi((s) => s.openSettings)
  const sidebarLabel = t(sidebarOpen ? 'sidebar.collapse' : 'sidebar.expand')

  return (
    <header
      className="app-drag relative flex shrink-0 items-center justify-between px-3"
      style={{
        height: 'var(--toolbar-height)',
        zIndex: 'var(--z-toolbar)',
        paddingLeft: sidebarOpen ? undefined : 82,
      }}
    >
      <div className="app-no-drag">
        <Button
          variant="ghost"
          size="icon"
          aria-label={sidebarLabel}
          title={`${sidebarLabel} (⌘⇧S)`}
          onClick={toggleSidebar}
        >
          <LayoutPanelLeft
            className="size-4 text-muted-foreground"
            strokeWidth={1.75}
            aria-hidden
          />
        </Button>
      </div>
      <div className="app-no-drag flex items-center gap-0.5">
        <TruthBadge />
        <UpdatePill />
        <AttentionPanel />
        <NotificationBell />
        <ThemeToggle />
        <Button
          variant={settingsOpen ? 'secondary' : 'ghost'}
          size="icon"
          aria-label={t('toolbar.settings')}
          title={`${t('toolbar.settings')} (⌘,)`}
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
          onClick={() => openSettings()}
        >
          <Settings className="size-4 text-muted-foreground" strokeWidth={1.75} aria-hidden />
        </Button>
      </div>
    </header>
  )
}
