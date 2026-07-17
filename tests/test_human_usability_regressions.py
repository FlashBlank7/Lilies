from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_runtime_status_tracks_current_patch_line() -> None:
    source = (ROOT / "platform/frontend/lib/runtime-status.ts").read_text(encoding="utf-8")

    assert "expectedRuntimeProductPhase = 'v0.4.x'" in source
    assert "expectedRuntimeVersionPattern = /^v0\\.4\\.\\d+$/" in source
    assert "runtime.product_phase !== expectedRuntimeProductPhase" in source
    assert "expectedRuntimeVersionPattern.test(runtime.version)" in source
    assert "expectedRuntimeVersion = 'v0.3.6'" not in source


def test_home_success_feedback_is_not_rendered_as_an_error() -> None:
    source = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")

    assert "const [notice, setNotice] = useState('')" in source
    assert 'className="success-banner" role="status"' in source
    assert 'className="error-banner" role="alert"' in source
    assert "showNotice(result.status === 'ready'" in source
    assert "setError(result.status === 'ready'" not in source


def test_customer_runtime_exposes_a_stable_loading_state() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(encoding="utf-8")
    platform_types = (ROOT / "platform/frontend/lib/platform.ts").read_text(encoding="utf-8")

    assert "data-runtime-loading={loading ? 'true' : 'false'}" in source
    assert "const loadGenerationRef = useRef(0)" in source
    assert "if (generation !== loadGenerationRef.current) return" in source
    assert "if (generation === loadGenerationRef.current) setLoading(false)" in source
    assert "data-runtime-ready={runtimeReady ? 'true' : 'false'}" in source
    assert "data-runtime-run-id={run?.id || ''}" in source
    assert "displaySnapshot?.capability_build_contract?.business_goal" in source
    assert 'data-runtime-purpose="true"' in source
    assert "capability_build_contract?:" in platform_types


def test_customer_runtime_guides_invalid_input_and_guards_retry() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "platform/frontend/app/runtime/[id]/runtime.module.css").read_text(
        encoding="utf-8"
    )

    assert "class RuntimeInputError extends Error" in source
    assert "const startRunLockedRef = useRef(false)" in source
    assert "if (startRunLockedRef.current) return" in source
    assert "startRunLockedRef.current = true" in source
    assert "startRunLockedRef.current = false" in source
    assert "data-runtime-invalid={invalidFieldName === field.name ? 'true' : 'false'}" in source
    assert "aria-invalid={invalidFieldName === field.name ? 'true' : undefined}" in source
    assert "runtimeInputRefs.current.get(caught.fieldName)" in source
    assert "input?.focus()" in source
    assert "if (invalidFieldName === name)" in source
    assert "function customerErrorMessage(error: unknown)" in source
    assert "if (error instanceof Error) return error.message" in source
    assert "if (run.status === 'failed') return ''" in source
    assert "function resultAvailabilityLabel(" in source
    assert "run?.status !== 'failed'" in source
    assert "if (run?.status === 'failed') return '未生成'" in source
    assert 'data-customer-runtime-action="retry"' in source
    assert "disabled={starting || running}" in source
    assert "正在重新运行" in source
    assert '[data-runtime-invalid="true"]' in styles
    assert ".recovery button:disabled" in styles


def test_customer_runtime_renders_serialized_structured_results_for_people() -> None:
    source = (ROOT / "platform/frontend/app/runtime/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "function serializedStructure(value: string)" in source
    assert "function escapeJsonStringControlCharacters(value: string)" in source
    assert "const parsed: unknown = JSON.parse(trimmed)" in source
    assert "JSON.parse(escapeJsonStringControlCharacters(trimmed))" in source
    assert "classification: '分类结果'" in source
    assert "urgency_level: '紧急程度'" in source
    assert "next_step: '下一步'" in source
    assert "structured ? resultText(structured, depth) : value" in source


def test_application_description_uses_the_business_goal_instead_of_raw_markdown() -> None:
    source = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")

    assert "function deriveApplicationDescription(" in source
    assert "contract?.business_goal?.trim()" in source
    assert "description: deriveApplicationDescription(requirement, capabilityBuildContract)" in source
    assert "description: requirement.slice(0, 180)" not in source


def test_engineer_workflow_overview_uses_plain_business_copy() -> None:
    source = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "function readableWorkflowPurpose(" in source
    assert "snapshot.capability_build_contract?.business_goal?.trim()" in source
    assert 'data-workflow-readable-purpose="true"' in source
    assert "detail: safeText(node.description, t.nodeInspectorNoDescription)" in source
    assert "next.join(', ')" not in source[source.index("const workflowStepSummaryItems") : source.index("const selectedBlockDefinition")]


def test_builder_events_publish_persisted_live_progress() -> None:
    backend = (ROOT / "platform/backend/src/agent_platform/builder.py").read_text(encoding="utf-8")
    frontend = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )

    persist = "await self.workflow_store.update_build(build_id, team_state=state)"
    emit = 'await self._emit(build_id, "build.operation"'
    assert backend.index(persist, backend.index("value = await self._execute(")) < backend.index(emit)
    assert '"progress": self._team_progress(state)' in backend
    assert "api<Build>(`/api/v1/builds/${buildId}`)" in frontend
    assert "scheduleBuildRefresh(buildId)" in frontend


def test_acceptance_repair_applies_and_reruns_without_manual_navigation() -> None:
    source = (ROOT / "platform/frontend/app/applications/[id]/page.tsx").read_text(
        encoding="utf-8"
    )
    copy = (ROOT / "platform/frontend/lib/i18n.ts").read_text(encoding="utf-8")

    start = source.index("async function applyAcceptanceRepair()")
    end = source.index("async function reconcileIncomingEdges", start)
    apply_block = source[start:end]
    assert "setStudioTab('test')" in apply_block
    assert "await runTests()" in apply_block
    assert "setStudioTab('edit')" not in apply_block
    assert "acceptanceRepairRef.current?.scrollIntoView" in source
    assert "block: 'start'" in source
    assert "ref={acceptanceRepairRef} tabIndex={-1}" in source
    repair_panel = source[source.index('data-acceptance-repair="failed-gate-preview"') :]
    assert repair_panel.index('className="acceptance-repair-actions"') < repair_panel.index(
        'className="acceptance-repair-body"'
    )
    assert "应用修复并重新验收" in copy


def test_markdown_preserves_workflow_identifiers_with_underscores() -> None:
    source = (ROOT / "platform/frontend/lib/markdown.tsx").read_text(encoding="utf-8")

    assert "marker === '_'" in source
    assert "/[\\p{L}\\p{N}]/u.test(previous)" in source
    assert "/[\\p{L}\\p{N}]/u.test(next)" in source
    assert "buffer += marker" in source
