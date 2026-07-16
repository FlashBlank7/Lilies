from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .applications import ApplicationService
from .capability_contracts import (
    CapabilityBase,
    CapabilityBuildContract,
    CarrierStatus,
    EnvironmentAvailability,
    EvidenceEnvironment,
    EvidenceLevel,
    ExternalContract,
    FunctionalCapability,
    RuntimeGuarantee,
    VerificationStatus,
)
from .models import utc_now
from .platform_harness import PlatformHarness
from .storage import Storage
from .workflow_models import (
    ApplicationSnapshot,
    TestAssertion,
    TestFrameSpec,
    WorkflowTestCase,
)
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import RevisionConflict, WorkflowStorage


EvaluationExecutionMode = Literal["plan_only", "static", "runtime", "observation"]
EvaluationEligibility = Literal["ready", "blocked_by_environment", "unsupported"]
EvaluationOutcome = Literal["completed", "failed", "blocked", "unsupported"]
EvaluationApplyMode = Literal["merge", "replace_generated"]


class EvaluationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    level: EvidenceLevel
    maximum_status: VerificationStatus
    compatible_environment_kinds: list[EvidenceEnvironment]
    execution_mode: EvaluationExecutionMode
    workflow_execution_allowed: bool
    draft_test_apply_allowed: bool
    external_mutation_allowed: bool
    required_evidence_categories: list[str]
    excluded_claims: list[str]


class EvaluationEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    kind: EvidenceEnvironment
    availability: EnvironmentAvailability
    execution_mode: EvaluationExecutionMode
    workflow_execution_allowed: bool
    external_mutation_allowed: bool
    compatible_profile_ids: list[str]
    evidence_sources: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    claim_ceiling: VerificationStatus


class EvaluationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = "h2_component"
    environment_id: str = "local_sandbox"


class EvaluationApplyRequest(EvaluationPlanRequest):
    expected_revision: int = Field(ge=1)
    expected_content_hash: str = Field(min_length=16, max_length=128)
    mode: EvaluationApplyMode = "replace_generated"
    idempotency_key: str = Field(min_length=1, max_length=200)


class EvaluationRunRequest(EvaluationPlanRequest):
    expected_revision: int | None = Field(default=None, ge=1)
    expected_content_hash: str | None = Field(default=None, min_length=16, max_length=128)

    @model_validator(mode="after")
    def validate_draft_guard(self) -> EvaluationRunRequest:
        if (self.expected_revision is None) != (self.expected_content_hash is None):
            raise ValueError("expected_revision and expected_content_hash must be provided together")
        return self


class EvaluationCasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    family: str
    title: str
    capability_ids: list[str]
    capability_kind: Literal["F", "G", "X", "compatibility"]
    executable: bool
    blockers: list[str] = Field(default_factory=list)
    required_signals: list[str] = Field(default_factory=list)
    test: WorkflowTestCase


class EvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    application_id: str
    draft_revision: int
    draft_content_hash: str
    capability_contract_id: str | None
    profile: EvaluationProfile
    environment: EvaluationEnvironment
    eligibility: EvaluationEligibility
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cases: list[EvaluationCasePlan] = Field(default_factory=list)
    generated_tests: list[WorkflowTestCase] = Field(default_factory=list)
    existing_test_ids: list[str] = Field(default_factory=list)
    required_capability_ids: list[str] = Field(default_factory=list)
    covered_capability_ids: list[str] = Field(default_factory=list)
    claim_ceiling: VerificationStatus
    verified_claim_candidates: list[str] = Field(default_factory=list)
    excluded_claims: list[str] = Field(default_factory=list)


class EvaluationRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str
    application_id: str
    platform_task_id: str
    draft_revision: int
    draft_content_hash: str
    capability_contract_id: str | None
    profile_id: str
    profile_level: EvidenceLevel
    environment_id: str
    environment_kind: EvidenceEnvironment
    execution_mode: EvaluationExecutionMode
    eligibility: EvaluationEligibility
    outcome: EvaluationOutcome
    achieved_status: VerificationStatus
    passed: bool | None
    generated_test_ids: list[str] = Field(default_factory=list)
    executed_test_ids: list[str] = Field(default_factory=list)
    capability_results: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    verified_claims: list[str] = Field(default_factory=list)
    excluded_claims: list[str] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


