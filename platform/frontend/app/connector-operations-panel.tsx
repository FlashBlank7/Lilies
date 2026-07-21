'use client'

import {
  AlertTriangle,
  Braces,
  CheckCircle2,
  FileJson,
  KeyRound,
  Octagon,
  Play,
  PlugZap,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TestTube2,
  Upload,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  api,
  idempotency,
  isAuthError,
  type ConnectorBinding,
  type ConnectorContractRun,
  type ConnectorExecutionDetail,
  type ConnectorExecutionPage,
  type ConnectorExercise,
  type ConnectorManifest,
  type OpenAPIConnectorGeneration,
  type ConnectorPolicy,
  type ConnectorReceipt,
} from '@/lib/platform'
import styles from './connector-operations.module.css'

type Props = {
  applicationId: string
  locale: 'zh' | 'en'
  onAuthRequired?: () => void
}

type BindingDraft = {
  connectorId: string
  version: string
  tenantId: string
  externalTenantId: string
  profileId: string
  secretRef: string
  externalSubject: string
  actorId: string
  roles: string
}

type ExecutionDraft = {
  tenantId: string
  operationId: string
  payload: string
  authorizationId: string
  idempotencyKey: string
  dryRun: boolean
}

type GenerationDraft = {
  connectorId: string
  version: string
  domain: string
  profileId: string
  baseUrl: string
  allowedHosts: string
  sourceMode: 'inline' | 'url'
  document: string
  documentUrl: string
  allowedDocumentHosts: string
}

const emptyBinding: BindingDraft = {
  connectorId: '',
  version: '1',
  tenantId: '',
  externalTenantId: '',
  profileId: '',
  secretRef: '',
  externalSubject: '',
  actorId: '',
  roles: 'operator',
}

const emptyExecution: ExecutionDraft = {
  tenantId: '',
  operationId: '',
  payload: '{\n  "case_id": "case-001"\n}',
  authorizationId: '',
  idempotencyKey: '',
  dryRun: true,
}

const emptyGeneration: GenerationDraft = {
  connectorId: '',
  version: '1',
  domain: '',
  profileId: 'generated-test',
  baseUrl: '',
  allowedHosts: '',
  sourceMode: 'inline',
  document: '',
  documentUrl: '',
  allowedDocumentHosts: '',
}

