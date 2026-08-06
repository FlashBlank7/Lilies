from __future__ import annotations

from .blocks import BlockRegistry
from .capability_evidence import (
    EvidenceEnvironment,
    VerificationStatus,
    CapabilityEvidenceCreateRequest,
    EvidenceArtifact,
    ModuleCapabilityClaim,
    ModuleKnownBoundary,
    ModulePort,
    ReusableModuleContract,
)
from .template_models import Template, TemplateMeta
from .template_store import ModuleVersionRecord, TemplateStore


CODEX_MODULE_ID = "codex_like_workspace_agent_module"


def codex_reference_contract() -> ReusableModuleContract:
    capability_ids = [
        "F.plan_act_observe",
        "F.workspace_result",
        "G.permission_boundary",
        "G.loop_trace",
        "X.workspace",
        "X.model",
    ]
    return ReusableModuleContract(
        capability_ids=capability_ids,
        inputs=[
            ModulePort(
                name="task",
                value_type="string",
                description="Natural-language workspace task.",
            ),
            ModulePort(
                name="workspace_path",
                value_type="string",
                description="Workspace root available to registered tools.",
            ),
        ],
        outputs=[
            ModulePort(
                name="answer",
                value_type="string",
                description="Customer-readable result grounded in tool feedback.",
            )
        ],
        dependencies=[],
        required_envelope="E2",
        risk_level="high",
        known_boundaries=[
            ModuleKnownBoundary(
                id="provider_availability",
                title="Provider availability is external",
                description=(
                    "The module verifies local graph behavior only; it does not prove "
                    "that a configured model provider is reachable."
                ),
                effect="blocked_by_environment",
                capability_ids=["X.model"],
            ),
            ModuleKnownBoundary(
                id="workspace_scope",
                title="Workspace scope is configured",
                description=(
                    "The verified claim is limited to registered workspace tools and does "
                    "not imply access to arbitrary repositories or host paths."
                ),
                effect="requires_approval",
                capability_ids=["X.workspace", "G.permission_boundary"],
            ),
        ],
        claims=[
            ModuleCapabilityClaim(
                capability_id=capability_id,
                statement=f"The editable module carries {capability_id} in its local workflow graph.",
                requested_status=VerificationStatus.component_verified,
                claim_scope=(
                    "Deterministic local component behavior with registered blocks; "
                    "live provider and production reliability are excluded."
                ),
            )
            for capability_id in capability_ids
        ],
    )


def _candidate_template(
    blocks: BlockRegistry,
    *,
    version: int,
    contract: ReusableModuleContract,
) -> Template:
    return Template(
        meta=TemplateMeta(
            name=CODEX_MODULE_ID,
            title="Codex-like Workspace Agent",
            description=(
                "Editable plan-act-observe workspace module with permission, stop, "
                "tool-feedback, trace, and customer-result flow."
            ),
            category="agent_architecture",
            tags=["codex", "workspace", "tool_loop", "verified_module"],
            icon="blocks",
            expected_inputs={"task": "string", "workspace_path": "string"},
            expected_outputs={"answer": "string"},
            author="platform",
            version=version,
            min_blocks_required=[
                "start",
                "context_assembler",
                "permission_gate",
                "capability_registry",
                "loop",
                "answer",
            ],
            confidence=1.0,
            seed_template=True,
        ),
        workflow=blocks.expand_template(
            "codex_like_workspace_agent",
            prefix="codex_module",
        ),
        module_contract=contract,
    )


def ensure_codex_reference_module(
    store: TemplateStore,
    blocks: BlockRegistry,
) -> ModuleVersionRecord:
    contract = codex_reference_contract()
    version = 1
    if CODEX_MODULE_ID in store.names():
        latest = store.get_record(CODEX_MODULE_ID)
        candidate = _candidate_template(blocks, version=latest.state.version, contract=contract)
        if store.content_hash(candidate) == latest.state.content_hash:
            version = latest.state.version
        else:
            version = latest.state.version + 1

    if CODEX_MODULE_ID not in store.names() or version not in store.versions(CODEX_MODULE_ID):
        candidate = _candidate_template(blocks, version=version, contract=contract)
        store.register(
            CODEX_MODULE_ID,
            candidate.workflow,
            meta_overrides=candidate.meta.model_dump(mode="json"),
            module_contract=contract,
            source="system",
            persist=True,
            exact_version=version,
        )

    for capability_id in contract.capability_ids:
        try:
            store.add_evidence(
                CODEX_MODULE_ID,
                version,
                CapabilityEvidenceCreateRequest(
                    capability_id=capability_id,
                    claim=f"Local editable Codex module carries {capability_id}.",
                    claim_scope=(
                        "Deterministic local component behavior; live provider, arbitrary "
                        "workspace access, and production reliability are excluded."
                    ),
                    requested_status="component_verified",
                    environment=EvidenceEnvironment.sandbox,
                    artifacts=[
                        EvidenceArtifact(
                            category="implementation",
                            path="platform/backend/src/agent_platform/blocks.py",
                            locator="_codex_like_workspace_agent_template",
                            description="Executable editable workflow graph.",
                        ),
                        EvidenceArtifact(
                            category="test",
                            path="tests/test_workflow.py",
                            description="Deterministic workflow and template component tests.",
                        ),
                    ],
                ),
            )
        except ValueError:
            # Source/test artifacts may be absent in a packaged runtime. The module
            # remains visible as draft instead of blocking service startup or overclaiming.
            continue
    try:
        return store.verify(CODEX_MODULE_ID, version)
    except ValueError:
        return store.get_record(CODEX_MODULE_ID, version)
