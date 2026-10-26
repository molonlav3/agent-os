import './update-preflight.css'
import { useEffect } from 'react'
import { useNavigate } from 'react-router'
import { ModalShell } from '@/components/ModalShell'
import { DisabledReason } from '@/components/DisabledReason'
import { Button } from '@/components/ui/button'
import { t, tPlural } from '@/i18n'
import { useUpdatePreflight } from '@/lib/update-preflight'

/**
 * The confirm step in front of `updates.apply`. It says what the gateway
 * restart would interrupt (running sessions, jobs due in the next 10
 * minutes, pending approvals), blocks with the reason while an approval is
 * waiting, and otherwise applies only after the operator confirms.
 */
export function UpdatePreflightDialog({
  latest,
  onConfirm,
  onCancel,
}: {
  latest: string
  onConfirm: () => void
  onCancel: () => void
}) {
  const navigate = useNavigate()
  const { preflight, loading, error, gate, run } = useUpdatePreflight()

  // One read per opening; the dialog mounts when the operator asks to update.
  useEffect(() => {
    run()
  }, [run])

  const sessions = preflight?.runningSessions.length ?? 0
  const jobs = preflight?.dueJobs ?? []
  const approvals = preflight?.pendingApprovals ?? 0
  const nothing = preflight !== null && !preflight.warn && approvals === 0

  return (
    <ModalShell
      role="alertdialog"
      labelledBy="update-preflight-title"
      describedBy="update-preflight-body"
      onClose={onCancel}
      overlayClassName="upf-modal__overlay"
      className="upf-modal"
    >
      <h2 id="update-preflight-title" className="upf-modal__title">
        {t('shell.preflightTitle')}
        {latest ? <span className="upf-modal__version"> · {latest}</span> : null}
      </h2>
      <div id="update-preflight-body" className="upf-modal__body" data-testid="update-preflight">
        <p>{t('shell.preflightLead')}</p>
        {loading && !preflight ? (
          <p className="upf-modal__muted">{t('shell.preflightChecking')}</p>
        ) : null}
        {error ? (
          <p className="upf-modal__error" role="alert">
            {t('shell.preflightCheckFailed', { message: error })}
          </p>
        ) : null}
        {preflight ? (
          <ul className="upf-modal__list">
            <li data-tone={sessions ? 'warn' : 'ok'} data-testid="preflight-sessions">
              {tPlural('shell.preflightSessions', sessions)}
            </li>
            <li data-tone={jobs.length ? 'warn' : 'ok'} data-testid="preflight-jobs">
              {tPlural('shell.preflightJobs', jobs.length)}
              {jobs.length ? (
                <span className="upf-modal__names"> ({jobs.map((j) => j.name).join(', ')})</span>
              ) : null}
            </li>
            <li data-tone={approvals ? 'danger' : 'ok'} data-testid="preflight-approvals">
              {tPlural('shell.preflightApprovals', approvals)}
            </li>
          </ul>
        ) : null}
        {nothing ? <p className="upf-modal__muted">{t('shell.preflightNothing')}</p> : null}
      </div>
      <div className="upf-modal__foot">
        {gate.code === 'approvals_pending' ? (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              onCancel()
              void navigate('/inbox')
            }}
          >
            {t('shell.preflightOpenInbox')}
          </Button>
        ) : null}
        <Button type="button" variant="outline" onClick={onCancel}>
          {t('shell.preflightCancel')}
        </Button>
        <DisabledReason gate={gate} className="upf-modal__gate">
          <Button
            type="button"
            variant={preflight?.warn || error ? 'destructive' : 'default'}
            disabled={gate.disabled}
            data-testid="preflight-confirm"
            onClick={onConfirm}
          >
            {preflight?.warn || error
              ? t('shell.preflightConfirmAnyway')
              : t('shell.preflightConfirm')}
          </Button>
        </DisabledReason>
      </div>
    </ModalShell>
  )
}
