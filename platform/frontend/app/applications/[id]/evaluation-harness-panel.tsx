'use client'

import { ClipboardCheck, Play, RefreshCw, Save } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { messages, type Locale } from '@/lib/i18n'
import {
  api,
  idempotency,
  isAuthError,
  type Draft,
  type EvaluationEnvironment,
  type EvaluationPlan,
  type EvaluationProfile,
  type EvaluationRunRecord,
} from '@/lib/platform'

type BusyAction = 'catalog' | 'preview' | 'apply' | 'run' | 'history' | ''

type EvaluationHarnessPanelProps = {
  applicationId: string
  draft: Draft | null
  locale: Locale
  onRefreshDraft: () => Promise<unknown>
  onNotice: (message: string) => void
  onAuthRequired: () => void
  onDraftTestsChanged: () => void
}

function testId(value: Record<string, unknown>) {
  return typeof value.id === 'string' ? value.id : ''
}

function dateLabel(value: string, locale: Locale) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed)
}

export function EvaluationHarnessPanel({
  applicationId,
  draft,
  locale,
  onRefreshDraft,
  onNotice,
  onAuthRequired,
  onDraftTestsChanged,
}: EvaluationHarnessPanelProps) {
  const t = messages[locale]
  const [profiles, setProfiles] = useState<EvaluationProfile[]>([])
  const [environments, setEnvironments] = useState<EvaluationEnvironment[]>([])
  const [profileId, setProfileId] = useState('h2_component')
  const [environmentId, setEnvironmentId] = useState('local_sandbox')
  const [plan, setPlan] = useState<EvaluationPlan | null>(null)
  const [runs, setRuns] = useState<EvaluationRunRecord[]>([])
  const [latestRun, setLatestRun] = useState<EvaluationRunRecord | null>(null)
  const [busy, setBusy] = useState<BusyAction>('')
  const [error, setError] = useState('')

  const handleError = useCallback((caught: unknown) => {
    const message = String(caught)
    setError(message)
    onNotice(message)
    if (isAuthError(caught)) onAuthRequired()
  }, [onAuthRequired, onNotice])

  const loadCatalog = useCallback(async () => {
    setBusy('catalog')
    setError('')
    try {
      const [nextProfiles, nextEnvironments, nextRuns] = await Promise.all([
        api<EvaluationProfile[]>('/api/v1/evaluation/profiles'),
        api<EvaluationEnvironment[]>('/api/v1/evaluation/environments'),
        api<EvaluationRunRecord[]>(`/api/v1/applications/${applicationId}/evaluation/runs?limit=20`),
      ])
      setProfiles(nextProfiles)
      setEnvironments(nextEnvironments)
      setRuns(nextRuns)
      const preferredProfile = nextProfiles.find(item => item.id === profileId) || nextProfiles[0]
      if (preferredProfile) {
        setProfileId(preferredProfile.id)
        const preferredEnvironment = nextEnvironments.find(
          item => item.id === environmentId && item.compatible_profile_ids.includes(preferredProfile.id),
        ) || nextEnvironments.find(item => item.compatible_profile_ids.includes(preferredProfile.id))
        if (preferredEnvironment) setEnvironmentId(preferredEnvironment.id)
      }
    } catch (caught) {
      handleError(caught)
    } finally {
      setBusy('')
    }
  }, [applicationId, environmentId, handleError, profileId])

  useEffect(() => {
    void loadCatalog()
  // The catalog is loaded once per application; profile changes are local UI state.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applicationId])

  const selectedProfile = profiles.find(item => item.id === profileId) || null
  const compatibleEnvironments = useMemo(
    () => environments.filter(item => item.compatible_profile_ids.includes(profileId)),
    [environments, profileId],
  )
  const selectedEnvironment = environments.find(item => item.id === environmentId) || null
  const planIsCurrent = Boolean(plan && draft && plan.draft_content_hash === draft.content_hash)
  const currentTestIds = new Set((draft?.snapshot.tests || []).map(testId).filter(Boolean))
  const generatedTestsApplied = Boolean(
    plan?.generated_tests.length
    && plan.generated_tests.every(item => currentTestIds.has(testId(item))),
  )
  const runtimeNeedsAppliedCases = Boolean(
    plan?.profile.execution_mode === 'runtime'
    && plan.generated_tests.length
    && !generatedTestsApplied,
  )
  const canApply = Boolean(
    planIsCurrent
    && plan?.eligibility === 'ready'
    && plan.profile.draft_test_apply_allowed
    && plan.generated_tests.length
    && !generatedTestsApplied,
  )
  const canRun = Boolean(
    planIsCurrent
    && plan?.eligibility === 'ready'
    && selectedEnvironment?.availability === 'available'
    && !runtimeNeedsAppliedCases,
  )
  const visibleRun = latestRun || runs[0] || null

  function chooseProfile(nextProfileId: string) {
    const nextEnvironment = environments.find(
      item => item.compatible_profile_ids.includes(nextProfileId) && item.availability === 'available',
    ) || environments.find(item => item.compatible_profile_ids.includes(nextProfileId))
    setProfileId(nextProfileId)
    if (nextEnvironment) setEnvironmentId(nextEnvironment.id)
    setPlan(null)
    setLatestRun(null)
    setError('')
  }

  function chooseEnvironment(nextEnvironmentId: string) {
    setEnvironmentId(nextEnvironmentId)
    setPlan(null)
    setLatestRun(null)
    setError('')
  }

  async function previewPlan() {
    if (!draft || busy) return
    setBusy('preview')
    setError('')
    try {
      const result = await api<EvaluationPlan>(
        `/api/v1/applications/${applicationId}/evaluation/plan`,
        {
          method: 'POST',
          body: JSON.stringify({ profile_id: profileId, environment_id: environmentId }),
        },
      )
      setPlan(result)
      setLatestRun(null)
      onNotice(t.evaluationPlanReady)
    } catch (caught) {
      handleError(caught)
    } finally {
      setBusy('')
    }
  }

  async function applyGeneratedTests() {
    if (!draft || !canApply || busy) return
    setBusy('apply')
    setError('')
    try {
      await api(`/api/v1/applications/${applicationId}/evaluation/tests/apply`, {
        method: 'POST',
        body: JSON.stringify({
          profile_id: profileId,
          environment_id: environmentId,
          expected_revision: draft.revision,
          expected_content_hash: draft.content_hash,
          mode: 'replace_generated',
          idempotency_key: idempotency(),
        }),
      })
      onDraftTestsChanged()
      await onRefreshDraft()
      const refreshedPlan = await api<EvaluationPlan>(
        `/api/v1/applications/${applicationId}/evaluation/plan`,
        {
          method: 'POST',
          body: JSON.stringify({ profile_id: profileId, environment_id: environmentId }),
        },
      )
      setPlan(refreshedPlan)
      onNotice(t.evaluationCasesApplied(refreshedPlan.generated_tests.length))
    } catch (caught) {
      handleError(caught)
      await onRefreshDraft().catch(() => undefined)
      setPlan(null)
    } finally {
      setBusy('')
    }
  }

  async function runEvaluation() {
    if (!draft || !canRun || busy) return
    setBusy('run')
    setError('')
    try {
      const result = await api<EvaluationRunRecord>(
        `/api/v1/applications/${applicationId}/evaluation/runs`,
        {
          method: 'POST',
          body: JSON.stringify({
            profile_id: profileId,
            environment_id: environmentId,
            expected_revision: draft.revision,
            expected_content_hash: draft.content_hash,
          }),
        },
      )
      setLatestRun(result)
      setRuns(current => [result, ...current.filter(item => item.id !== result.id)].slice(0, 20))
      onNotice(result.passed ? t.evaluationRunPassed : t.evaluationRunFinished(result.achieved_status))
    } catch (caught) {
      handleError(caught)
      await onRefreshDraft().catch(() => undefined)
      setPlan(null)
    } finally {
      setBusy('')
    }
  }

  async function refreshHistory() {
    if (busy) return
    setBusy('history')
    setError('')
    try {
      const nextRuns = await api<EvaluationRunRecord[]>(
        `/api/v1/applications/${applicationId}/evaluation/runs?limit=20`,
      )
      setRuns(nextRuns)
      setLatestRun(null)
    } catch (caught) {
      handleError(caught)
    } finally {
      setBusy('')
    }
  }

  return <section className="evaluation-workbench" data-evaluation-harness="studio">
    <header className="evaluation-heading">
      <div>
        <span className="panel-kicker">{t.evaluationKicker}</span>
        <h2>{t.evaluationTitle}</h2>
        <p>{t.evaluationHelp}</p>
      </div>
      <button
        aria-label={t.evaluationRefreshCatalog}
        className="evaluation-icon-button"
        disabled={Boolean(busy)}
        onClick={() => void loadCatalog()}
        title={t.evaluationRefreshCatalog}
        type="button"
      ><RefreshCw aria-hidden="true" size={15} /></button>
    </header>

    <div
      aria-label={t.evaluationProfileLabel}
      className="evaluation-profile-control"
      data-evaluation-profile-controls="h0-h5"
      role="radiogroup"
    >
      {profiles.map(item => <button
        aria-checked={profileId === item.id}
        className={profileId === item.id ? 'active' : ''}
        data-evaluation-profile={item.id}
        key={item.id}
        onClick={() => chooseProfile(item.id)}
        role="radio"
        title={item.description}
        type="button"
      ><b>{item.level}</b><span>{t.evaluationProfileName(item.level)}</span></button>)}
    </div>

    <label className="evaluation-environment" data-evaluation-environment="selector">
      <span>{t.evaluationEnvironmentLabel}</span>
      <select
        disabled={!compatibleEnvironments.length || Boolean(busy)}
        onChange={event => chooseEnvironment(event.target.value)}
        value={environmentId}
      >
        {compatibleEnvironments.map(item => <option key={item.id} value={item.id}>
          {t.evaluationEnvironmentName(item.id)} · {t.evaluationAvailability(item.availability)}
        </option>)}
      </select>
    </label>

    {selectedProfile && selectedEnvironment && <div className="evaluation-boundary-grid">
      <div><span>{t.evaluationExecutionMode}</span><b>{t.evaluationExecutionName(selectedProfile.execution_mode)}</b></div>
      <div><span>{t.evaluationMutationBoundary}</span><b>{selectedEnvironment.external_mutation_allowed ? t.evaluationExternalMutation : t.evaluationNoExternalMutation}</b></div>
      <div><span>{t.evaluationClaimCeiling}</span><b>{selectedEnvironment.claim_ceiling}</b></div>
      <p className={selectedEnvironment.availability === 'available' ? 'available' : 'unavailable'}>
        {t.evaluationAvailability(selectedEnvironment.availability)}
        {selectedEnvironment.missing_requirements.length > 0 ? ` · ${selectedEnvironment.missing_requirements.join('; ')}` : ''}
      </p>
    </div>}

    <div className="evaluation-primary-actions">
      <button data-evaluation-action="preview" disabled={!draft || Boolean(busy)} onClick={() => void previewPlan()} type="button">
        <ClipboardCheck aria-hidden="true" size={15} />
        {busy === 'preview' ? t.evaluationPlanning : t.evaluationPreviewPlan}
      </button>
      <button data-evaluation-action="apply" disabled={!canApply || Boolean(busy)} onClick={() => void applyGeneratedTests()} type="button">
        <Save aria-hidden="true" size={15} />
        {busy === 'apply' ? t.evaluationApplying : generatedTestsApplied ? t.evaluationCasesAlreadyApplied : t.evaluationApplyCases}
      </button>
      <button data-evaluation-action="run" disabled={!canRun || Boolean(busy)} onClick={() => void runEvaluation()} type="button">
        <Play aria-hidden="true" size={15} />
        {busy === 'run' ? t.evaluationRunning : t.evaluationRun}
      </button>
    </div>

    {busy === 'catalog' && <p className="muted">{t.evaluationCatalogLoading}</p>}
    {error && <p className="evaluation-error">{error}</p>}
    {!plan && busy !== 'catalog' && <p className="evaluation-empty">{t.evaluationPreviewHint}</p>}

    {plan && <section className="evaluation-plan" data-evaluation-plan={plan.eligibility}>
      <div className="evaluation-section-heading">
        <div><strong>{t.evaluationPlanTitle}</strong><small>{plan.profile.level} · {t.evaluationEnvironmentName(plan.environment.id)}</small></div>
        <span className={plan.eligibility}>{t.evaluationEligibility(plan.eligibility)}</span>
      </div>
      {!planIsCurrent && <p className="evaluation-warning">{t.evaluationPlanStale}</p>}
      <dl className="evaluation-plan-summary">
        <div><dt>{t.evaluationGeneratedCases}</dt><dd>{plan.generated_tests.length}</dd></div>
        <div><dt>{t.evaluationCapabilityCoverage}</dt><dd>{plan.covered_capability_ids.length}/{plan.required_capability_ids.length}</dd></div>
        <div><dt>{t.evaluationClaimCeiling}</dt><dd>{plan.claim_ceiling}</dd></div>
      </dl>
      {runtimeNeedsAppliedCases && <p className="evaluation-warning">{t.evaluationApplyBeforeRun}</p>}
      {plan.blockers.length > 0 && <div className="evaluation-message-list blocked"><b>{t.evaluationBlockers}</b><ul>{plan.blockers.map(item => <li key={item}>{item}</li>)}</ul></div>}
      {plan.warnings.length > 0 && <div className="evaluation-message-list"><b>{t.evaluationWarnings}</b><ul>{plan.warnings.map(item => <li key={item}>{item}</li>)}</ul></div>}
      <div className="evaluation-case-list">
        {plan.cases.map(item => <article key={item.id} data-evaluation-case={item.family}>
          <div><strong>{item.title}</strong><span>{item.capability_kind} · {item.family}</span></div>
          <small>{item.capability_ids.join(', ') || t.evaluationCompatibilityCase}</small>
          <p>{item.required_signals.join(' · ')}</p>
          {item.blockers.length > 0 && <em>{item.blockers.join('; ')}</em>}
        </article>)}
      </div>
    </section>}

    {visibleRun && <section className="evaluation-result" data-evaluation-run={visibleRun.outcome}>
      <div className="evaluation-section-heading">
        <div><strong>{t.evaluationLatestResult}</strong><small>{dateLabel(visibleRun.updated_at, locale)}</small></div>
        <span className={visibleRun.passed ? 'ready' : 'blocked'}>{visibleRun.achieved_status}</span>
      </div>
      <dl className="evaluation-plan-summary">
        <div><dt>{t.evaluationOutcome}</dt><dd>{visibleRun.outcome}</dd></div>
        <div><dt>{t.evaluationExecutedCases}</dt><dd>{visibleRun.executed_test_ids.length}</dd></div>
        <div><dt>{t.evaluationProfileLabel}</dt><dd>{visibleRun.profile_level}</dd></div>
      </dl>
      {visibleRun.blockers.length > 0 && <div className="evaluation-message-list blocked"><b>{t.evaluationBlockers}</b><ul>{visibleRun.blockers.map(item => <li key={item}>{item}</li>)}</ul></div>}
      <div className="evaluation-claims">
        <div><b>{t.evaluationVerifiedClaims}</b>{visibleRun.verified_claims.length ? <ul>{visibleRun.verified_claims.map(item => <li key={item}>{item}</li>)}</ul> : <p>{t.evaluationNoVerifiedClaims}</p>}</div>
        <div><b>{t.evaluationExcludedClaims}</b>{visibleRun.excluded_claims.length ? <ul>{visibleRun.excluded_claims.map(item => <li key={item}>{item}</li>)}</ul> : <p>{t.evaluationNoExcludedClaims}</p>}</div>
      </div>
      {visibleRun.capability_results.length > 0 && <details style={{ maxWidth: '100%', minWidth: 0, overflow: 'hidden' }}><summary>{t.evaluationCaseEvidence}</summary><pre style={{ boxSizing: 'border-box', display: 'block', marginLeft: 0, marginRight: 0, minWidth: 0, overflow: 'auto', padding: 0, width: 'calc(100% - 20px)' }}>{JSON.stringify(visibleRun.capability_results, null, 2)}</pre></details>}
      <details style={{ maxWidth: '100%', minWidth: 0, overflow: 'hidden' }}><summary>{t.engineeringDetails}</summary><pre style={{ boxSizing: 'border-box', display: 'block', marginLeft: 0, marginRight: 0, minWidth: 0, overflow: 'auto', padding: 0, width: 'calc(100% - 20px)' }}>{JSON.stringify(visibleRun.report, null, 2)}</pre></details>
    </section>}

    <section className="evaluation-history">
      <div className="evaluation-section-heading">
        <div><strong>{t.evaluationHistory}</strong><small>{t.evaluationHistoryCount(runs.length)}</small></div>
        <button
          aria-label={t.evaluationRefreshHistory}
          className="evaluation-icon-button"
          disabled={Boolean(busy)}
          onClick={() => void refreshHistory()}
          title={t.evaluationRefreshHistory}
          type="button"
        ><RefreshCw aria-hidden="true" size={14} /></button>
      </div>
      {runs.length > 0 ? <div className="evaluation-history-list">{runs.map(item => <button key={item.id} onClick={() => setLatestRun(item)} type="button">
        <span><b>{item.profile_level}</b>{t.evaluationEnvironmentName(item.environment_id)}</span>
        <span className={item.passed ? 'ready' : 'blocked'}>{item.achieved_status}</span>
        <small>{dateLabel(item.updated_at, locale)}</small>
      </button>)}</div> : <p className="muted">{t.evaluationHistoryEmpty}</p>}
    </section>
  </section>
}