_PROFILE_DEFINITIONS: tuple[EvaluationProfile, ...] = (
    EvaluationProfile(
        id="h0_design",
        title="H0 Design review",
        description="Inspect capability intent, generated cases, blockers, and claim boundaries without execution.",
        level=EvidenceLevel.H0,
        maximum_status=VerificationStatus.design_only,
        compatible_environment_kinds=[EvidenceEnvironment.mock],
        execution_mode="plan_only",
        workflow_execution_allowed=False,
        draft_test_apply_allowed=True,
        external_mutation_allowed=False,
        required_evidence_categories=["design"],
        excluded_claims=["static correctness", "runtime behavior", "external integration"],
    ),
    EvaluationProfile(
        id="h1_static",
        title="H1 Static contract",
        description="Validate typed contracts, graph structure, configuration, and generated case coverage without running the workflow.",
        level=EvidenceLevel.H1,
        maximum_status=VerificationStatus.static_verified,
        compatible_environment_kinds=[EvidenceEnvironment.mock],
        execution_mode="static",
        workflow_execution_allowed=False,
        draft_test_apply_allowed=True,
        external_mutation_allowed=False,
        required_evidence_categories=["implementation", "default", "api"],
        excluded_claims=["runtime behavior", "live external behavior", "production reliability"],
    ),
    EvaluationProfile(
        id="h2_component",
        title="H2 Component sandbox",
        description="Run generated capability cases inside the isolated local workflow runtime.",
        level=EvidenceLevel.H2,
        maximum_status=VerificationStatus.component_verified,
        compatible_environment_kinds=[EvidenceEnvironment.sandbox],
        execution_mode="runtime",
        workflow_execution_allowed=True,
        draft_test_apply_allowed=True,
        external_mutation_allowed=False,
        required_evidence_categories=["implementation", "test"],
        excluded_claims=["live provider quality", "production durability", "customer environment behavior"],
    ),
    EvaluationProfile(
        id="h3_integration",
        title="H3 Integration contract",
        description="Run capability cases through local authenticated services, durable storage, and declared contract fixtures.",
        level=EvidenceLevel.H3,
        maximum_status=VerificationStatus.integration_verified,
        compatible_environment_kinds=[EvidenceEnvironment.contract],
        execution_mode="runtime",
        workflow_execution_allowed=True,
        draft_test_apply_allowed=True,
        external_mutation_allowed=False,
        required_evidence_categories=["implementation", "api", "test", "integration"],
        excluded_claims=["unconfigured live systems", "production SLO", "production billing authority"],
    ),
    EvaluationProfile(
        id="h4_live",
        title="H4 Live verification",
        description="Run against an explicitly enabled live target with eligible model, tool, and external evidence.",
        level=EvidenceLevel.H4,
        maximum_status=VerificationStatus.live_verified,
        compatible_environment_kinds=[EvidenceEnvironment.live],
        execution_mode="runtime",
        workflow_execution_allowed=True,
        draft_test_apply_allowed=True,
        external_mutation_allowed=True,
        required_evidence_categories=["implementation", "api", "test", "integration", "live"],
        excluded_claims=["production reliability", "production observation", "billing reconciliation"],
    ),
    EvaluationProfile(
        id="h5_production_observation",
        title="H5 Production observation",
        description="Consume eligible read-only production telemetry without starting or mutating a workflow.",
        level=EvidenceLevel.H5,
        maximum_status=VerificationStatus.production_observed,
        compatible_environment_kinds=[EvidenceEnvironment.production_observation],
        execution_mode="observation",
        workflow_execution_allowed=False,
        draft_test_apply_allowed=False,
        external_mutation_allowed=False,
        required_evidence_categories=[
            "implementation",
            "api",
            "test",
            "integration",
            "live",
            "telemetry",
        ],
        excluded_claims=["workflow execution during observation", "unobserved tenants", "future SLO compliance"],
    ),
)


_VERIFIED_STATUS_ORDER: dict[VerificationStatus, int] = {
    VerificationStatus.design_only: 0,
    VerificationStatus.static_verified: 1,
    VerificationStatus.component_verified: 2,
    VerificationStatus.integration_verified: 3,
    VerificationStatus.live_verified: 4,
    VerificationStatus.production_observed: 5,
}


