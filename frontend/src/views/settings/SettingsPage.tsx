import './settings.css'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircleIcon,
  BotIcon,
  CheckCircle2Icon,
  FileCode2Icon,
  KeyRoundIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  SlidersHorizontalIcon,
  XCircleIcon,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useRpc } from '@/app/providers'
import { t, tPlural } from '@/i18n'
import '@/i18n/en/settings'
import { SetupPage } from '@/views/setup/SetupPage'
import { ENV_QUERY_KEY, type EnvListResponse } from '@/views/env/logic'
import { ConfigPage } from '@/views/config/ConfigPage'
import { WebPushToggle } from './WebPushToggle'
import {
  loadSettingsSnapshot,
  readinessFromSnapshot,
  SETTINGS_SNAPSHOT_QUERY_KEY,
  type SettingsSnapshot,
} from './snapshot'

type SettingsSurface = 'guided' | 'advanced'

function initialSurface(pathname: string): SettingsSurface {
  return pathname.replace(/\/+$/, '').endsWith('/config') ? 'advanced' : 'guided'
}

export function SettingsPage() {
  const rpc = useRpc()
  const navigate = useNavigate()
  const { pathname, search, hash } = useLocation()
  const [surface, setSurface] = useState<SettingsSurface>(() => initialSurface(pathname))
  const [surfacePath, setSurfacePath] = useState(pathname)

  if (pathname !== surfacePath) {
    setSurfacePath(pathname)
    setSurface(initialSurface(pathname))
  }

  useEffect(() => {
    document.title = t('settings.documentTitle')
  }, [])

  useEffect(() => {
    const requestedStep = new URLSearchParams(search).get('step')
    if (requestedStep === 'channels' || hash === '#channels') {
      navigate('/channels?view=setup', { replace: true })
    }
  }, [hash, navigate, search])

  const snapshotQuery = useQuery<SettingsSnapshot>({
    queryKey: SETTINGS_SNAPSHOT_QUERY_KEY,
    queryFn: () => loadSettingsSnapshot(rpc),
    refetchOnWindowFocus: false,
  })

  // Shares ENV_QUERY_KEY with the Environment screen, so opening it from here
  // renders from cache instead of refetching.
  const envQuery = useQuery<EnvListResponse>({
    queryKey: ENV_QUERY_KEY,
    queryFn: () => rpc.call<EnvListResponse>('env.list', {}),
    refetchOnWindowFocus: false,
  })
  const envLabel = envQuery.data
    ? t('settings.envCount', { set: envQuery.data.setCount, total: envQuery.data.totalCount })
    : t('settings.envManage')

  const snapshot = snapshotQuery.data
  const snapshotUnavailable = snapshotQuery.isError && !snapshot
  const readiness = useMemo(() => readinessFromSnapshot(snapshot), [snapshot])
  const readinessLabel =
    readiness.total > 0
      ? t('settings.readinessCount', { ready: readiness.ready, total: readiness.total })
      : snapshotQuery.isError
        ? t('settings.statusUnavailable')
        : t('settings.statusChecking')
  const activeModel = snapshot?.config?.llm?.model || t('settings.modelUnset')
  const activeProviderId = snapshot?.config?.llm?.provider
  const activeProvider =
    snapshot?.catalog?.providers?.find((provider) => provider.providerId === activeProviderId)
      ?.label ||
    activeProviderId ||
    t('settings.providerUnset')
  const statusLabel = snapshot?.writeBlocked
    ? t('settings.statusChangesPaused')
    : snapshot?.pendingRestart
      ? t('settings.statusRestartNeeded')
      : snapshotQuery.isError
        ? t('settings.statusUnavailable')
        : readiness.actionRequired > 0
          ? tPlural('settings.setupItemsLeft', readiness.actionRequired)
          : snapshotQuery.isSuccess
            ? t('settings.statusReady')
            : t('settings.statusChecking')
  const statusTone = snapshot?.writeBlocked
    ? 'tone-danger'
    : snapshot?.pendingRestart
      ? 'tone-warn'
      : snapshotQuery.isError
        ? 'tone-danger'
        : readiness.actionRequired > 0
          ? 'tone-warn'
          : snapshotQuery.isSuccess
            ? 'tone-ok'
            : 'tone-info'
  const StatusIcon =
    snapshotQuery.isFetching && !snapshot
      ? LoaderCircleIcon
      : statusTone === 'tone-ok'
        ? CheckCircle2Icon
        : statusTone === 'tone-warn'
          ? AlertCircleIcon
          : XCircleIcon

  const reloadSnapshot = async () => {
    const result = await snapshotQuery.refetch()
    return result.data
  }

  const onSurfaceKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const nextSurface = event.key === 'ArrowLeft' || event.key === 'Home' ? 'guided' : 'advanced'
    setSurface(nextSurface)
    document.getElementById(`settings-tab-${nextSurface}`)?.focus()
  }

  return (
    <div className="settings-workspace">
      <header className="settings-stage__header">
        <div className="settings-stage__title-block">
          <span className="t-label">{t('settings.eyebrow')}</span>
          <h1 className="t-display">{t('settings.title')}</h1>
          <p className="settings-stage__subtitle">{t('settings.subtitle')}</p>
        </div>

        <div className="settings-stage__actions">
          <span className={`settings-health ${statusTone}`} role="status">
            <StatusIcon
              className={snapshotQuery.isFetching && !snapshot ? 'settings-spin' : ''}
              aria-hidden="true"
            />
            {statusLabel}
          </span>
          <Button
            type="button"
            variant="outline"
            className="text-xs uppercase tracking-[0.14em]"
            title={t('settings.refreshTitle')}
            aria-label={t('settings.refreshTitle')}
            aria-busy={snapshotQuery.isFetching}
            disabled={snapshotQuery.isFetching}
            onClick={() => void reloadSnapshot()}
          >
            <RefreshCwIcon className={snapshotQuery.isFetching ? 'settings-spin' : ''} />
            <span>
              {snapshotQuery.isFetching ? t('settings.refreshBusy') : t('settings.refresh')}
            </span>
          </Button>
        </div>
      </header>

      <section className="settings-toolbar" aria-label={t('settings.toolbarLandmark')}>
        <nav
          className="settings-surface-tabs"
          aria-label={t('settings.tabsLandmark')}
          role="tablist"
        >
          <button
            id="settings-tab-guided"
            type="button"
            role="tab"
            className={surface === 'guided' ? 'is-active' : ''}
            aria-selected={surface === 'guided'}
            aria-controls="settings-panel-guided"
            tabIndex={surface === 'guided' ? 0 : -1}
            onClick={() => setSurface('guided')}
            onKeyDown={onSurfaceKeyDown}
          >
            <SlidersHorizontalIcon aria-hidden="true" />
            <span>
              <strong>{t('settings.tabGuided')}</strong>
              <small>{t('settings.tabGuidedNote')}</small>
            </span>
          </button>
          <button
            id="settings-tab-advanced"
            type="button"
            role="tab"
            className={surface === 'advanced' ? 'is-active' : ''}
            aria-selected={surface === 'advanced'}
            aria-controls="settings-panel-advanced"
            tabIndex={surface === 'advanced' ? 0 : -1}
            onClick={() => setSurface('advanced')}
            onKeyDown={onSurfaceKeyDown}
          >
            <FileCode2Icon aria-hidden="true" />
            <span>
              <strong>{t('settings.tabAdvanced')}</strong>
              <small>{t('settings.tabAdvancedNote')}</small>
            </span>
          </button>
        </nav>

        <div className="settings-glance" aria-label={t('settings.glanceLandmark')}>
          <div className="settings-glance__item">
            <CheckCircle2Icon aria-hidden="true" />
            <span>
              <small>{t('settings.glanceProgress')}</small>
              <strong>{readinessLabel}</strong>
            </span>
          </div>
          <div className="settings-glance__item">
            <BotIcon aria-hidden="true" />
            <span>
              <small>{activeProvider}</small>
              <strong title={activeModel}>{activeModel}</strong>
            </span>
          </div>
          {/* A pointer, not a second copy of the table: environment variables
              are managed on their own screen so there is one place to look. */}
          <button
            type="button"
            className="settings-glance__item settings-glance__item--action"
            onClick={() => navigate('/env')}
          >
            <KeyRoundIcon aria-hidden="true" />
            <span>
              <small>{t('settings.glanceEnvironment')}</small>
              <strong>{envLabel}</strong>
            </span>
          </button>
        </div>
      </section>

      <WebPushToggle />

      {snapshot?.diskDiverged ? (
        <div className="settings-load-error tone-danger" role="alert">
          <span>{t('settings.divergedBody')}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => void reloadSnapshot()}>
            {t('settings.divergedAction')}
          </Button>
        </div>
      ) : null}

      {snapshotQuery.isError ? (
        <div className="settings-load-error tone-danger" role="alert">
          <span>{t('settings.loadErrorBody')}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => void reloadSnapshot()}>
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      <section
        id="settings-panel-guided"
        className="settings-surface"
        role="tabpanel"
        aria-labelledby="settings-tab-guided"
        hidden={surface !== 'guided'}
        tabIndex={0}
      >
        {!snapshotUnavailable ? (
          <SetupPage
            embedded
            externalSnapshot={snapshot ?? null}
            onSnapshotReload={reloadSnapshot}
          />
        ) : null}
      </section>
      <section
        id="settings-panel-advanced"
        className="settings-surface"
        role="tabpanel"
        aria-labelledby="settings-tab-advanced"
        hidden={surface !== 'advanced'}
        tabIndex={0}
      >
        {!snapshotUnavailable ? (
          <ConfigPage
            embedded
            externalSnapshot={snapshot ?? null}
            externalSnapshotError={snapshotUnavailable}
            onSnapshotReload={reloadSnapshot}
          />
        ) : null}
      </section>
    </div>
  )
}
