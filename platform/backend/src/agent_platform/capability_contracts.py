from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionEnvelope(str, Enum):
    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class EnvironmentAvailability(str, Enum):
    available = "available"
    unavailable = "unavailable"
    unknown = "unknown"
    not_required = "not_required"


class CarrierType(str, Enum):
    atomic_block = "atomic_block"
    reusable_module = "reusable_module"
    runtime_service = "runtime_service"
    platform_control = "platform_control"
    connector_external_contract = "connector_external_contract"


class CarrierStatus(str, Enum):
    proposed = "proposed"
    bound = "bound"
    blocked_by_environment = "blocked_by_environment"
    unsupported = "unsupported"


class CoverageOwner(str, Enum):
    workflow_runtime = "workflow_runtime"
    evaluation_harness = "evaluation_harness"
    platform_harness = "platform_harness"
    external_system = "external_system"


class CoverageStatus(str, Enum):
    available = "available"
    partial = "partial"
    missing = "missing"
    not_applicable = "not_applicable"


class EvidenceLevel(str, Enum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    H4 = "H4"
    H5 = "H5"


class EvidenceEnvironment(str, Enum):
    mock = "mock"
    contract = "contract"
    sandbox = "sandbox"
    live = "live"
    production_observation = "production_observation"


class VerificationStatus(str, Enum):
    design_only = "design_only"
    static_verified = "static_verified"
    component_verified = "component_verified"
    integration_verified = "integration_verified"
    live_verified = "live_verified"
    production_observed = "production_observed"
    blocked_by_environment = "blocked_by_environment"
    unsupported = "unsupported"


class CapabilityBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    required: bool = True
    requires: list[str] = Field(default_factory=list, max_length=40)
    excludes: list[str] = Field(default_factory=list, max_length=40)
    required_envelope: ExecutionEnvelope = ExecutionEnvelope.E0
    acceptance: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_relationships(self) -> CapabilityBase:
        if self.id in self.requires:
            raise ValueError(f"capability {self.id} cannot require itself")
        if self.id in self.excludes:
            raise ValueError(f"capability {self.id} cannot exclude itself")
        if len(self.requires) != len(set(self.requires)):
            raise ValueError(f"capability {self.id} has duplicate requires entries")
        if len(self.excludes) != len(set(self.excludes)):
            raise ValueError(f"capability {self.id} has duplicate excludes entries")
        return self


class FunctionalCapability(CapabilityBase):
    kind: Literal["F"] = "F"
    inputs: list[str] = Field(default_factory=list, max_length=20)
    outputs: list[str] = Field(default_factory=list, max_length=20)


class RuntimeGuarantee(CapabilityBase):
    kind: Literal["G"] = "G"
    guarantee_type: Literal[
        "state",
        "tool_loop",
        "permission",
        "durability",
        "scheduling",
        "retry_resume",
        "idempotency",
        "isolation",
        "audit",
        "compensation",
        "observability",
        "budget",
        "other",
    ] = "other"


class ExternalContract(CapabilityBase):
    kind: Literal["X"] = "X"
    contract_type: Literal[
        "workspace",
        "model_provider",
        "site_access",
        "identity",
        "data_schema",
        "storage",
        "notification",
        "callback",
        "writeback",
        "deployment",
        "other",
    ] = "other"
    provider: str = Field(default="", max_length=300)
    interface: str = Field(default="", max_length=1000)
    availability: EnvironmentAvailability = EnvironmentAvailability.unknown
    availability_reason: str = Field(default="", max_length=2000)
    mutating: bool = False


class StartInputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    value_type: Literal["string", "number", "boolean", "object", "array", "file", "file_list"]
    required: bool = True
    default: Any = None
    description: str = Field(default="", max_length=1000)


class CapabilityCarrierDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    carrier_type: CarrierType
    resource_hint: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=2000)
    status: CarrierStatus = CarrierStatus.proposed
    implementation_refs: list[str] = Field(default_factory=list, max_length=30)


class CapabilityCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    owner: CoverageOwner
    status: CoverageStatus
    surface: str = Field(min_length=1, max_length=500)
    notes: str = Field(default="", max_length=2000)


class CapabilityEvidencePlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_ids: list[str] = Field(min_length=1, max_length=20)
    target_level: EvidenceLevel
    environment: EvidenceEnvironment
    expected_status: VerificationStatus
    required_evidence: list[str] = Field(min_length=1, max_length=30)
    claim_scope: str = Field(min_length=1, max_length=2000)


class AcceptanceEvidenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: EvidenceLevel
    environment: EvidenceEnvironment
    expected_status: VerificationStatus
    claim_scope: str = Field(default="", max_length=2000)


class CapabilityClaimScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceiling: VerificationStatus
    verified: list[str] = Field(default_factory=list, max_length=40)
    excluded: list[str] = Field(default_factory=list, max_length=40)


class CapabilityBuildContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    contract_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=160)
    generation_source: Literal["model", "legacy_compatibility", "reference"] = "model"
    source_requirement: str = Field(min_length=1, max_length=30_000)
    target_user: str = Field(min_length=1, max_length=2000)
    business_goal: str = Field(min_length=1, max_length=4000)
    start_inputs: list[StartInputContract] = Field(min_length=1, max_length=30)
    functional_capabilities: list[FunctionalCapability] = Field(min_length=1, max_length=80)
    runtime_guarantees: list[RuntimeGuarantee] = Field(default_factory=list, max_length=80)
    external_contracts: list[ExternalContract] = Field(default_factory=list, max_length=80)
    required_envelope: ExecutionEnvelope
    risk_level: RiskLevel
    risk_reasons: list[str] = Field(default_factory=list, max_length=30)
    carrier_decisions: list[CapabilityCarrierDecision] = Field(default_factory=list, max_length=240)
    platform_coverage: list[CapabilityCoverage] = Field(default_factory=list, max_length=240)
    evidence_plan: list[CapabilityEvidencePlanItem] = Field(default_factory=list, max_length=160)
    workflow_outline: list[str] = Field(min_length=1, max_length=60)
    runtime_interface: str = Field(min_length=1, max_length=4000)
    claim_scope: CapabilityClaimScope
    unresolved_decisions: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> CapabilityBuildContract:
        capability_ids = [item.id for item in self.capabilities]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capability ids must be unique across F/G/X")
        input_names = [item.name for item in self.start_inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("start input names must be unique")
        carrier_ids = [item.capability_id for item in self.carrier_decisions]
        if len(carrier_ids) != len(set(carrier_ids)):
            raise ValueError("each capability may have only one carrier decision")
        return self

    @property
    def capabilities(self) -> list[CapabilityBase]:
        return [
            *self.functional_capabilities,
            *self.runtime_guarantees,
            *self.external_contracts,
        ]


class CapabilityClosureResult(BaseModel):
    contract_id: str
    valid: bool
    declared_envelope: ExecutionEnvelope
    computed_envelope: ExecutionEnvelope
    envelope_sufficient: bool
    ordered_capability_ids: list[str]
    missing_dependencies: list[dict[str, str]]
    dependency_cycles: list[list[str]]
    exclusion_conflicts: list[list[str]]
    unavailable_external_contracts: list[str]
    unknown_carrier_decisions: list[str]
    unknown_coverage_capabilities: list[str]
    unknown_evidence_capabilities: list[str]
    invalid_carrier_states: list[str]
    missing_carrier_decisions: list[str]
    unbound_required_capabilities: list[str]
    missing_coverage: list[str]
    missing_evidence_plan: list[str]
    blocking_errors: list[str]
    scoped_gaps: list[str]
    claim_ceiling: VerificationStatus


_ENVELOPE_ORDER = {item: index for index, item in enumerate(ExecutionEnvelope)}


def evaluate_capability_contract(
    contract: CapabilityBuildContract,
    *,
    require_bound_carriers: bool = False,
) -> CapabilityClosureResult:
    capabilities = {item.id: item for item in contract.capabilities}
    required = {item.id for item in contract.capabilities if item.required}
    missing_dependencies: list[dict[str, str]] = []
    for item in contract.capabilities:
        if not item.required:
            continue
        for dependency in item.requires:
            if dependency not in capabilities:
                missing_dependencies.append({"capability_id": item.id, "missing": dependency})
            elif dependency not in required:
                missing_dependencies.append({"capability_id": item.id, "missing": dependency})

    cycles: list[list[str]] = []
    ordered: list[str] = []
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(capability_id: str) -> None:
        if capability_id in visited:
            return
        if capability_id in visiting:
            start = visiting.index(capability_id)
            cycle = [*visiting[start:], capability_id]
            if cycle not in cycles:
                cycles.append(cycle)
            return
        visiting.append(capability_id)
        capability = capabilities[capability_id]
        for dependency in capability.requires:
            if dependency in capabilities and dependency in required:
                visit(dependency)
        visiting.pop()
        visited.add(capability_id)
        ordered.append(capability_id)

    for capability_id in sorted(required):
        visit(capability_id)

    conflict_pairs: set[tuple[str, str]] = set()
    for capability_id in sorted(required):
        for excluded in capabilities[capability_id].excludes:
            if excluded in required:
                conflict_pairs.add(tuple(sorted((capability_id, excluded))))
    exclusion_conflicts = [list(pair) for pair in sorted(conflict_pairs)]

    computed = max(
        (capabilities[item].required_envelope for item in required),
        key=lambda value: _ENVELOPE_ORDER[value],
        default=ExecutionEnvelope.E0,
    )
    envelope_sufficient = (
        _ENVELOPE_ORDER[contract.required_envelope] >= _ENVELOPE_ORDER[computed]
    )

    decisions = {item.capability_id: item for item in contract.carrier_decisions}
    unknown_carriers = sorted(set(decisions) - set(capabilities))
    missing_carriers = sorted(item for item in required if item not in decisions)
    unbound: list[str] = []
    invalid_carrier_states: list[str] = []
    for capability_id in sorted(required):
        decision = decisions.get(capability_id)
        if decision is None:
            continue
        capability = capabilities[capability_id]
        environment_blocked = (
            isinstance(capability, ExternalContract)
            and capability.availability == EnvironmentAvailability.unavailable
            and decision.status == CarrierStatus.blocked_by_environment
        )
        if decision.status == CarrierStatus.unsupported:
            invalid_carrier_states.append(
                f"required capability is unsupported: {capability_id}"
            )
        elif decision.status == CarrierStatus.blocked_by_environment and not environment_blocked:
            invalid_carrier_states.append(
                f"environment-blocked carrier is not an unavailable external contract: {capability_id}"
            )
        if (
            environment_blocked and not decision.implementation_refs
        ) or (
            not environment_blocked
            and (decision.status != CarrierStatus.bound or not decision.implementation_refs)
        ):
            unbound.append(capability_id)

    coverage_ids = {item.capability_id for item in contract.platform_coverage}
    evidence_ids = {
        capability_id
        for item in contract.evidence_plan
        for capability_id in item.capability_ids
    }
    unknown_coverage = sorted(coverage_ids - set(capabilities))
    unknown_evidence = sorted(evidence_ids - set(capabilities))
    missing_coverage = sorted(required - coverage_ids)
    missing_evidence = sorted(required - evidence_ids)
    unavailable = sorted(
        item.id
        for item in contract.external_contracts
        if item.required and item.availability == EnvironmentAvailability.unavailable
    )

    blocking_errors = [
        *(f"missing dependency: {item['capability_id']} requires {item['missing']}" for item in missing_dependencies),
        *(f"dependency cycle: {' -> '.join(cycle)}" for cycle in cycles),
        *(f"exclusion conflict: {' excludes '.join(pair)}" for pair in exclusion_conflicts),
        *([] if envelope_sufficient else [
            f"declared envelope {contract.required_envelope.value} is below computed {computed.value}"
        ]),
        *(f"missing carrier decision: {item}" for item in missing_carriers),
        *(f"carrier decision references unknown capability: {item}" for item in unknown_carriers),
        *(f"coverage references unknown capability: {item}" for item in unknown_coverage),
        *(f"evidence plan references unknown capability: {item}" for item in unknown_evidence),
        *invalid_carrier_states,
        *(f"missing platform/evaluation ownership: {item}" for item in missing_coverage),
        *(f"missing evidence plan: {item}" for item in missing_evidence),
    ]
    if require_bound_carriers:
        blocking_errors.extend(f"carrier is not bound: {item}" for item in unbound)

    scoped_gaps = [
        *(f"external contract unavailable: {item}" for item in unavailable),
        *(f"carrier binding pending: {item}" for item in unbound),
        *contract.unresolved_decisions,
    ]
    claim_ceiling = (
        VerificationStatus.blocked_by_environment
        if unavailable
        else contract.claim_scope.ceiling
    )
    return CapabilityClosureResult(
        contract_id=contract.contract_id,
        valid=not blocking_errors,
        declared_envelope=contract.required_envelope,
        computed_envelope=computed,
        envelope_sufficient=envelope_sufficient,
        ordered_capability_ids=ordered,
        missing_dependencies=missing_dependencies,
        dependency_cycles=cycles,
        exclusion_conflicts=exclusion_conflicts,
        unavailable_external_contracts=unavailable,
        unknown_carrier_decisions=unknown_carriers,
        unknown_coverage_capabilities=unknown_coverage,
        unknown_evidence_capabilities=unknown_evidence,
        invalid_carrier_states=invalid_carrier_states,
        missing_carrier_decisions=missing_carriers,
        unbound_required_capabilities=unbound,
        missing_coverage=missing_coverage,
        missing_evidence_plan=missing_evidence,
        blocking_errors=blocking_errors,
        scoped_gaps=scoped_gaps,
        claim_ceiling=claim_ceiling,
    )


def capability_contract_routing(
    contract: CapabilityBuildContract,
    *,
    requested_planning_mode: Literal["auto", "required", "disabled"] = "auto",
) -> dict[str, Any]:
    closure = evaluate_capability_contract(contract)
    envelope_index = _ENVELOPE_ORDER[closure.computed_envelope]
    force_plan = envelope_index >= _ENVELOPE_ORDER[ExecutionEnvelope.E2] or contract.risk_level in {
        RiskLevel.high,
        RiskLevel.critical,
    }
    effective_planning_mode = "required" if force_plan else requested_planning_mode
    reuse_depth = "deep" if envelope_index >= 2 else "shallow" if envelope_index == 1 else "none"
    return {
        "activation_id": "capability_build_contract_routing",
        "routing_source": "capability_build_contract",
        "contract_id": contract.contract_id,
        "required_envelope": closure.computed_envelope.value,
        "declared_envelope": contract.required_envelope.value,
        "risk_level": contract.risk_level.value,
        "risk_reasons": contract.risk_reasons,
        "requested_planning_mode": requested_planning_mode,
        "effective_planning_mode": effective_planning_mode,
        "runtime_builder_policy": {
            "reuse_depth": reuse_depth,
            "planning_required": effective_planning_mode == "required",
            "carrier_binding_required": True,
            "claim_ceiling": closure.claim_ceiling.value,
        },
        "unavailable_external_contracts": closure.unavailable_external_contracts,
        "legacy_complexity_router_used": False,
        "decision_reasons": [
            f"execution envelope: {closure.computed_envelope.value}",
            f"orthogonal risk: {contract.risk_level.value}",
            *(f"unavailable external contract: {item}" for item in closure.unavailable_external_contracts),
        ],
    }


def render_workflow_build_plan(contract: CapabilityBuildContract, *, locale: str = "zh") -> str:
    chinese = locale == "zh"
    capability_sections = [
        ("功能能力 (F)" if chinese else "Functional capabilities (F)", contract.functional_capabilities),
        ("运行保证 (G)" if chinese else "Runtime guarantees (G)", contract.runtime_guarantees),
        ("外部契约 (X)" if chinese else "External contracts (X)", contract.external_contracts),
    ]
    lines = [
        "# 工作流搭建方案" if chinese else "# Workflow build plan",
        "",
        f"## {'目标使用者' if chinese else 'Target user'}",
        contract.target_user,
        "",
        f"## {'业务目标' if chinese else 'Business goal'}",
        contract.business_goal,
        "",
        f"## {'启动输入' if chinese else 'Start inputs'}",
        *(
            f"- `{item.name}` ({item.value_type}) - {item.label}: {item.description}"
            for item in contract.start_inputs
        ),
        "",
    ]
    for title, capabilities in capability_sections:
        lines.extend([f"## {title}"])
        if capabilities:
            for item in capabilities:
                detail = f"{item.description} [{item.required_envelope.value}]"
                if isinstance(item, ExternalContract):
                    detail += f" [{item.availability.value}]"
                lines.append(f"- `{item.id}` {item.title}: {detail}")
        else:
            lines.append("- 无" if chinese else "- None declared")
        lines.append("")
    lines.extend([
        f"## {'工作流步骤' if chinese else 'Workflow steps'}",
        *(f"{index}. {step}" for index, step in enumerate(contract.workflow_outline, start=1)),
        "",
        f"## {'运行界面' if chinese else 'Runtime interface'}",
        contract.runtime_interface,
        "",
        f"## {'执行包络与风险' if chinese else 'Execution envelope and risk'}",
        f"- {'执行包络' if chinese else 'Envelope'}: `{contract.required_envelope.value}`",
        f"- {'风险等级' if chinese else 'Risk'}: `{contract.risk_level.value}`",
        *(f"- {item}" for item in contract.risk_reasons),
        "",
        f"## {'能力载体' if chinese else 'Capability carriers'}",
        *(
            f"- `{item.capability_id}` -> `{item.carrier_type.value}` / {item.resource_hint}: {item.rationale}"
            for item in contract.carrier_decisions
        ),
        "",
        f"## {'证据与验收' if chinese else 'Evidence and acceptance'}",
        *(
            f"- {', '.join(item.capability_ids)} -> {item.target_level.value}/{item.environment.value}/{item.expected_status.value}: "
            + "; ".join(item.required_evidence)
            for item in contract.evidence_plan
        ),
        "",
        f"## {'权限、边界与声明范围' if chinese else 'Permissions, boundaries, and claim scope'}",
        f"- {'声明上限' if chinese else 'Claim ceiling'}: `{contract.claim_scope.ceiling.value}`",
        *(f"- {'已覆盖' if chinese else 'Verified'}: {item}" for item in contract.claim_scope.verified),
        *(f"- {'不包含' if chinese else 'Excluded'}: {item}" for item in contract.claim_scope.excluded),
        "",
        f"## {'未决事项' if chinese else 'Unresolved decisions'}",
        *(f"- {item}" for item in contract.unresolved_decisions),
        *(
            ["- 无" if chinese else "- None"]
            if not contract.unresolved_decisions
            else []
        ),
        "",
        f"## {'下一步生成建议' if chinese else 'Next build suggestion'}",
        (
            "Builder Team 应按能力依赖顺序选择载体、绑定实现、生成对应证据，再构建可编辑画布。"
            if chinese
            else "Builder Team should select carriers in dependency order, bind implementation, generate scoped evidence, and then build the editable canvas."
        ),
    ])
    return "\n".join(lines)


def legacy_intake_capability_contract(
    *,
    requirement: str,
    workflow_intent: dict[str, Any],
    completed_requirement: str,
) -> CapabilityBuildContract:
    target_user = str(workflow_intent.get("target_user") or "Legacy intake user")
    runtime_input = str(workflow_intent.get("runtime_input") or "request")
    runtime_output = str(workflow_intent.get("runtime_output") or "result")
    core_steps = [
        str(item)
        for item in workflow_intent.get("core_steps", [])
        if str(item).strip()
    ] or ["Process the submitted request", "Return the declared result"]
    permissions = [
        str(item)
        for item in workflow_intent.get("permissions", [])
        if str(item).strip()
    ]
    acceptance_cases = [
        str(item)
        for item in workflow_intent.get("acceptance_cases", [])
        if str(item).strip()
    ] or ["The declared result exists"]
    capability = FunctionalCapability(
        id="F.legacy_goal",
        title="Legacy intake goal",
        description=(completed_requirement.strip() or requirement)[:2000],
        required_envelope=ExecutionEnvelope.E1,
        inputs=[runtime_input],
        outputs=[runtime_output],
        acceptance=acceptance_cases,
    )
    guarantee = RuntimeGuarantee(
        id="G.legacy_trace",
        title="Traceable workflow execution",
        description="Execute the legacy workflow plan with a visible result and basic trace.",
        requires=[capability.id],
        required_envelope=ExecutionEnvelope.E1,
        guarantee_type="observability",
        acceptance=["A run exposes its final result and trace."],
    )
    return CapabilityBuildContract(
        contract_id=f"legacy.{uuid4()}",
        generation_source="legacy_compatibility",
        source_requirement=requirement,
        target_user=target_user,
        business_goal=requirement,
        start_inputs=[StartInputContract(
            name="request",
            label=runtime_input[:200] or "Request",
            value_type="string",
            description="Legacy requirement-intake input.",
        )],
        functional_capabilities=[capability],
        runtime_guarantees=[guarantee],
        required_envelope=ExecutionEnvelope.E1,
        risk_level=RiskLevel.medium if permissions else RiskLevel.low,
        risk_reasons=permissions,
        carrier_decisions=[
            CapabilityCarrierDecision(
                capability_id=capability.id,
                carrier_type=CarrierType.reusable_module,
                resource_hint="Builder-selected editable workflow module",
                rationale="Compatibility mapping retains model-written requirement semantics.",
            ),
            CapabilityCarrierDecision(
                capability_id=guarantee.id,
                carrier_type=CarrierType.runtime_service,
                resource_hint="workflow runtime and trace",
                rationale="Basic execution and trace are runtime responsibilities.",
            ),
        ],
        platform_coverage=[
            CapabilityCoverage(
                capability_id=capability.id,
                owner=CoverageOwner.workflow_runtime,
                status=CoverageStatus.partial,
                surface="legacy intake compatibility",
                notes="Carrier binding remains for Builder Team.",
            ),
            CapabilityCoverage(
                capability_id=guarantee.id,
                owner=CoverageOwner.workflow_runtime,
                status=CoverageStatus.available,
                surface="workflow runtime trace",
            ),
        ],
        evidence_plan=[
            CapabilityEvidencePlanItem(
                capability_ids=[capability.id, guarantee.id],
                target_level=EvidenceLevel.H1,
                environment=EvidenceEnvironment.mock,
                expected_status=VerificationStatus.static_verified,
                required_evidence=acceptance_cases,
                claim_scope="Compatibility contract structure only; no live behavior is implied.",
            )
        ],
        workflow_outline=core_steps,
        runtime_interface=f"Submit {runtime_input} and receive {runtime_output}.",
        claim_scope=CapabilityClaimScope(
            ceiling=VerificationStatus.static_verified,
            verified=["legacy requirement normalized into F/G/X structure"],
            excluded=["live provider quality", "production environment behavior"],
        ),
        unresolved_decisions=[
            "Builder Team must bind proposed carriers before completion."
        ],
    )


def _reference_contract(
    *,
    contract_id: str,
    requirement: str,
    target_user: str,
    business_goal: str,
    start_inputs: list[StartInputContract],
    functional: list[FunctionalCapability],
    guarantees: list[RuntimeGuarantee],
    external: list[ExternalContract],
    envelope: ExecutionEnvelope,
    risk: RiskLevel,
    risk_reasons: list[str],
    outline: list[str],
    runtime_interface: str,
    excluded_claims: list[str],
) -> CapabilityBuildContract:
    capabilities: list[CapabilityBase] = [*functional, *guarantees, *external]
    carriers: list[CapabilityCarrierDecision] = []
    coverage: list[CapabilityCoverage] = []
    evidence: list[CapabilityEvidencePlanItem] = []
    for capability in capabilities:
        if isinstance(capability, ExternalContract):
            carrier_type = CarrierType.connector_external_contract
            owner = CoverageOwner.external_system
            available = capability.availability != EnvironmentAvailability.unavailable
            carrier_status = (
                CarrierStatus.proposed
                if available
                else CarrierStatus.blocked_by_environment
            )
            coverage_status = CoverageStatus.partial if available else CoverageStatus.missing
            expected_status = (
                VerificationStatus.component_verified
                if available
                else VerificationStatus.blocked_by_environment
            )
            environment = EvidenceEnvironment.contract
        elif isinstance(capability, RuntimeGuarantee):
            carrier_type = (
                CarrierType.platform_control
                if capability.guarantee_type in {"permission", "isolation", "audit"}
                else CarrierType.runtime_service
            )
            owner = (
                CoverageOwner.platform_harness
                if carrier_type == CarrierType.platform_control
                else CoverageOwner.workflow_runtime
            )
            carrier_status = CarrierStatus.proposed
            coverage_status = CoverageStatus.partial
            expected_status = VerificationStatus.component_verified
            environment = EvidenceEnvironment.sandbox
        else:
            carrier_type = CarrierType.reusable_module
            owner = CoverageOwner.workflow_runtime
            carrier_status = CarrierStatus.proposed
            coverage_status = CoverageStatus.partial
            expected_status = VerificationStatus.component_verified
            environment = EvidenceEnvironment.sandbox
        carriers.append(CapabilityCarrierDecision(
            capability_id=capability.id,
            carrier_type=carrier_type,
            resource_hint=f"reference:{capability.id}",
            rationale=f"Reference carrier for {capability.title}.",
            status=carrier_status,
            implementation_refs=(
                [f"contract:{capability.id}"]
                if carrier_status == CarrierStatus.blocked_by_environment
                else []
            ),
        ))
        coverage.append(CapabilityCoverage(
            capability_id=capability.id,
            owner=owner,
            status=coverage_status,
            surface=f"reference:{owner.value}",
            notes=capability.availability_reason if isinstance(capability, ExternalContract) else "",
        ))
        evidence.append(CapabilityEvidencePlanItem(
            capability_ids=[capability.id],
            target_level=EvidenceLevel.H2,
            environment=environment,
            expected_status=expected_status,
            required_evidence=[f"scenario-specific evidence for {capability.id}"],
            claim_scope=f"Only {capability.title} inside the declared reference boundary.",
        ))
    return CapabilityBuildContract(
        contract_id=contract_id,
        generation_source="reference",
        source_requirement=requirement,
        target_user=target_user,
        business_goal=business_goal,
        start_inputs=start_inputs,
        functional_capabilities=functional,
        runtime_guarantees=guarantees,
        external_contracts=external,
        required_envelope=envelope,
        risk_level=risk,
        risk_reasons=risk_reasons,
        carrier_decisions=carriers,
        platform_coverage=coverage,
        evidence_plan=evidence,
        workflow_outline=outline,
        runtime_interface=runtime_interface,
        claim_scope=CapabilityClaimScope(
            ceiling=(
                VerificationStatus.blocked_by_environment
                if any(item.availability == EnvironmentAvailability.unavailable for item in external)
                else VerificationStatus.component_verified
            ),
            verified=["typed capability closure and local component boundary"],
            excluded=excluded_claims,
        ),
    )


def reference_capability_contract(scenario_id: str) -> CapabilityBuildContract:
    if scenario_id == "codex_like_workspace_agent":
        return _reference_contract(
            contract_id="reference.codex_workspace.v1",
            requirement="Build a workflow that can work like Codex inside a selected workspace.",
            target_user="Software team member working in a selected repository",
            business_goal="Plan a workspace task, use bounded tools, observe results, and return an inspectable answer.",
            start_inputs=[
                StartInputContract(name="task", label="Workspace task", value_type="string", description="Natural-language task."),
                StartInputContract(name="workspace_path", label="Workspace", value_type="string", description="Path inside the configured root."),
            ],
            functional=[
                FunctionalCapability(id="F.plan_act_observe", title="Plan-act-observe", description="Plan, choose a tool, observe feedback, and continue.", required_envelope="E2", inputs=["task"], outputs=["answer"]),
                FunctionalCapability(id="F.workspace_result", title="Workspace result", description="Return one customer-readable result grounded in workspace evidence.", requires=["F.plan_act_observe"], required_envelope="E2", outputs=["answer"]),
            ],
            guarantees=[
                RuntimeGuarantee(id="G.permission_boundary", title="Permission boundary", description="Resolve plan permission before mutating work.", required_envelope="E2", guarantee_type="permission"),
                RuntimeGuarantee(id="G.loop_trace", title="Bounded loop trace", description="Bound iterations and record feedback, stop reason, and checkpoints.", requires=["F.plan_act_observe"], required_envelope="E2", guarantee_type="tool_loop"),
            ],
            external=[
                ExternalContract(id="X.workspace", title="Workspace access", description="Read and mutate only inside the configured workspace root.", required_envelope="E2", contract_type="workspace", provider="Lilies sandbox", interface="registered workspace tools", availability="available"),
                ExternalContract(id="X.model", title="Model provider", description="A model chooses actions and produces the answer.", required_envelope="E2", contract_type="model_provider", provider="configured provider", interface="streaming model API", availability="unknown", availability_reason="Provider availability is deployment-specific."),
            ],
            envelope=ExecutionEnvelope.E2,
            risk=RiskLevel.medium,
            risk_reasons=["Tool use can read or mutate workspace data."],
            outline=["Assemble workspace context", "Produce and approve a plan", "Run the bounded tool-feedback loop", "Return result and trace"],
            runtime_interface="Task, workspace, network policy, approval, progress, trace, and final result.",
            excluded_claims=["unrestricted host access", "production durability", "live model quality"],
        )
    if scenario_id == "daily_web_collection":
        return _reference_contract(
            contract_id="reference.daily_web_collection.v1",
            requirement="Run a daily workflow that collects public Web material, deduplicates it, summarizes it, and notifies me.",
            target_user="Knowledge worker monitoring recurring public sources",
            business_goal="Produce one traceable daily digest without duplicate collection or silent loss after restart.",
            start_inputs=[
                StartInputContract(name="topic", label="Topic", value_type="string", description="Collection focus."),
                StartInputContract(name="sources", label="Sources", value_type="array", description="Approved source list."),
            ],
            functional=[
                FunctionalCapability(id="F.collect_sources", title="Collect sources", description="Fetch approved pages and traverse pagination.", required_envelope="E3", inputs=["sources"], outputs=["items"]),
                FunctionalCapability(id="F.digest_notify", title="Digest and notify", description="Deduplicate, summarize, store, and send one digest.", requires=["F.collect_sources"], required_envelope="E3", outputs=["digest"]),
            ],
            guarantees=[
                RuntimeGuarantee(id="G.daily_schedule", title="Daily schedule", description="Fire once per declared daily schedule with durable history.", required_envelope="E3", guarantee_type="scheduling"),
                RuntimeGuarantee(id="G.retry_resume_dedupe", title="Retry, resume, and dedupe", description="Resume after interruption and avoid duplicate items or fires.", requires=["G.daily_schedule"], required_envelope="E3", guarantee_type="retry_resume"),
                RuntimeGuarantee(id="G.provenance", title="Source provenance", description="Record source URL, collection time, and transformation trace.", requires=["F.collect_sources"], required_envelope="E3", guarantee_type="audit"),
            ],
            external=[
                ExternalContract(id="X.site_access", title="Site access", description="Sources permit the declared automated access pattern.", required_envelope="E3", contract_type="site_access", interface="HTTP/Web connector", availability="unavailable", availability_reason="No live site permission or access fixture is attached."),
                ExternalContract(id="X.digest_storage", title="Digest storage", description="Persist collected items and daily output.", required_envelope="E3", contract_type="storage", provider="configured storage", availability="unknown"),
                ExternalContract(id="X.notification", title="Notification channel", description="Deliver the completed digest.", required_envelope="E3", contract_type="notification", provider="configured channel", availability="unknown"),
            ],
            envelope=ExecutionEnvelope.E3,
            risk=RiskLevel.medium,
            risk_reasons=["Recurring network access must respect source policy and rate limits."],
            outline=["Trigger once per day", "Collect approved sources with pagination", "Deduplicate and persist provenance", "Summarize and notify", "Record retry/resume history"],
            runtime_interface="Schedule status, last/next fire, collection progress, provenance, failures, retry, cancel, and digest.",
            excluded_claims=["permission to scrape arbitrary sites", "restart-safe production operation", "notification delivery without configured credentials"],
        )
    if scenario_id == "customer_system_embedding":
        return _reference_contract(
            contract_id="reference.customer_embedding.v1",
            requirement="Embed an AI workflow deeply into a customer system with tenant identity, data access, and governed writeback.",
            target_user="Customer-system operator and governed end user",
            business_goal="Handle tenant-scoped requests and write results back with audit and compensation boundaries.",
            start_inputs=[
                StartInputContract(name="tenant_id", label="Tenant", value_type="string", default="test-tenant", description="Platform-injected authenticated customer tenant for the controlled reference profile."),
                StartInputContract(name="actor_id", label="Actor", value_type="string", default="test-operator", description="Platform-injected actor resolved from the signed subject mapping."),
                StartInputContract(name="actor_roles", label="Actor roles", value_type="array", default=["operator"], description="Platform-injected roles resolved from the tenant binding."),
                StartInputContract(name="request", label="Request", value_type="object", default={"case_id": "case-001"}, description="Versioned customer payload."),
                StartInputContract(name="connector_profile_id", label="Connector profile", value_type="string", default="test", description="Platform-injected deployment profile from the tenant binding."),
                StartInputContract(name="connector_authorization_id", label="Mutation authorization", value_type="string", required=False, default="", description="Optional exact-payload authorization supplied only for an approved mutation."),
                StartInputContract(name="connector_idempotency_key", label="Connector idempotency key", value_type="string", default="evaluation-customer-embedding", description="Platform-injected request identity; evaluation replaces it per case."),
                StartInputContract(name="write_mode", label="Write mode", value_type="string", default="dry_run", description="Controlled evaluation defaults to adapter-free mutation preview."),
            ],
            functional=[
                FunctionalCapability(id="F.embedded_request", title="Embedded request handling", description="Receive, validate, and process a customer-system request.", required_envelope="E4", inputs=["request"], outputs=["result"]),
                FunctionalCapability(id="F.governed_writeback", title="Governed writeback", description="Write an approved result into the customer system.", requires=["F.embedded_request", "G.tenant_isolation", "G.compensation"], required_envelope="E5", outputs=["writeback_receipt"]),
            ],
            guarantees=[
                RuntimeGuarantee(id="G.tenant_isolation", title="Tenant isolation", description="Keep identity, data, secrets, and traces tenant-scoped.", required_envelope="E5", guarantee_type="isolation"),
                RuntimeGuarantee(id="G.idempotent_write", title="Idempotent write", description="Prevent duplicate side effects across retries.", required_envelope="E5", guarantee_type="idempotency"),
                RuntimeGuarantee(id="G.compensation", title="Compensation", description="Record and execute a compensating action for failed writeback.", requires=["G.idempotent_write"], required_envelope="E5", guarantee_type="compensation"),
                RuntimeGuarantee(id="G.audit", title="Governed audit", description="Record identity, authorization, input, decision, side effect, and receipt.", required_envelope="E4", guarantee_type="audit"),
            ],
            external=[
                ExternalContract(id="X.customer_identity", title="Customer identity", description="Authenticate user and tenant with mapped roles.", required_envelope="E4", contract_type="identity", interface="customer IdP", availability="unavailable", availability_reason="No test tenant or identity provider is attached."),
                ExternalContract(id="X.customer_schema", title="Customer schema", description="Validate versioned input and output records.", required_envelope="E4", contract_type="data_schema", interface="versioned schema contract", availability="unavailable", availability_reason="No customer schema fixture is attached."),
                ExternalContract(id="X.customer_writeback", title="Customer writeback", description="Perform a bounded customer-system mutation and return a receipt.", requires=["G.idempotent_write", "G.compensation"], required_envelope="E5", contract_type="writeback", interface="customer write API", availability="unavailable", availability_reason="No test writeback endpoint is attached.", mutating=True),
                ExternalContract(id="X.customer_callback", title="Customer status callback", description="Accept signed, ordered status callbacks and reject stale or replayed delivery.", requires=["F.governed_writeback", "G.audit"], required_envelope="E5", contract_type="callback", interface="customer callback endpoint", availability="unavailable", availability_reason="No signed callback fixture is attached."),
                ExternalContract(id="X.deployment", title="Deployment profile", description="Run in the customer-approved network and secret boundary.", required_envelope="E5", contract_type="deployment", interface="customer deployment profile", availability="unavailable", availability_reason="No customer deployment environment is attached."),
            ],
            envelope=ExecutionEnvelope.E5,
            risk=RiskLevel.high,
            risk_reasons=["Tenant data and customer-system writeback are high-impact side effects."],
            outline=["Authenticate tenant and validate schema", "Read authorized customer context", "Produce and review a decision", "Perform idempotent writeback", "Accept ordered status callback", "Record receipt, audit, and compensation state"],
            runtime_interface="Embedded request endpoint plus operator approval, tenant trace, writeback receipt, callback status, compensation, and audit views.",
            excluded_claims=["customer production identity", "real customer writeback", "tenant isolation without test environment", "production deployment compliance"],
        )
    raise KeyError(f"unknown reference capability scenario: {scenario_id}")


def reference_capability_contracts() -> list[CapabilityBuildContract]:
    return [
        reference_capability_contract("codex_like_workspace_agent"),
        reference_capability_contract("daily_web_collection"),
        reference_capability_contract("customer_system_embedding"),
    ]