class EvaluationHarness:
    GENERATED_TEST_PREFIX = "eval_"

    def __init__(
        self,
        *,
        storage: Storage,
        workflow_store: WorkflowStorage,
        applications: ApplicationService,
        workflow_runtime: WorkflowRuntime,
        harness: PlatformHarness,
        live_enabled: bool = False,
        production_observation_enabled: bool = False,
        production_observation_evidence_path: Path | None = None,
    ) -> None:
        self.storage = storage
        self.workflow_store = workflow_store
        self.applications = applications
        self.workflow_runtime = workflow_runtime
        self.harness = harness
        self.live_enabled = live_enabled
        self.production_observation_enabled = production_observation_enabled
        self.production_observation_evidence_path = production_observation_evidence_path

    def profiles(self) -> list[EvaluationProfile]:
        return [item.model_copy(deep=True) for item in _PROFILE_DEFINITIONS]

    def profile(self, profile_id: str) -> EvaluationProfile:
        for item in _PROFILE_DEFINITIONS:
            if item.id == profile_id:
                return item.model_copy(deep=True)
        raise KeyError(f"unknown evaluation profile: {profile_id}")

    def environments(self) -> list[EvaluationEnvironment]:
        observation_path = self.production_observation_evidence_path
        observation_ready = bool(
            self.production_observation_enabled
            and observation_path
            and observation_path.is_file()
        )
        return [
            EvaluationEnvironment(
                id="local_mock",
                title="Local mock",
                description="Design and static checks over the immutable draft without workflow execution.",
                kind=EvidenceEnvironment.mock,
                availability=EnvironmentAvailability.available,
                execution_mode="static",
                workflow_execution_allowed=False,
                external_mutation_allowed=False,
                compatible_profile_ids=["h0_design", "h1_static"],
                evidence_sources=["draft snapshot", "Capability Build Contract", "block schemas"],
                claim_ceiling=VerificationStatus.static_verified,
            ),
            EvaluationEnvironment(
                id="local_sandbox",
                title="Local sandbox",
                description="Single-process isolated component execution with local tools and fixtures.",
                kind=EvidenceEnvironment.sandbox,
                availability=EnvironmentAvailability.available,
                execution_mode="runtime",
                workflow_execution_allowed=True,
                external_mutation_allowed=False,
                compatible_profile_ids=["h2_component"],
                evidence_sources=["Workflow Runtime", "local sandbox", "Platform Harness trace"],
                claim_ceiling=VerificationStatus.component_verified,
            ),
            EvaluationEnvironment(
                id="local_contract",
                title="Local contract integration",
                description="Authenticated API, durable storage, runtime, and attached contract fixtures in one local deployment.",
                kind=EvidenceEnvironment.contract,
                availability=EnvironmentAvailability.available,
                execution_mode="runtime",
                workflow_execution_allowed=True,
                external_mutation_allowed=False,
                compatible_profile_ids=["h3_integration"],
                evidence_sources=["authenticated API", "SQLite persistence", "declared local fixtures"],
                claim_ceiling=VerificationStatus.integration_verified,
            ),
            EvaluationEnvironment(
                id="configured_live",
                title="Configured live target",
                description="Explicitly enabled live provider and external target; never enabled by profile selection alone.",
                kind=EvidenceEnvironment.live,
                availability=(
                    EnvironmentAvailability.available
                    if self.live_enabled
                    else EnvironmentAvailability.unavailable
                ),
                execution_mode="runtime",
                workflow_execution_allowed=True,
                external_mutation_allowed=True,
                compatible_profile_ids=["h4_live"],
                evidence_sources=["configured live provider and external target"] if self.live_enabled else [],
                missing_requirements=(
                    []
                    if self.live_enabled
                    else ["evaluation_live_enabled must be explicitly configured"]
                ),
                claim_ceiling=(
                    VerificationStatus.live_verified
                    if self.live_enabled
                    else VerificationStatus.blocked_by_environment
                ),
            ),
            EvaluationEnvironment(
                id="production_observation",
                title="Production observation",
                description="Read-only eligible production evidence; this environment cannot start or mutate a workflow.",
                kind=EvidenceEnvironment.production_observation,
                availability=(
                    EnvironmentAvailability.available
                    if observation_ready
                    else EnvironmentAvailability.unavailable
                ),
                execution_mode="observation",
                workflow_execution_allowed=False,
                external_mutation_allowed=False,
                compatible_profile_ids=["h5_production_observation"],
                evidence_sources=[str(observation_path)] if observation_ready and observation_path else [],
                missing_requirements=self._observation_missing_requirements(),
                claim_ceiling=(
                    VerificationStatus.production_observed
                    if observation_ready
                    else VerificationStatus.blocked_by_environment
                ),
            ),
        ]

    def environment(self, environment_id: str) -> EvaluationEnvironment:
        for item in self.environments():
            if item.id == environment_id:
                return item
        raise KeyError(f"unknown evaluation environment: {environment_id}")

    async def plan(
        self,
        application_id: str,
        request: EvaluationPlanRequest,
    ) -> EvaluationPlan:
        draft = await self.workflow_store.get_draft(application_id)
        return self._plan_for_draft(application_id, draft, request)

    def plan_for_contract(
        self,
        contract: CapabilityBuildContract,
        request: EvaluationPlanRequest,
        *,
        application_id: str = "contract-preview",
        snapshot: ApplicationSnapshot | None = None,
    ) -> EvaluationPlan:
        resolved_snapshot = snapshot or ApplicationSnapshot(
            name="Contract preview",
            description="",
            requirement=contract.source_requirement,
            capability_build_contract=contract,
        )
        return self._plan_for_draft(
            application_id,
            {
                "revision": 1,
                "content_hash": resolved_snapshot.content_hash(),
                "snapshot": resolved_snapshot,
            },
            request,
        )

    async def apply_generated_tests(
        self,
        application_id: str,
        request: EvaluationApplyRequest,
    ) -> dict[str, Any]:
        plan = await self.plan(application_id, request)
        if plan.eligibility == "unsupported":
            raise ValueError("evaluation plan is unsupported: " + "; ".join(plan.blockers))
        if not plan.profile.draft_test_apply_allowed:
            raise ValueError(f"profile {plan.profile.id} is read-only and cannot apply draft tests")
        draft = await self.workflow_store.get_draft(application_id)
        existing = list(draft["snapshot"].tests)
        generated_by_id = {item.id: item for item in plan.generated_tests}
        if request.mode == "replace_generated":
            retained = [item for item in existing if not item.id.startswith(self.GENERATED_TEST_PREFIX)]
        else:
            retained = [item for item in existing if item.id not in generated_by_id]
        next_tests = [*retained, *plan.generated_tests]
        result = await self.applications.apply_operations_atomically(
            application_id,
            expected_revision=request.expected_revision,
            expected_content_hash=request.expected_content_hash,
            operations=[{
                "op": "replace_tests",
                "data": {"tests": [item.model_dump(mode="json") for item in next_tests]},
            }],
            idempotency_key=request.idempotency_key,
            change_context_operation="evaluation_tests_apply",
        )
        await self.storage.append_event(application_id, "evaluation.tests.applied", {
            "profile_id": plan.profile.id,
            "environment_id": plan.environment.id,
            "mode": request.mode,
            "generated_test_ids": sorted(generated_by_id),
            "revision": result["revision"],
            "content_hash": result["content_hash"],
        })
        return {
            **result,
            "profile_id": plan.profile.id,
            "environment_id": plan.environment.id,
            "mode": request.mode,
            "generated_test_ids": sorted(generated_by_id),
            "test_count": len(next_tests),
        }

    async def run(
        self,
        application_id: str,
        request: EvaluationRunRequest,
    ) -> EvaluationRunRecord:
        draft = await self.workflow_store.get_draft(application_id)
        if request.expected_revision is not None:
            if draft["revision"] != request.expected_revision:
                raise RevisionConflict(
                    f"expected revision {request.expected_revision}, current {draft['revision']}"
                )
            if draft["content_hash"] != request.expected_content_hash:
                raise RevisionConflict("draft content hash changed before evaluation")
        plan = self._plan_for_draft(application_id, draft, request)
        run_id = f"evaluation:{uuid4()}"
        created_at = utc_now()
        task_metadata = {
            "application_id": application_id,
            "workflow_id": application_id,
            "draft_revision": draft["revision"],
            "content_hash": draft["content_hash"],
            "profile_id": plan.profile.id,
            "profile_level": plan.profile.level.value,
            "environment_id": plan.environment.id,
            "environment_kind": plan.environment.kind.value,
            "execution_mode": plan.profile.execution_mode,
            "eligibility": plan.eligibility,
        }
        await self.harness.start_task(
            run_id,
            kind="evaluation_run",
            owner_id=application_id,
            resource_id=application_id,
            metadata=task_metadata,
        )
        try:
            record = await self._execute_plan(
                run_id=run_id,
                application_id=application_id,
                draft=draft,
                plan=plan,
                created_at=created_at,
            )
            await self.storage.save_evaluation_run(record.model_dump(mode="json"))
            await self.harness.finish_task(
                run_id,
                status="succeeded" if record.outcome != "failed" else "failed",
                error="; ".join(record.blockers) if record.outcome == "failed" else "",
                metadata={
                    "evaluation_outcome": record.outcome,
                    "achieved_status": record.achieved_status.value,
                    "passed": record.passed,
                    "blockers": record.blockers,
                },
            )
        except Exception as error:
            record = self._failed_record(
                run_id=run_id,
                application_id=application_id,
                draft=draft,
                plan=plan,
                created_at=created_at,
                error=error,
            )
            await self.storage.save_evaluation_run(record.model_dump(mode="json"))
            await self.harness.finish_task(
                run_id,
                status="failed",
                error=str(error),
                metadata={
                    "evaluation_outcome": "failed",
                    "achieved_status": VerificationStatus.design_only.value,
                    "blockers": record.blockers,
                },
            )
        await self.storage.append_event(run_id, "evaluation.completed", record.model_dump(mode="json"))
        await self.storage.append_event(application_id, "evaluation.completed", {
            "evaluation_run_id": record.id,
            "profile_id": record.profile_id,
            "environment_id": record.environment_id,
            "outcome": record.outcome,
            "achieved_status": record.achieved_status.value,
            "passed": record.passed,
            "blockers": record.blockers,
        })
        return record

    async def get_run(self, run_id: str) -> EvaluationRunRecord:
        return EvaluationRunRecord.model_validate(await self.storage.get_evaluation_run(run_id))

    async def list_runs(self, application_id: str, *, limit: int = 50) -> list[EvaluationRunRecord]:
        rows = await self.storage.list_evaluation_runs(application_id, limit=limit)
        return [EvaluationRunRecord.model_validate(row) for row in rows]

    def _plan_for_draft(
        self,
        application_id: str,
        draft: dict[str, Any],
        request: EvaluationPlanRequest,
    ) -> EvaluationPlan:
        profile = self.profile(request.profile_id)
        environment = self.environment(request.environment_id)
        snapshot: ApplicationSnapshot = draft["snapshot"]
        contract = snapshot.capability_build_contract
        blockers: list[str] = []
        warnings: list[str] = []
        eligibility: EvaluationEligibility = "ready"
        if environment.kind not in profile.compatible_environment_kinds:
            eligibility = "unsupported"
            blockers.append(
                f"profile {profile.id} is incompatible with environment {environment.id}"
            )
        elif profile.id not in environment.compatible_profile_ids:
            eligibility = "unsupported"
            blockers.append(
                f"environment {environment.id} does not support profile {profile.id}"
            )
        elif environment.availability != EnvironmentAvailability.available:
            eligibility = "blocked_by_environment"
            blockers.extend(environment.missing_requirements or [
                f"environment {environment.id} is {environment.availability.value}"
            ])
        if contract is None and profile.level not in {EvidenceLevel.H0, EvidenceLevel.H1}:
            eligibility = "unsupported"
            blockers.append("H2-H5 evaluation requires a Capability Build Contract")
        unavailable_contracts = self._unavailable_external_contracts(contract)
        if unavailable_contracts:
            warnings.extend(
                f"external contract unavailable: {item.id} ({item.availability_reason or 'no environment'})"
                for item in unavailable_contracts
            )
            if profile.level not in {EvidenceLevel.H0, EvidenceLevel.H1} and eligibility == "ready":
                eligibility = "blocked_by_environment"
                blockers.extend(f"external contract unavailable: {item.id}" for item in unavailable_contracts)
        cases = self._generate_cases(snapshot, contract, profile, environment, eligibility)
        generated_tests = [item.test for item in cases]
        required_capability_ids = [
            item.id for item in contract.capabilities if item.required
        ] if contract else []
        covered_capability_ids = sorted({
            capability_id
            for item in cases
            for capability_id in item.capability_ids
        })
        missing_coverage = sorted(set(required_capability_ids) - set(covered_capability_ids))
        if missing_coverage:
            eligibility = "unsupported"
            blockers.extend(
                f"no generated evaluation case for capability: {item}"
                for item in missing_coverage
            )
        claim_ceiling = self._plan_claim_ceiling(profile, environment, contract, eligibility)
        excluded = list(dict.fromkeys([
            *profile.excluded_claims,
            *(contract.claim_scope.excluded if contract else ["capability-scoped runtime verification"]),
            *warnings,
            *blockers,
        ]))
        return EvaluationPlan(
            application_id=application_id,
            draft_revision=int(draft["revision"]),
            draft_content_hash=str(draft["content_hash"]),
            capability_contract_id=contract.contract_id if contract else None,
            profile=profile,
            environment=environment,
            eligibility=eligibility,
            blockers=list(dict.fromkeys(blockers)),
            warnings=list(dict.fromkeys(warnings)),
            cases=cases,
            generated_tests=generated_tests,
            existing_test_ids=[item.id for item in snapshot.tests],
            required_capability_ids=required_capability_ids,
            covered_capability_ids=covered_capability_ids,
            claim_ceiling=claim_ceiling,
            verified_claim_candidates=(contract.claim_scope.verified if contract else []),
            excluded_claims=excluded,
        )

    def _generate_cases(
        self,
        snapshot: ApplicationSnapshot,
        contract: CapabilityBuildContract | None,
        profile: EvaluationProfile,
        environment: EvaluationEnvironment,
        eligibility: EvaluationEligibility,
    ) -> list[EvaluationCasePlan]:
        if contract is None:
            test_id = self._stable_case_id("compatibility", "draft", profile.id, environment.id)
            test = WorkflowTestCase(
                id=test_id,
                name="Draft structure compatibility",
                requirement="Validate the current draft structure without a Capability Build Contract.",
                frame=TestFrameSpec(
                    title="Draft structure compatibility",
                    category="structure",
                    purpose="Bound legacy drafts to design or static evidence only.",
                    reviewer_guidance="Create a Capability Build Contract before requesting H2 or higher.",
                    reference="EVAL-001 / EVAL-003",
                    failure_target="Capability Build Contract",
                ),
                inputs={},
                assertions=[TestAssertion(path=[], operator="exists", structural=True)],
                mandatory=True,
                structural_only=True,
                evidence_target=self._evidence_target(profile, environment),
                feedback_hints=["Add a Capability Build Contract for capability-scoped runtime evaluation."],
            )
            return [EvaluationCasePlan(
                id=test_id,
                family="compatibility_structure",
                title=test.name,
                capability_ids=[],
                capability_kind="compatibility",
                executable=False,
                blockers=([] if eligibility == "ready" else ["evaluation plan is not eligible"]),
                required_signals=["valid draft graph", "typed node configuration"],
                test=test,
            )]
        return [
            self._case_for_capability(
                snapshot,
                contract,
                capability,
                profile,
                environment,
                eligibility,
            )
            for capability in contract.capabilities
            if capability.required
        ]

    def _case_for_capability(
        self,
        snapshot: ApplicationSnapshot,
        contract: CapabilityBuildContract,
        capability: CapabilityBase,
        profile: EvaluationProfile,
        environment: EvaluationEnvironment,
        eligibility: EvaluationEligibility,
    ) -> EvaluationCasePlan:
        family, category, desired_types, minimum_tool_calls, signals = self._case_shape(capability)
        node_types = {node.type for node in snapshot.workflow.nodes}
        carrier_authoritative, carrier_node_types, carrier_refs = self._carrier_requirements(
            snapshot,
            contract,
            capability.id,
        )
        if carrier_authoritative:
            required_node_types = carrier_node_types
            if not set(carrier_node_types) & {"tool", "tool_executor", "http_request"}:
                minimum_tool_calls = 0
            signals = [*signals, *(f"carrier:{item}" for item in carrier_refs)]
        else:
            required_node_types = [
                self._present_or_primary(node_types, candidates)
                for candidates in desired_types
            ]
            required_node_types = [item for item in required_node_types if item]
        blockers: list[str] = []
        if isinstance(capability, ExternalContract) and capability.availability == EnvironmentAvailability.unavailable:
            blockers.append(
                f"external contract {capability.id} is unavailable: "
                f"{capability.availability_reason or 'no eligible fixture'}"
            )
        if eligibility != "ready":
            blockers.append(f"evaluation plan is {eligibility}")
        test_id = self._stable_case_id(
            contract.contract_id,
            capability.id,
            profile.id,
            environment.id,
        )
        title = f"{capability.title} [{profile.level.value}]"
        case_inputs = self._sample_inputs(contract)
        if "connector_idempotency_key" in case_inputs:
            case_inputs["connector_idempotency_key"] = f"evaluation-{test_id}"
        test = WorkflowTestCase(
            id=test_id,
            name=title,
            requirement=capability.description,
            frame=TestFrameSpec(
                title=title,
                category=category,
                purpose=f"Verify {capability.id} within {profile.title}.",
                reviewer_guidance=(
                    f"The result cannot exceed {profile.maximum_status.value} in "
                    f"{environment.title}."
                ),
                reference=f"{contract.contract_id} / {capability.id}",
                failure_target=capability.title,
            ),
            inputs=case_inputs,
            assertions=[TestAssertion(path=[], operator="exists", structural=True)],
            required_node_types=required_node_types,
            minimum_tool_calls=minimum_tool_calls,
            mandatory=True,
            structural_only=True,
            feedback_hints=[
                f"Inspect the carrier and evidence plan for {capability.id}.",
                *capability.acceptance[:3],
            ],
            capability_ids=[capability.id],
            evidence_target=self._evidence_target(profile, environment),
        )
        return EvaluationCasePlan(
            id=test_id,
            family=family,
            title=title,
            capability_ids=[capability.id],
            capability_kind=capability.kind,
            executable=(
                eligibility == "ready"
                and profile.workflow_execution_allowed
                and environment.workflow_execution_allowed
                and not blockers
            ),
            blockers=blockers,
            required_signals=signals,
            test=test,
        )

    async def _execute_plan(
        self,
        *,
        run_id: str,
        application_id: str,
        draft: dict[str, Any],
        plan: EvaluationPlan,
        created_at: str,
    ) -> EvaluationRunRecord:
        if plan.eligibility == "unsupported":
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="unsupported",
                achieved_status=VerificationStatus.unsupported,
                passed=None,
                blockers=plan.blockers,
            )
        if plan.eligibility == "blocked_by_environment":
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="blocked",
                achieved_status=VerificationStatus.blocked_by_environment,
                passed=None,
                blockers=plan.blockers,
            )
        if plan.profile.execution_mode == "plan_only":
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="completed",
                achieved_status=VerificationStatus.design_only,
                passed=True,
                report={"plan_only": True, "case_count": len(plan.cases)},
            )
        if plan.profile.execution_mode == "static":
            validation = await self.applications.validate_draft(application_id)
            passed = bool(validation.get("valid"))
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="completed" if passed else "failed",
                achieved_status=(
                    VerificationStatus.static_verified
                    if passed
                    else VerificationStatus.design_only
                ),
                passed=passed,
                blockers=[] if passed else [str(item) for item in validation.get("errors", [])],
                report={"static_validation": validation},
            )
        if plan.profile.execution_mode == "observation":
            observation = self._load_production_observation(application_id, draft["content_hash"])
            if observation["valid"]:
                return self._record(
                    run_id,
                    application_id,
                    draft,
                    plan,
                    created_at,
                    outcome="completed",
                    achieved_status=plan.claim_ceiling,
                    passed=True,
                    report={"production_observation": observation},
                )
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="blocked",
                achieved_status=VerificationStatus.blocked_by_environment,
                passed=None,
                blockers=observation["errors"],
                report={"production_observation": observation},
            )
        current_test_ids = {item.id for item in draft["snapshot"].tests}
        missing_generated = sorted(
            item.id for item in plan.generated_tests if item.id not in current_test_ids
        )
        if missing_generated:
            return self._record(
                run_id,
                application_id,
                draft,
                plan,
                created_at,
                outcome="unsupported",
                achieved_status=VerificationStatus.unsupported,
                passed=None,
                blockers=[
                    "generated evaluation cases must be applied to the current draft before runtime evaluation",
                    *[f"missing generated case: {item}" for item in missing_generated],
                ],
            )
        report = await self.workflow_runtime.run_test_suite(
            application_id,
            harness_task_id=run_id,
            manage_harness_task=False,
            origin="evaluation_harness",
        )
        passed = bool(report.get("passed"))
        achieved = plan.claim_ceiling if passed else VerificationStatus.design_only
        blockers = [] if passed else self._runtime_failure_messages(report)
        executed_test_ids = [
            str(item.get("test_id"))
            for item in report.get("tests", [])
            if isinstance(item, dict) and item.get("test_id")
        ]
        capability_results = self._capability_results(plan, report, achieved)
        return self._record(
            run_id,
            application_id,
            draft,
            plan,
            created_at,
            outcome="completed" if passed else "failed",
            achieved_status=achieved,
            passed=passed,
            blockers=blockers,
            report={"runtime_test_report": report},
            executed_test_ids=executed_test_ids,
            capability_results=capability_results,
        )

    def _record(
        self,
        run_id: str,
        application_id: str,
        draft: dict[str, Any],
        plan: EvaluationPlan,
        created_at: str,
        *,
        outcome: EvaluationOutcome,
        achieved_status: VerificationStatus,
        passed: bool | None,
        blockers: list[str] | None = None,
        report: dict[str, Any] | None = None,
        executed_test_ids: list[str] | None = None,
        capability_results: list[dict[str, Any]] | None = None,
    ) -> EvaluationRunRecord:
        resolved_blockers = list(dict.fromkeys(blockers or []))
        results = capability_results or self._capability_results(plan, {}, achieved_status)
        verified_claims = [
            f"{item['capability_id']}: {item['status']}"
            for item in results
            if item.get("passed") is True
        ]
        excluded_claims = list(dict.fromkeys([
            *plan.excluded_claims,
            *resolved_blockers,
            *(
                ["runtime behavior was not executed"]
                if plan.profile.execution_mode in {"plan_only", "static", "observation"}
                else []
            ),
        ]))
        return EvaluationRunRecord(
            id=run_id,
            application_id=application_id,
            platform_task_id=run_id,
            draft_revision=int(draft["revision"]),
            draft_content_hash=str(draft["content_hash"]),
            capability_contract_id=plan.capability_contract_id,
            profile_id=plan.profile.id,
            profile_level=plan.profile.level,
            environment_id=plan.environment.id,
            environment_kind=plan.environment.kind,
            execution_mode=plan.profile.execution_mode,
            eligibility=plan.eligibility,
            outcome=outcome,
            achieved_status=achieved_status,
            passed=passed,
            generated_test_ids=[item.id for item in plan.generated_tests],
            executed_test_ids=executed_test_ids or [],
            capability_results=results,
            blockers=resolved_blockers,
            verified_claims=verified_claims,
            excluded_claims=excluded_claims,
            report=report or {},
            created_at=created_at,
            updated_at=utc_now(),
        )

    def _failed_record(
        self,
        *,
        run_id: str,
        application_id: str,
        draft: dict[str, Any],
        plan: EvaluationPlan,
        created_at: str,
        error: Exception,
    ) -> EvaluationRunRecord:
        return self._record(
            run_id,
            application_id,
            draft,
            plan,
            created_at,
            outcome="failed",
            achieved_status=VerificationStatus.design_only,
            passed=False,
            blockers=[f"{type(error).__name__}: {error}"],
            report={"error_type": type(error).__name__, "error": str(error)},
        )

    def _capability_results(
        self,
        plan: EvaluationPlan,
        report: dict[str, Any],
        achieved_status: VerificationStatus,
    ) -> list[dict[str, Any]]:
        test_results = {
            str(item.get("test_id")): item
            for item in report.get("tests", [])
            if isinstance(item, dict)
        }
        results: list[dict[str, Any]] = []
        for case in plan.cases:
            test_result = test_results.get(case.id)
            if case.blockers:
                passed: bool | None = None
                status = VerificationStatus.blocked_by_environment.value
            elif test_result is not None:
                passed = bool(test_result.get("passed"))
                status = (
                    achieved_status.value
                    if passed
                    else VerificationStatus.design_only.value
                )
            else:
                passed = (
                    plan.profile.execution_mode in {"plan_only", "static", "observation"}
                    and achieved_status not in {
                        VerificationStatus.blocked_by_environment,
                        VerificationStatus.unsupported,
                    }
                )
                status = achieved_status.value
            for capability_id in case.capability_ids:
                results.append({
                    "capability_id": capability_id,
                    "case_id": case.id,
                    "family": case.family,
                    "passed": passed,
                    "status": status,
                    "blockers": case.blockers,
                    "run_id": str(test_result.get("run_id", "")) if test_result else "",
                })
        return results

    @staticmethod
    def _case_shape(
        capability: CapabilityBase,
    ) -> tuple[str, str, list[tuple[str, ...]], int, list[str]]:
        if isinstance(capability, FunctionalCapability):
            text = f"{capability.id} {capability.title} {capability.description}".casefold()
            if any(token in text for token in ("loop", "tool", "collect", "workspace")):
                return (
                    "functional_tool_feedback",
                    "tooling",
                    [("loop", "iteration"), ("tool_executor", "tool", "http_request")],
                    1,
                    ["bounded iteration", "tool evidence", "terminal output"],
                )
            return (
                "functional_output",
                "content",
                [("answer", "end")],
                0,
                ["declared input", "terminal output", "carrier binding"],
            )
        if isinstance(capability, RuntimeGuarantee):
            mapping: dict[str, tuple[str, str, list[tuple[str, ...]], int, list[str]]] = {
                "tool_loop": ("bounded_tool_loop", "tooling", [("loop", "iteration"), ("tool_executor", "tool")], 1, ["feedback", "stop condition", "tool trace"]),
                "permission": ("permission_boundary", "safety", [("permission_gate", "human_input")], 0, ["approval decision", "denial path"]),
                "isolation": ("isolation_boundary", "safety", [("connector_action", "sandbox_boundary")], 0, ["tenant boundary", "scope declaration"]),
                "scheduling": ("durable_schedule", "structure", [("schedule_trigger",)], 0, ["schedule", "history", "deduplication"]),
                "durability": ("durable_state", "structure", [("event_recorder", "loop")], 0, ["checkpoint", "restart state"]),
                "retry_resume": ("retry_resume", "safety", [("loop", "iteration")], 0, ["retry", "resume", "deduplication"]),
                "idempotency": ("idempotent_side_effect", "safety", [], 0, ["idempotency key", "duplicate suppression"]),
                "audit": ("audit_provenance", "safety", [("event_recorder",)], 0, ["identity", "source", "decision trace"]),
                "compensation": ("compensation", "safety", [], 0, ["side-effect receipt", "compensating action"]),
                "observability": ("observable_result", "structure", [("event_recorder", "answer", "end")], 0, ["trace", "terminal status"]),
                "budget": ("budget_boundary", "safety", [("budget_gate",)], 0, ["configured limit", "exhaustion outcome"]),
            }
            return mapping.get(
                capability.guarantee_type,
                ("runtime_guarantee", "custom", [], 0, [capability.guarantee_type]),
            )
        mapping = {
            "workspace": ("workspace_contract", [("sandbox_boundary",)]),
            "model_provider": ("model_contract", [("model_turn", "llm")]),
            "site_access": ("site_access_contract", [("http_request", "tool_executor", "tool")]),
            "identity": ("identity_contract", [("permission_gate",)]),
            "storage": ("storage_contract", []),
            "notification": ("notification_contract", []),
            "callback": ("callback_contract", [("connector_action", "http_request", "tool_executor")]),
            "writeback": ("writeback_contract", [("connector_action", "http_request", "tool_executor")]),
            "deployment": ("deployment_contract", [("sandbox_boundary",)]),
            "data_schema": ("schema_contract", []),
        }
        family, desired = mapping.get(capability.contract_type, ("external_contract", []))
        signals = ["availability", "interface", "evidence receipt"]
        if capability.mutating:
            signals.extend(["permission", "idempotency", "compensation"])
        return family, "safety", desired, 0, signals

    @staticmethod
    def _present_or_primary(node_types: set[str], candidates: tuple[str, ...]) -> str:
        return next((item for item in candidates if item in node_types), candidates[0] if candidates else "")

    @staticmethod
    def _carrier_requirements(
        snapshot: ApplicationSnapshot,
        contract: CapabilityBuildContract,
        capability_id: str,
    ) -> tuple[bool, list[str], list[str]]:
        decision = next(
            (
                item
                for item in contract.carrier_decisions
                if item.capability_id == capability_id
            ),
            None,
        )
        if (
            decision is None
            or decision.status != CarrierStatus.bound
            or not decision.implementation_refs
        ):
            return False, [], []

        nodes_by_id = {node.id: node.type for node in snapshot.workflow.nodes}
        available_types = {node.type for node in snapshot.workflow.nodes}
        required_types: list[str] = []
        for reference in decision.implementation_refs:
            node_type = nodes_by_id.get(reference)
            if node_type is None and reference in available_types:
                node_type = reference
            if node_type and node_type not in required_types:
                required_types.append(node_type)
        return True, required_types, list(decision.implementation_refs)

    @staticmethod
    def _sample_inputs(contract: CapabilityBuildContract) -> dict[str, Any]:
        samples: dict[str, Any] = {}
        for item in contract.start_inputs:
            if item.default is not None:
                samples[item.name] = item.default
            elif item.value_type == "string":
                samples[item.name] = f"Evaluation sample for {item.label}"
            elif item.value_type == "number":
                samples[item.name] = 1
            elif item.value_type == "boolean":
                samples[item.name] = False
            elif item.value_type == "object":
                samples[item.name] = {}
            elif item.value_type in {"array", "file_list"}:
                samples[item.name] = []
            else:
                samples[item.name] = ""
        return samples

    @staticmethod
    def _evidence_target(profile: EvaluationProfile, environment: EvaluationEnvironment):
        from .capability_contracts import AcceptanceEvidenceTarget

        return AcceptanceEvidenceTarget(
            level=profile.level,
            environment=environment.kind,
            expected_status=profile.maximum_status,
            claim_scope=(
                f"Only evidence produced by {profile.title} in {environment.title}; "
                "selection alone grants no verification status."
            ),
        )

    @staticmethod
    def _stable_case_id(*parts: str) -> str:
        raw = "|".join(parts)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
        label = re.sub(r"[^a-z0-9]+", "_", parts[1].casefold()).strip("_")[:36]
        return f"eval_{label or 'case'}_{digest}"

    @staticmethod
    def _unavailable_external_contracts(
        contract: CapabilityBuildContract | None,
    ) -> list[ExternalContract]:
        if contract is None:
            return []
        return [
            item
            for item in contract.external_contracts
            if item.required and item.availability == EnvironmentAvailability.unavailable
        ]

    @staticmethod
    def _minimum_status(*statuses: VerificationStatus) -> VerificationStatus:
        verified = [item for item in statuses if item in _VERIFIED_STATUS_ORDER]
        return min(verified, key=_VERIFIED_STATUS_ORDER.__getitem__) if verified else VerificationStatus.design_only

    def _plan_claim_ceiling(
        self,
        profile: EvaluationProfile,
        environment: EvaluationEnvironment,
        contract: CapabilityBuildContract | None,
        eligibility: EvaluationEligibility,
    ) -> VerificationStatus:
        if eligibility == "unsupported":
            return VerificationStatus.unsupported
        if eligibility == "blocked_by_environment":
            return VerificationStatus.blocked_by_environment
        statuses = [profile.maximum_status, environment.claim_ceiling]
        if contract and contract.claim_scope.ceiling in _VERIFIED_STATUS_ORDER:
            statuses.append(contract.claim_scope.ceiling)
        return self._minimum_status(*statuses)

    def _observation_missing_requirements(self) -> list[str]:
        missing: list[str] = []
        if not self.production_observation_enabled:
            missing.append("evaluation_production_observation_enabled must be explicitly configured")
        if self.production_observation_evidence_path is None:
            missing.append("a production observation evidence path is required")
        elif not self.production_observation_evidence_path.is_file():
            missing.append("the configured production observation evidence file does not exist")
        return missing

    def _load_production_observation(
        self,
        application_id: str,
        content_hash: str,
    ) -> dict[str, Any]:
        path = self.production_observation_evidence_path
        errors = self._observation_missing_requirements()
        payload: dict[str, Any] = {}
        if not errors and path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
                else:
                    errors.append("production observation evidence must be a JSON object")
            except (OSError, ValueError) as error:
                errors.append(f"cannot read production observation evidence: {error}")
        required = {
            "application_id": application_id,
            "draft_content_hash": content_hash,
            "status": "observed",
        }
        for key, expected in required.items():
            if payload and payload.get(key) != expected:
                errors.append(f"production observation {key} does not match the evaluated draft")
        if payload and not payload.get("observed_at"):
            errors.append("production observation is missing observed_at")
        if payload and not payload.get("telemetry_refs"):
            errors.append("production observation is missing telemetry_refs")
        return {
            "valid": not errors,
            "source": str(path) if path else "",
            "errors": errors,
            "observation": payload,
        }

    @staticmethod
    def _runtime_failure_messages(report: dict[str, Any]) -> list[str]:
        messages: list[str] = []
        for item in report.get("tests", []):
            if not isinstance(item, dict) or item.get("passed"):
                continue
            readable = item.get("readable_report")
            if isinstance(readable, dict):
                checks = readable.get("failed_checks")
                if isinstance(checks, list):
                    messages.extend(str(check) for check in checks)
            if not messages:
                messages.append(f"evaluation case failed: {item.get('test_id', 'unknown')}")
        return list(dict.fromkeys(messages or ["evaluation runtime cases failed"]))