function splitValues(value: string) {
  return value.split(/[\n,]/).map(item => item.trim()).filter(Boolean)
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function shortId(value: string) {
  return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-5)}` : value
}

export function ConnectorOperationsPanel({ applicationId, locale, onAuthRequired }: Props) {
  const zh = locale === 'zh'
  const onAuthRequiredRef = useRef(onAuthRequired)
  const [manifests, setManifests] = useState<ConnectorManifest[]>([])
  const [generations, setGenerations] = useState<OpenAPIConnectorGeneration[]>([])
  const [bindings, setBindings] = useState<ConnectorBinding[]>([])
  const [policies, setPolicies] = useState<ConnectorPolicy[]>([])
  const [executions, setExecutions] = useState<ConnectorExecutionPage | null>(null)
  const [exercises, setExercises] = useState<ConnectorExercise[]>([])
  const [events, setEvents] = useState<Array<Record<string, unknown>>>([])
  const [selectedExecutionId, setSelectedExecutionId] = useState('')
  const [manifestText, setManifestText] = useState('')
  const [generationDraft, setGenerationDraft] = useState<GenerationDraft>(emptyGeneration)
  const [selectedGenerationId, setSelectedGenerationId] = useState('')
  const [contractRun, setContractRun] = useState<ConnectorContractRun | null>(null)
  const [allowMutatingContracts, setAllowMutatingContracts] = useState(false)
  const [bindingDraft, setBindingDraft] = useState<BindingDraft>(emptyBinding)
  const [executionDraft, setExecutionDraft] = useState<ExecutionDraft>(emptyExecution)
  const [policyReason, setPolicyReason] = useState('Operator-controlled integration boundary')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => { onAuthRequiredRef.current = onAuthRequired }, [onAuthRequired])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const applicationScope = `application_id=${encodeURIComponent(applicationId)}`
      const [nextManifests, nextGenerations, nextBindings, nextPolicies, nextExecutions, nextExercises] = await Promise.all([
        api<ConnectorManifest[]>('/api/v1/connectors/manifests'),
        api<OpenAPIConnectorGeneration[]>('/api/v1/connectors/generations'),
        api<ConnectorBinding[]>(`/api/v1/connectors/bindings?${applicationScope}`),
        api<ConnectorPolicy[]>(`/api/v1/connectors/policies?${applicationScope}`),
        api<ConnectorExecutionPage>(`/api/v1/connectors/executions?${applicationScope}&limit=50`),
        api<ConnectorExercise[]>(`/api/v1/connectors/exercises?${applicationScope}`),
      ])
      setManifests(nextManifests)
      setGenerations(nextGenerations)
      setBindings(nextBindings)
      setPolicies(nextPolicies)
      setExecutions(nextExecutions)
      setExercises(nextExercises)
      setSelectedGenerationId(current => current || nextGenerations[0]?.id || '')
      const firstManifest = nextManifests[0]
      const firstBinding = nextBindings[0]
      setBindingDraft(current => ({
        ...current,
        connectorId: current.connectorId || firstManifest?.connector_id || '',
        version: current.connectorId ? current.version : String(firstManifest?.version || 1),
        profileId: current.profileId || firstManifest?.deployment_profiles[0]?.id || '',
        tenantId: current.tenantId || firstBinding?.tenant_id || '',
      }))
      setExecutionDraft(current => ({
        ...current,
        tenantId: current.tenantId || firstBinding?.tenant_id || '',
        operationId: current.operationId || firstManifest?.operations[0]?.id || '',
        idempotencyKey: current.idempotencyKey || idempotency(),
      }))
    } catch (caught) {
      if (isAuthError(caught)) onAuthRequiredRef.current?.()
      else setError(String(caught))
    } finally {
      setLoading(false)
    }
  }, [applicationId])

  useEffect(() => { void refresh() }, [refresh])

  const selectedManifest = useMemo(() => manifests.find(item => (
    item.connector_id === bindingDraft.connectorId && item.version === Number(bindingDraft.version)
  )) || manifests[0], [bindingDraft.connectorId, bindingDraft.version, manifests])
  const selectedGeneration = generations.find(item => item.id === selectedGenerationId)
    || generations[0]
  const selectedBinding = bindings.find(item => item.tenant_id === executionDraft.tenantId)
    || bindings.find(item => item.application_ids.includes(applicationId))
    || bindings[0]
  const selectedPolicy = policies.find(item => item.tenant_id === bindingDraft.tenantId && (
    !bindingDraft.connectorId || item.connector_id === bindingDraft.connectorId
  ))

  async function generateFromOpenAPI() {
    setBusy('generation')
    setError('')
    setNotice('')
    setContractRun(null)
    try {
      const body = {
        connector_id: generationDraft.connectorId,
        version: Number(generationDraft.version),
        domain: generationDraft.domain,
        document: generationDraft.sourceMode === 'inline' ? generationDraft.document : '',
        document_url: generationDraft.sourceMode === 'url' ? generationDraft.documentUrl : '',
        allowed_document_hosts: splitValues(generationDraft.allowedDocumentHosts),
        deployment: {
          profile_id: generationDraft.profileId,
          environment: 'test',
          base_url: generationDraft.baseUrl,
          allowed_hosts: splitValues(generationDraft.allowedHosts),
          available: true,
          claim_ceiling: 'H3',
        },
      }
      const generated = await api<OpenAPIConnectorGeneration>('/api/v1/connectors/generations', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      setSelectedGenerationId(generated.id)
      setNotice(zh ? 'OpenAPI 已解析，映射与契约用例已生成。' : 'OpenAPI parsed; mappings and contract cases generated.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function runGeneratedContracts() {
    if (!selectedGeneration) return
    setBusy('contract-run')
    setError('')
    try {
      const run = await api<ConnectorContractRun>(`/api/v1/connectors/generations/${selectedGeneration.id}/contract-runs`, {
        method: 'POST',
        body: JSON.stringify({ allow_mutating_operations: allowMutatingContracts }),
      })
      setContractRun(run)
      setNotice(run.status === 'passed'
        ? (zh ? '自动契约全部通过，可以登记。' : 'Generated contracts passed; registration is available.')
        : (zh ? '契约未通过，失败原因已保留。' : 'Contracts did not pass; failure evidence is retained.'))
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function registerGenerated() {
    if (!selectedGeneration) return
    setBusy('generation-register')
    setError('')
    try {
      await api(`/api/v1/connectors/generations/${selectedGeneration.id}/register`, { method: 'POST' })
      setNotice(zh ? '已验证的 Connector 版本已登记。' : 'Verified Connector version registered.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function registerManifest() {
    setBusy('manifest')
    setError('')
    setNotice('')
    try {
      const body = JSON.parse(manifestText) as Record<string, unknown>
      await api('/api/v1/connectors/manifests', { method: 'POST', body: JSON.stringify(body) })
      setManifestText('')
      setNotice(zh ? 'Connector 版本已登记。' : 'Connector version registered.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function saveBinding() {
    if (!selectedManifest) return
    const existing = bindings.find(item => (
      item.connector_id === bindingDraft.connectorId
      && item.connector_version === Number(bindingDraft.version)
      && item.tenant_id === bindingDraft.tenantId
    ))
    setBusy('binding')
    setError('')
    try {
      await api('/api/v1/connectors/bindings', {
        method: 'PUT',
        body: JSON.stringify({
          expected_revision: existing?.revision || 0,
          binding: {
            ...(existing || {}),
            connector_id: bindingDraft.connectorId,
            connector_version: Number(bindingDraft.version),
            tenant_id: bindingDraft.tenantId,
            external_tenant_id: bindingDraft.externalTenantId || existing?.external_tenant_id,
            profile_id: bindingDraft.profileId,
            secret_ref: bindingDraft.secretRef || existing?.secret_ref,
            application_ids: Array.from(new Set([...(existing?.application_ids || []), applicationId])),
            allowed_operations: selectedManifest.operations.map(item => item.id),
            subjects: [{
              external_subject: bindingDraft.externalSubject || existing?.subjects[0]?.external_subject,
              actor_id: bindingDraft.actorId || existing?.subjects[0]?.actor_id,
              roles: splitValues(bindingDraft.roles || existing?.subjects[0]?.roles.join(',') || 'operator'),
            }],
            enabled: true,
          },
        }),
      })
      setNotice(zh ? '测试租户绑定已保存。' : 'Test tenant binding saved.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function savePolicy() {
    if (!selectedManifest || !bindingDraft.tenantId) return
    const existing = selectedPolicy
    setBusy('policy')
    setError('')
    try {
      await api('/api/v1/connectors/policies', {
        method: 'PUT',
        body: JSON.stringify({
          expected_revision: existing?.revision || 0,
          policy: {
            ...(existing || {}),
            connector_id: selectedManifest.connector_id,
            connector_version: selectedManifest.version,
            tenant_id: bindingDraft.tenantId,
            domain: selectedManifest.domain,
            allowed_profiles: [bindingDraft.profileId || selectedManifest.deployment_profiles[0]?.id],
            allowed_operations: selectedManifest.operations.map(item => item.id),
            required_roles: splitValues(bindingDraft.roles || 'operator'),
            max_payload_bytes: existing?.max_payload_bytes || 100000,
            mutation_preauthorization_required: true,
            allow_dry_run: true,
            allow_compensation_during_stop: true,
          },
        }),
      })
      setNotice(zh ? '租户策略已保存。' : 'Tenant policy saved.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function execute() {
    if (!selectedManifest || !selectedBinding) return
    const subject = selectedBinding.subjects[0]
    setBusy('execute')
    setError('')
    try {
      const payload = JSON.parse(executionDraft.payload) as Record<string, unknown>
      const result = await api<{ receipt: ConnectorReceipt }>('/api/v1/connectors/executions', {
        method: 'POST',
        body: JSON.stringify({
          connector_id: selectedManifest.connector_id,
          connector_version: selectedManifest.version,
          tenant_id: selectedBinding.tenant_id,
          actor_id: subject.actor_id,
          actor_roles: subject.roles,
          profile_id: selectedBinding.profile_id,
          operation_id: executionDraft.operationId,
          payload,
          idempotency_key: executionDraft.idempotencyKey,
          authorization_id: executionDraft.authorizationId,
          dry_run: executionDraft.dryRun,
          application_id: applicationId,
          run_id: '',
        }),
      })
      setNotice(`${zh ? '执行回执' : 'Execution receipt'}: ${shortId(result.receipt.execution_id)} · ${result.receipt.status}`)
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function authorize(receipt: ConnectorReceipt) {
    setBusy(`authorize:${receipt.execution_id}`)
    setError('')
    try {
      const detail = await api<ConnectorExecutionDetail>(`/api/v1/connectors/executions/${receipt.execution_id}`)
      const grant = await api<{ id: string }>('/api/v1/connectors/authorizations', {
        method: 'POST',
        body: JSON.stringify({
          connector_id: receipt.connector_id,
          connector_version: receipt.connector_version,
          tenant_id: receipt.tenant_id,
          actor_id: detail.actor_id,
          profile_id: receipt.profile_id,
          operation_id: receipt.operation_id,
          payload: detail.request_payload,
          expires_in_seconds: 300,
          max_uses: 1,
        }),
      })
      setExecutionDraft({
        tenantId: receipt.tenant_id,
        operationId: receipt.operation_id,
        payload: JSON.stringify(detail.request_payload, null, 2),
        authorizationId: grant.id,
        idempotencyKey: detail.idempotency_key,
        dryRun: false,
      })
      setNotice(zh ? '精确载荷已授权；请核对后执行。' : 'Exact payload authorized; review before execution.')
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function setEmergency(policy: ConnectorPolicy, enabled: boolean) {
    if (!policyReason.trim()) return
    setBusy(`stop:${policy.tenant_id}`)
    setError('')
    try {
      await api(`/api/v1/connectors/policies/${policy.connector_id}/${policy.connector_version}/${policy.tenant_id}/emergency-stop`, {
        method: 'POST',
        body: JSON.stringify({
          enabled,
          reason: policyReason,
          expected_revision: policy.revision,
        }),
      })
      setNotice(enabled ? (zh ? '紧急停止已生效。' : 'Emergency stop enabled.') : (zh ? '紧急停止已解除。' : 'Emergency stop cleared.'))
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function compensate(receipt: ConnectorReceipt) {
    if (!window.confirm(zh ? '确认执行显式补偿？该操作会写回客户测试系统。' : 'Run the explicit compensation against the customer test system?')) return
    setBusy(`compensate:${receipt.execution_id}`)
    setError('')
    try {
      const detail = await api<ConnectorExecutionDetail>(`/api/v1/connectors/executions/${receipt.execution_id}`)
      const compensationPayload = record(detail.response.compensation_payload)
      const manifest = manifests.find(item => item.connector_id === receipt.connector_id && item.version === receipt.connector_version)
      const operation = manifest?.operations.find(item => item.id === receipt.operation_id)
      if (!operation?.compensation_operation_id || !Object.keys(compensationPayload).length) throw new Error('No compensation contract or payload')
      const grant = await api<{ id: string }>('/api/v1/connectors/authorizations', {
        method: 'POST',
        body: JSON.stringify({
          connector_id: receipt.connector_id,
          connector_version: receipt.connector_version,
          tenant_id: receipt.tenant_id,
          actor_id: detail.actor_id,
          profile_id: receipt.profile_id,
          operation_id: operation.compensation_operation_id,
          payload: compensationPayload,
          expires_in_seconds: 300,
          max_uses: 1,
        }),
      })
      await api(`/api/v1/connectors/executions/${receipt.execution_id}/compensate`, {
        method: 'POST',
        body: JSON.stringify({
          actor_id: detail.actor_id,
          actor_roles: detail.actor_roles,
          authorization_id: grant.id,
          idempotency_key: idempotency(),
        }),
      })
      setNotice(zh ? '补偿已完成并保留独立回执。' : 'Compensation completed with a separate receipt.')
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function inspectEvents(executionId: string) {
    setSelectedExecutionId(executionId)
    setBusy(`events:${executionId}`)
    try {
      setEvents(await api<Array<Record<string, unknown>>>(`/api/v1/connectors/executions/${executionId}/events`))
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  async function runExercise(policy: ConnectorPolicy) {
    setBusy(`exercise:${policy.tenant_id}`)
    try {
      await api('/api/v1/connectors/exercises', {
        method: 'POST',
        body: JSON.stringify({
          connector_id: policy.connector_id,
          connector_version: policy.connector_version,
          tenant_id: policy.tenant_id,
          kind: 'emergency_stop',
        }),
      })
      await refresh()
    } catch (caught) {
      setError(String(caught))
    } finally {
      setBusy('')
    }
  }

  const connectorState = loading
    ? 'loading'
    : manifests.length === 0
      ? 'not-configured'
      : bindings.length === 0
        ? 'available-unbound'
        : 'bound'

  return <div className={styles.panel} data-connector-state={connectorState} data-engineer-connector-workspace="true">
    <header className={styles.heading}>
      <div><span><PlugZap size={15} />Connector SDK</span><h2>{zh ? '客户系统集成' : 'Customer integrations'}</h2></div>
      <button className={styles.iconButton} onClick={() => void refresh()} disabled={loading} aria-label={zh ? '刷新集成数据' : 'Refresh integrations'} title={zh ? '刷新' : 'Refresh'}><RefreshCw className={loading ? styles.spin : ''} size={16} /></button>
    </header>

    <div className={styles.metrics}>
      <span><b>{generations.length}</b>{zh ? '自动生成' : 'generated'}</span>
      <span><b>{manifests.length}</b>{zh ? '已登记' : 'registered'}</span>
      <span><b>{bindings.length}</b>{zh ? '租户' : 'tenants'}</span>
    </div>
    {error && <div className={styles.error} role="alert"><AlertTriangle size={16} /><span>{error}</span></div>}
    {notice && <div className={styles.notice}><CheckCircle2 size={16} /><span>{notice}</span></div>}
    {!loading && manifests.length === 0 && <div className={styles.emptyState}>
      <PlugZap size={20} />
      <strong>{zh ? '尚未配置客户系统连接器' : 'No customer connector configured'}</strong>
      <span>{zh ? '导入客户系统的 OpenAPI 文档并运行自动契约。' : 'Import the customer OpenAPI document and run generated contracts.'}</span>
    </div>}

    <section className={styles.section} data-connector-section="openapi-generation" data-openapi-default-path="true">
      <header><Upload size={16} /><div><strong>{zh ? 'OpenAPI 自动接入' : 'OpenAPI import'}</strong><small>{zh ? '来源 → 映射 → 契约 → 登记' : 'source → mapping → contract → registration'}</small></div></header>
      <div className={styles.formGrid}>
        <label><span>Connector ID</span><input data-openapi-field="connector-id" value={generationDraft.connectorId} onChange={event => setGenerationDraft(current => ({ ...current, connectorId: event.target.value }))} /></label>
        <label><span>{zh ? '领域' : 'Domain'}</span><input data-openapi-field="domain" value={generationDraft.domain} onChange={event => setGenerationDraft(current => ({ ...current, domain: event.target.value }))} /></label>
        <label><span>{zh ? '版本' : 'Version'}</span><input type="number" min="1" value={generationDraft.version} onChange={event => setGenerationDraft(current => ({ ...current, version: event.target.value }))} /></label>
        <label><span>Profile</span><input value={generationDraft.profileId} onChange={event => setGenerationDraft(current => ({ ...current, profileId: event.target.value }))} /></label>
        <label className={styles.wide}><span>Base URL</span><input data-openapi-field="base-url" value={generationDraft.baseUrl} onChange={event => setGenerationDraft(current => ({ ...current, baseUrl: event.target.value }))} placeholder="https://customer-system.example/api/" /></label>
        <label className={styles.wide}><span>{zh ? '允许的运行主机' : 'Allowed runtime hosts'}</span><input data-openapi-field="allowed-hosts" value={generationDraft.allowedHosts} onChange={event => setGenerationDraft(current => ({ ...current, allowedHosts: event.target.value }))} placeholder="customer-system.example" /></label>
        <label><span>{zh ? '文档来源' : 'Document source'}</span><select value={generationDraft.sourceMode} onChange={event => setGenerationDraft(current => ({ ...current, sourceMode: event.target.value as 'inline' | 'url' }))}><option value="inline">{zh ? '粘贴 JSON / YAML' : 'Paste JSON / YAML'}</option><option value="url">URL</option></select></label>
        {generationDraft.sourceMode === 'url' && <label><span>{zh ? '允许的文档主机' : 'Allowed document hosts'}</span><input value={generationDraft.allowedDocumentHosts} onChange={event => setGenerationDraft(current => ({ ...current, allowedDocumentHosts: event.target.value }))} /></label>}
        {generationDraft.sourceMode === 'inline'
          ? <label className={styles.wide}><span>OpenAPI JSON / YAML</span><textarea data-openapi-field="document" value={generationDraft.document} onChange={event => setGenerationDraft(current => ({ ...current, document: event.target.value }))} /></label>
          : <label className={styles.wide}><span>OpenAPI URL</span><input value={generationDraft.documentUrl} onChange={event => setGenerationDraft(current => ({ ...current, documentUrl: event.target.value }))} placeholder="https://customer-system.example/openapi.json" /></label>}
      </div>
      <button data-connector-action="generate-openapi" onClick={() => void generateFromOpenAPI()} disabled={!generationDraft.connectorId || !generationDraft.domain || !generationDraft.baseUrl || !(generationDraft.document || generationDraft.documentUrl) || busy === 'generation'}><Braces size={15} />{busy === 'generation' ? (zh ? '生成中' : 'Generating') : (zh ? '生成 Connector 与契约' : 'Generate Connector and contracts')}</button>

      {generations.length > 0 && <div className={styles.contractList} data-openapi-generations="true">{generations.map(item => <button key={item.id} onClick={() => { setSelectedGenerationId(item.id); setContractRun(null) }} className={selectedGeneration?.id === item.id ? styles.selected : ''}><strong>{item.provenance.title}</strong><span>{item.connector_id}@{item.version} · {item.status}{item.evidence_stale ? ` · ${zh ? '证据已过期' : 'stale evidence'}` : ''}</span><small>{item.generated_operation_count}/{item.discovered_operation_count} ops · {item.mapped_field_count}/{item.total_field_count} fields · {(item.parse_ms + item.generate_ms).toFixed(1)} ms</small></button>)}</div>}

      {selectedGeneration && <div className={styles.generationReview} data-openapi-generation-review={selectedGeneration.status}>
        <div className={styles.operationMap}>{selectedGeneration.manifest.operations.map(operation => <div key={operation.id}><span><strong>{operation.title}</strong><small>{operation.method} {operation.path}</small></span><code>{operation.parameters?.length || 0} params{operation.request_body ? ' + body' : ''}</code></div>)}</div>
        {selectedGeneration.gaps.length > 0 && <div className={styles.gapList}>{selectedGeneration.gaps.map(gap => <div key={`${gap.code}:${gap.location}`}><AlertTriangle size={14} /><span><strong>{gap.code} · {gap.capability}</strong><small>{gap.message}</small></span></div>)}</div>}
        <div className={styles.actions}>
          <label className={styles.inlineCheck}><input type="checkbox" checked={allowMutatingContracts} onChange={event => setAllowMutatingContracts(event.target.checked)} /><span>{zh ? '允许测试环境写契约' : 'Allow test-environment mutation contracts'}</span></label>
          <button data-connector-action="run-generated-contracts" onClick={() => void runGeneratedContracts()} disabled={selectedGeneration.evidence_stale || busy === 'contract-run'}><TestTube2 size={15} />{busy === 'contract-run' ? (zh ? '测试中' : 'Testing') : (zh ? '运行自动契约' : 'Run generated contracts')}</button>
          <button className={styles.secondary} data-connector-action="register-generated" onClick={() => void registerGenerated()} disabled={contractRun?.status !== 'passed' || busy === 'generation-register'}><ShieldCheck size={15} />{zh ? '登记已验证版本' : 'Register verified version'}</button>
        </div>
        {contractRun && <div className={styles.contractResults} data-contract-run-status={contractRun.status}><header><strong>{contractRun.status}</strong><span>{contractRun.passed} {zh ? '通过' : 'passed'} · {contractRun.failed} {zh ? '失败' : 'failed'} · {contractRun.blocked_by_environment} {zh ? '环境受限' : 'environment blocked'} · {contractRun.test_ms.toFixed(1)} ms</span></header>{contractRun.results.map(result => <div key={result.case.id} data-case-status={result.status}><span><strong>{result.case.operation_id} · {result.case.kind}</strong><small>{result.case.expected}</small><small>{result.actual}</small></span><b>{result.status}</b></div>)}</div>}
      </div>}
    </section>

    <section className={styles.section} data-connector-section="contract">
      <header><FileJson size={16} /><div><strong>{zh ? '版本化合同' : 'Versioned contracts'}</strong><small>H3 max · mock/test/live/private</small></div></header>
      <div className={styles.contractList}>{manifests.map(item => <button key={`${item.connector_id}:${item.version}`} onClick={() => setBindingDraft(current => ({ ...current, connectorId: item.connector_id, version: String(item.version), profileId: item.deployment_profiles[0]?.id || '' }))} className={selectedManifest === item ? styles.selected : ''}><strong>{item.title}</strong><span>{item.connector_id}@{item.version}</span><small>{item.operations.length} ops · {item.deployment_profiles.map(profile => `${profile.id}:${profile.environment}`).join(', ')}</small></button>)}</div>
      <details className={styles.details} data-manual-manifest-legacy="true"><summary>{zh ? '专家旧路径：手工登记 manifest JSON' : 'Expert legacy path: register manifest JSON'}</summary><textarea value={manifestText} onChange={event => setManifestText(event.target.value)} placeholder="Connector manifest JSON" /><button onClick={() => void registerManifest()} disabled={!manifestText.trim() || busy === 'manifest'}><FileJson size={15} />{busy === 'manifest' ? (zh ? '登记中' : 'Registering') : (zh ? '手工登记' : 'Register manually')}</button></details>
    </section>

    {manifests.length > 0 && <section className={styles.section} data-connector-section="tenant-policy">
      <header><KeyRound size={16} /><div><strong>{zh ? '测试租户与策略' : 'Test tenant and policy'}</strong><small>{selectedPolicy ? `r${selectedPolicy.revision}` : zh ? '尚未配置' : 'not configured'}</small></div></header>
      <div className={styles.formGrid}>
        <label><span>Connector</span><select value={bindingDraft.connectorId} onChange={event => setBindingDraft(current => ({ ...current, connectorId: event.target.value }))}>{manifests.map(item => <option key={`${item.connector_id}:${item.version}`} value={item.connector_id}>{item.connector_id}@{item.version}</option>)}</select></label>
        <label><span>{zh ? '租户 ID' : 'Tenant ID'}</span><input value={bindingDraft.tenantId} onChange={event => setBindingDraft(current => ({ ...current, tenantId: event.target.value }))} /></label>
        <label><span>{zh ? '外部租户' : 'External tenant'}</span><input value={bindingDraft.externalTenantId} onChange={event => setBindingDraft(current => ({ ...current, externalTenantId: event.target.value }))} /></label>
        <label><span>Profile</span><select value={bindingDraft.profileId} onChange={event => setBindingDraft(current => ({ ...current, profileId: event.target.value }))}>{selectedManifest?.deployment_profiles.map(item => <option key={item.id} value={item.id}>{item.id} · {item.environment}</option>)}</select></label>
        <label className={styles.wide}><span>Secret ref</span><input value={bindingDraft.secretRef} onChange={event => setBindingDraft(current => ({ ...current, secretRef: event.target.value }))} placeholder="secret://tenant/name" /></label>
        <label><span>{zh ? '外部主体' : 'External subject'}</span><input value={bindingDraft.externalSubject} onChange={event => setBindingDraft(current => ({ ...current, externalSubject: event.target.value }))} /></label>
        <label><span>Actor</span><input value={bindingDraft.actorId} onChange={event => setBindingDraft(current => ({ ...current, actorId: event.target.value }))} /></label>
        <label className={styles.wide}><span>Roles</span><input value={bindingDraft.roles} onChange={event => setBindingDraft(current => ({ ...current, roles: event.target.value }))} /></label>
      </div>
      <div className={styles.actions}><button onClick={() => void saveBinding()} disabled={!bindingDraft.tenantId || busy === 'binding'}><KeyRound size={15} />{zh ? '保存绑定' : 'Save binding'}</button><button className={styles.secondary} onClick={() => void savePolicy()} disabled={!bindingDraft.tenantId || busy === 'policy'}><ShieldCheck size={15} />{zh ? '保存策略' : 'Save policy'}</button></div>
      {selectedPolicy && <div className={styles.stopRow}><input value={policyReason} onChange={event => setPolicyReason(event.target.value)} aria-label={zh ? '策略变更原因' : 'Policy change reason'} /><button className={selectedPolicy.emergency_stop ? styles.resume : styles.stop} data-connector-action="emergency-stop" onClick={() => void setEmergency(selectedPolicy, !selectedPolicy.emergency_stop)} disabled={!policyReason.trim() || busy.startsWith('stop:')}><Octagon size={15} />{selectedPolicy.emergency_stop ? (zh ? '解除停止' : 'Clear stop') : (zh ? '紧急停止' : 'Emergency stop')}</button>{selectedPolicy.emergency_stop && <button className={styles.secondary} onClick={() => void runExercise(selectedPolicy)}><ShieldCheck size={15} />{zh ? '验证停止' : 'Verify stop'}</button>}</div>}
    </section>}

    {manifests.length > 0 && <section className={styles.section} data-connector-section="controlled-execution">
      <header><Play size={16} /><div><strong>{zh ? '受控执行' : 'Controlled execution'}</strong><small>{executionDraft.dryRun ? 'dry-run' : zh ? '已请求写入' : 'mutation requested'}</small></div></header>
      <div className={styles.formGrid}>
        <label><span>{zh ? '租户' : 'Tenant'}</span><select value={executionDraft.tenantId} onChange={event => setExecutionDraft(current => ({ ...current, tenantId: event.target.value }))}>{bindings.map(item => <option key={`${item.connector_id}:${item.tenant_id}`} value={item.tenant_id}>{item.tenant_id}</option>)}</select></label>
        <label><span>{zh ? '操作' : 'Operation'}</span><select value={executionDraft.operationId} onChange={event => setExecutionDraft(current => ({ ...current, operationId: event.target.value }))}>{selectedManifest?.operations.map(item => <option key={item.id} value={item.id}>{item.id} · {item.kind}</option>)}</select></label>
        <label className={styles.wide}><span>Payload JSON</span><textarea value={executionDraft.payload} onChange={event => setExecutionDraft(current => ({ ...current, payload: event.target.value }))} /></label>
        <label className={styles.wide}><span>Idempotency key</span><input value={executionDraft.idempotencyKey} onChange={event => setExecutionDraft(current => ({ ...current, idempotencyKey: event.target.value }))} /></label>
        <label className={styles.wide}><span>Authorization ID</span><input value={executionDraft.authorizationId} onChange={event => setExecutionDraft(current => ({ ...current, authorizationId: event.target.value }))} placeholder={zh ? 'dry-run 后生成' : 'created after dry-run'} /></label>
        <label className={styles.check}><input type="checkbox" checked={executionDraft.dryRun} onChange={event => setExecutionDraft(current => ({ ...current, dryRun: event.target.checked }))} /><span>Dry-run</span></label>
      </div>
      <button data-connector-action="execute" onClick={() => void execute()} disabled={!selectedBinding || !executionDraft.operationId || busy === 'execute'}><Play size={15} />{busy === 'execute' ? (zh ? '执行中' : 'Running') : executionDraft.dryRun ? (zh ? '预演' : 'Preview') : (zh ? '执行已授权操作' : 'Execute authorized action')}</button>
    </section>}

    <section className={styles.section} data-connector-section="receipts">
      <header><ShieldCheck size={16} /><div><strong>{zh ? '执行回执' : 'Execution receipts'}</strong><small>{executions?.claim_boundary}</small></div></header>
      <div className={styles.receipts}>{executions?.items.map(item => <article key={item.execution_id} data-connector-status={item.status}>
        <button className={styles.receiptMain} onClick={() => void inspectEvents(item.execution_id)}><span className={`${styles.status} ${styles[item.status]}`}>{item.status}</span><strong>{item.operation_id}</strong><code>{shortId(item.execution_id)}</code><small>{item.tenant_id} · {item.side_effect_state}{item.callback_status ? ` · ${item.callback_status}` : ''}</small></button>
        <div>{item.status === 'dry_run' && item.operation_kind !== 'read' && <button title={zh ? '为精确载荷创建短时授权' : 'Authorize exact payload'} aria-label={zh ? '授权精确载荷' : 'Authorize exact payload'} onClick={() => void authorize(item)}><KeyRound size={14} /></button>}{item.compensation_available && !item.compensation_execution_id && <button title={zh ? '执行补偿' : 'Compensate'} aria-label={zh ? '执行补偿' : 'Compensate'} onClick={() => void compensate(item)}><RotateCcw size={14} /></button>}</div>
      </article>)}</div>
      {!executions?.items.length && <p className={styles.empty}>{zh ? '尚无执行回执。' : 'No execution receipts.'}</p>}
      {selectedExecutionId && <div className={styles.eventList}><strong>{zh ? '审计事件' : 'Audit events'} · {shortId(selectedExecutionId)}</strong>{events.map((item, index) => <div key={`${String(item.sequence)}:${index}`}><code>{String(item.event_type || '')}</code><span>{String(item.created_at || '')}</span></div>)}</div>}
    </section>

    <section className={styles.section}>
      <header><ShieldCheck size={16} /><div><strong>{zh ? '演练证据' : 'Exercise evidence'}</strong><small>H3 controlled · H5 blocked</small></div></header>
      <div className={styles.exerciseList}>{exercises.map(item => <div key={item.id}><span>{item.kind}</span><strong>{item.status}</strong><code>{item.evidence_level}</code></div>)}</div>
    </section>
  </div>
}
