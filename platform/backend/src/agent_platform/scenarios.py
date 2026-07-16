from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .blocks import BlockRegistry
from .capability_contracts import (
    AcceptanceEvidenceTarget,
    CapabilityBuildContract,
    CarrierStatus,
    CoverageStatus,
    EvidenceEnvironment,
    EvidenceLevel,
    EnvironmentAvailability,
    VerificationStatus,
    reference_capability_contract,
)
from .workflow_models import TestAssertion, TestFrameSpec, WorkflowSpec, WorkflowTestCase


class ScenarioEvidenceProfile(BaseModel):
    profile_id: str
    selected_level: Literal["H0", "H1", "H2", "H3", "H4", "H5"]
    status: Literal[
        "design_only",
        "static_verified",
        "component_verified",
        "contract_verified",
        "integration_verified",
        "live_verified",
        "production_observed",
    ]
    model_boundary: str
    tool_boundary: str
    environment_boundary: str
    claim_scope: str
    excluded_claims: list[str] = Field(default_factory=list)


class ScenarioDefinition(BaseModel):
    id: str
    title: str
    description: str
    customer_outcome: str
    capability_set: list[str]
    required_envelope: str
    external_contracts: list[str]
    runtime_inputs: list[dict[str, Any]]
    structural_contract: dict[str, Any]
    evidence_profile: ScenarioEvidenceProfile
    capability_build_contract: CapabilityBuildContract
    workflow: WorkflowSpec
    acceptance_cases: list[WorkflowTestCase]

    def summary(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"workflow", "acceptance_cases"},
        ) | {"acceptance_case_count": len(self.acceptance_cases)}


class ScenarioCatalog:
    def __init__(self, blocks: BlockRegistry) -> None:
        self.blocks = blocks

    def list(self) -> list[dict[str, Any]]:
        return [
            self.get("codex_like_workspace_agent").summary(),
            self.get("daily_web_collection").summary(),
        ]

    def get(self, scenario_id: str) -> ScenarioDefinition:
        if scenario_id == "daily_web_collection":
            return self._daily_web_collection()
        if scenario_id != "codex_like_workspace_agent":
            raise KeyError(f"unknown scenario: {scenario_id}")
        prefix = "codex"
        capability_contract = reference_capability_contract(scenario_id)
        binding_refs = {
            "F.plan_act_observe": ["codex_loop"],
            "F.workspace_result": ["codex_answer"],
            "G.permission_boundary": ["codex_permission"],
            "G.loop_trace": ["codex_loop", "codex_trace"],
            "X.workspace": ["codex_sandbox"],
            "X.model": ["codex_plan"],
        }
        for decision in capability_contract.carrier_decisions:
            decision.status = CarrierStatus.bound
            decision.implementation_refs = binding_refs[decision.capability_id]
        for coverage in capability_contract.platform_coverage:
            coverage.status = CoverageStatus.available
            coverage.surface = "codex scenario component replay"
        workflow = self.blocks.expand_template(scenario_id, prefix=prefix)
        validation_errors = self.blocks.validate_workflow(workflow)
        if validation_errors:
            raise ValueError("invalid built-in scenario: " + "; ".join(validation_errors))
        return ScenarioDefinition(
            id=scenario_id,
            title="Codex-like Workspace Agent",
            description=(
                "Plan a workspace task, request approval, use registered file/command/Web tools inside "
                "the declared boundary, feed tool evidence into the next model turn, and return a final result."
            ),
            customer_outcome="Give one workspace task and receive an inspectable plan, live progress, and a final answer.",
            capability_set=[item.id for item in capability_contract.capabilities],
            required_envelope=(
                f"{capability_contract.required_envelope.value} local interactive workspace; "
                "E3 only when durable cross-process execution is configured"
            ),
            external_contracts=[
                f"{item.id}: {item.description} [{item.availability.value}]"
                for item in capability_contract.external_contracts
            ],
            runtime_inputs=[
                {"name": "task", "type": "string", "required": True, "customer_visible": True},
                {"name": "workspace_path", "type": "string", "required": False, "default": ".", "customer_visible": True},
                {"name": "network_policy", "type": "string", "required": False, "default": "none", "customer_visible": True},
                {"name": "cancel_requested", "type": "boolean", "required": False, "default": False, "customer_visible": False},
            ],
            structural_contract={
                "outer_graph": "acyclic",
                "top_level_node_types": [
                    "start",
                    "context_assembler",
                    "workspace_context_injector",
                    "context_compactor",
                    "capability_registry",
                    "model_turn",
                    "budget_gate",
                    "round_limit",
                    "permission_gate",
                    "sandbox_boundary",
                    "loop",
                    "event_recorder",
                    "answer",
                ],
                "nested_loop_node_types": [
                    "start",
                    "model_turn",
                    "tool_call_router",
                    "if_else",
                    "tool_executor",
                    "tool_result_normalizer",
                    "variable_aggregator",
                    "stop_continue_controller",
                    "end",
                ],
                "loop_fields": [
                    "initial_state",
                    "state_update",
                    "feedback_value",
                    "break_condition",
                    "cancel_condition",
                    "max_iterations",
                    "checkpoint_each_iteration",
                ],
            },
            evidence_profile=ScenarioEvidenceProfile(
                profile_id="codex_workspace_h2_component",
                selected_level="H2",
                status="component_verified",
                model_boundary="deterministic scripted model for the recorded replay",
                tool_boundary="registered Read tool executed against an isolated local workspace",
                environment_boundary="single-process test API with a local sandbox adapter",
                claim_scope="structured loop and tool-feedback capability work at component level",
                excluded_claims=[
                    "unrestricted host execution",
                    "live Web access",
                    "cross-process durable completion",
                    "production reliability or SLO",
                ],
            ),
            capability_build_contract=capability_contract,
            workflow=workflow,
            acceptance_cases=self._codex_acceptance_cases(prefix),
        )

    def _daily_web_collection(self) -> ScenarioDefinition:
        prefix = "daily"
        contract = reference_capability_contract("daily_web_collection")
        binding_refs = {
            "F.collect_sources": ["daily_collect"],
            "F.digest_notify": ["daily_digest", "daily_answer"],
            "G.daily_schedule": ["daily_schedule", "platform.durable_jobs"],
            "G.retry_resume_dedupe": ["platform.durable_jobs", "daily_collect"],
            "G.provenance": ["daily_collect", "platform.collection_receipts"],
            "X.site_access": ["daily_collect"],
            "X.digest_storage": ["platform.collection_receipts"],
            "X.notification": ["daily_answer", "customer_runtime"],
        }
        for decision in contract.carrier_decisions:
            decision.status = CarrierStatus.bound
            decision.implementation_refs = binding_refs[decision.capability_id]
        for external in contract.external_contracts:
            external.availability = EnvironmentAvailability.available
            if external.id == "X.site_access":
                external.provider = "controlled allowlisted HTTP fixture"
                external.interface = "web_collection block"
                external.availability_reason = (
                    "Available only for explicitly allowlisted controlled sources; arbitrary sites remain excluded."
                )
            elif external.id == "X.digest_storage":
                external.provider = "Lilies local durable store"
                external.interface = "collection receipt registry"
                external.availability_reason = "Available in the local integration environment."
            else:
                external.provider = "Customer Runtime"
                external.interface = "latest digest result"
                external.availability_reason = (
                    "Customer Runtime delivery is available; external push channels remain excluded."
                )
        for coverage in contract.platform_coverage:
            coverage.status = CoverageStatus.available
            coverage.surface = "daily collection local H3 integration"
        contract.claim_scope.excluded = sorted(
            set(contract.claim_scope.excluded)
            | {
                "permission to scrape arbitrary sites",
                "external push notification delivery",
                "distributed exactly-once side effects",
                "production unattended reliability or SLO",
            }
        )
        workflow = self.blocks.expand_template("daily_web_collection", prefix=prefix)
        validation_errors = self.blocks.validate_workflow(workflow)
        if validation_errors:
            raise ValueError("invalid built-in scenario: " + "; ".join(validation_errors))
        return ScenarioDefinition(
            id="daily_web_collection",
            title="Daily Controlled Web Collection",
            description=(
                "Run one durable daily job, collect explicitly approved sources, resume from per-source "
                "receipts, deduplicate content, and deliver a traceable Markdown digest."
            ),
            customer_outcome=(
                "See when the collection runs, what changed, which sources were denied or failed, and the "
                "latest digest without operating worker internals."
            ),
            capability_set=[item.id for item in contract.capabilities],
            required_envelope="E3 local durable integration; E4 for multi-tenant production operation",
            external_contracts=[
                f"{item.id}: {item.description} [{item.availability.value}] {item.availability_reason}"
                for item in contract.external_contracts
            ],
            runtime_inputs=[
                {
                    "name": "topic",
                    "type": "string",
                    "required": True,
                    "default": "Daily source digest",
                    "customer_visible": True,
                },
                {
                    "name": "sources",
                    "type": "array",
                    "required": True,
                    "default": [],
                    "customer_visible": True,
                },
            ],
            structural_contract={
                "top_level_node_types": [
                    "schedule_trigger",
                    "web_collection",
                    "collection_digest",
                    "answer",
                ],
                "durable_schedule_fields": [
                    "durable",
                    "max_attempts",
                    "retry_backoff_seconds",
                    "lease_seconds",
                ],
                "source_boundary_fields": [
                    "allowed_hosts",
                    "permission_basis",
                    "respect_robots",
                    "robots_failure_policy",
                    "timeout_seconds",
                    "max_content_bytes",
                ],
                "platform_records": [
                    "durable_jobs",
                    "durable_job_attempts",
                    "durable_job_events",
                    "collection_receipts",
                ],
            },
            evidence_profile=ScenarioEvidenceProfile(
                profile_id="daily_collection_h3_local_contract",
                selected_level="H3",
                status="integration_verified",
                model_boundary="deterministic digest transform; no model-quality claim",
                tool_boundary="controlled allowlisted HTTP collector with robots and provenance receipts",
                environment_boundary="local API, SQLite durability, and controlled HTTP fixture",
                claim_scope=(
                    "restart-safe local durable jobs and controlled-source collection at H3 integration"
                ),
                excluded_claims=list(contract.claim_scope.excluded),
            ),
            capability_build_contract=contract,
            workflow=workflow,
            acceptance_cases=self._daily_acceptance_cases(prefix),
        )

    @staticmethod
    def _daily_acceptance_cases(prefix: str) -> list[WorkflowTestCase]:
        common_inputs = {
            "topic": "Daily controlled source digest",
            "sources": [],
        }
        required_nodes = [
            "schedule_trigger",
            "web_collection",
            "collection_digest",
            "answer",
        ]
        return [
            WorkflowTestCase(
                id="daily_durable_structure",
                name="Durable schedule and collection carriers remain editable",
                requirement=(
                    "The daily workflow must visibly carry durable scheduling, controlled collection, "
                    "traceable digest, and customer output."
                ),
                frame=TestFrameSpec(
                    title="Daily durable capability carriers",
                    category="structure",
                    purpose="Prevent the E3 scenario from collapsing into one opaque scheduler service.",
                    reviewer_guidance=(
                        "Structure proves editable carriers, not live-site permission or production reliability."
                    ),
                    reference="SCENARIO-002 / ARCH-007",
                    failure_target="Daily Collection Schedule or Collect Approved Sources",
                ),
                inputs=common_inputs,
                assertions=[TestAssertion(path=["answer"], operator="exists", structural=True)],
                required_node_types=required_nodes,
                mandatory=True,
                structural_only=True,
                capability_ids=[
                    "F.collect_sources",
                    "F.digest_notify",
                    "G.daily_schedule",
                    "G.retry_resume_dedupe",
                ],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H1,
                    environment=EvidenceEnvironment.mock,
                    expected_status=VerificationStatus.static_verified,
                    claim_scope="Editable workflow and durable-carrier structure only.",
                ),
            ),
            WorkflowTestCase(
                id="daily_local_contract_runtime",
                name="Local contract run returns a customer digest",
                requirement=(
                    "The controlled local scenario must execute through the published workflow and return "
                    "a readable digest even when no source changed."
                ),
                frame=TestFrameSpec(
                    title="Daily collection local contract",
                    category="content",
                    purpose="Exercise the deterministic workflow path without a paid model or public site.",
                    reviewer_guidance=(
                        "A separate controlled-HTTP replay is required to prove source access and provenance."
                    ),
                    reference="SCENARIO-002",
                    failure_target="Build Traceable Digest or Daily Digest",
                ),
                inputs=common_inputs,
                assertions=[
                    TestAssertion(path=["answer"], operator="exists", structural=True),
                    TestAssertion(path=["answer"], operator="min_length", expected=20, structural=True),
                ],
                required_node_types=required_nodes,
                mandatory=True,
                structural_only=False,
                capability_ids=["F.collect_sources", "F.digest_notify", "X.digest_storage"],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H3,
                    environment=EvidenceEnvironment.contract,
                    expected_status=VerificationStatus.integration_verified,
                    claim_scope="Local workflow, durable storage, and Customer Runtime result integration.",
                ),
            ),
            WorkflowTestCase(
                id="daily_source_provenance_boundary",
                name="Source access and provenance boundary is explicit",
                requirement=(
                    "Collection must retain access policy and provenance carriers without claiming arbitrary-site permission."
                ),
                frame=TestFrameSpec(
                    title="Controlled source boundary",
                    category="safety",
                    purpose="Keep source permission, robots, receipts, and claim exclusions inspectable.",
                    reviewer_guidance=(
                        "Use an authorized live profile before making a public-site access claim."
                    ),
                    reference="SCENARIO-002 / EVAL-004",
                    failure_target="Collect Approved Sources",
                ),
                inputs=common_inputs,
                assertions=[TestAssertion(path=["answer"], operator="exists", structural=True)],
                required_node_types=["web_collection", "collection_digest"],
                mandatory=True,
                structural_only=True,
                capability_ids=["G.provenance", "X.site_access", "X.notification"],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H3,
                    environment=EvidenceEnvironment.contract,
                    expected_status=VerificationStatus.integration_verified,
                    claim_scope="Controlled-source contract and provenance shape; live sites excluded.",
                ),
            ),
        ]

    @staticmethod
    def _codex_acceptance_cases(prefix: str) -> list[WorkflowTestCase]:
        common_inputs = {
            "task": "Read README.md, use its evidence, and summarize what this workspace provides.",
            "workspace_path": ".",
            "network_policy": "none",
            "cancel_requested": False,
            "__permissions__": {f"{prefix}_permission": True},
        }
        required_nodes = [
            "start",
            "context_assembler",
            "workspace_context_injector",
            "context_compactor",
            "capability_registry",
            "model_turn",
            "permission_gate",
            "sandbox_boundary",
            "loop",
            "event_recorder",
            "answer",
        ]
        return [
            WorkflowTestCase(
                id="codex_workspace_tool_feedback",
                name="Workspace tool evidence reaches the final answer",
                requirement="The agent must inspect the selected workspace and use tool evidence before answering.",
                frame=TestFrameSpec(
                    title="Plan-act-observe tool feedback",
                    category="tooling",
                    purpose="Prove the Codex-like loop performs a workspace tool call and returns a readable result.",
                    reviewer_guidance="Inspect the run Trace to confirm the tool result entered the next model turn.",
                    reference="SCENARIO-001 / ARCH-006",
                    failure_target="Plan-Act-Observe Loop or Execute Routed Tool",
                ),
                inputs=common_inputs,
                assertions=[
                    TestAssertion(path=["answer"], operator="exists", structural=True),
                    TestAssertion(path=["answer"], operator="min_length", expected=5, structural=True),
                ],
                required_node_types=required_nodes,
                required_tools=["Read"],
                minimum_tool_calls=1,
                mandatory=True,
                structural_only=True,
                feedback_hints=[
                    "Check whether the first model turn routed Read and whether normalized output became loop feedback.",
                ],
                capability_ids=[
                    "F.plan_act_observe",
                    "F.workspace_result",
                    "G.loop_trace",
                    "X.workspace",
                    "X.model",
                ],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H2,
                    environment=EvidenceEnvironment.sandbox,
                    expected_status=VerificationStatus.component_verified,
                    claim_scope="Isolated workspace tool-feedback behavior only.",
                ),
            ),
            WorkflowTestCase(
                id="codex_workspace_permission_boundary",
                name="Plan and permission boundary remain visible",
                requirement="Planning, permission, sandbox, and bounded Loop controls must stay editable and connected.",
                frame=TestFrameSpec(
                    title="Permission and workspace boundary",
                    category="safety",
                    purpose="Prevent a coding-agent preset from hiding permission or sandbox behavior.",
                    reviewer_guidance="A passing structure check does not prove a production sandbox or live network policy.",
                    reference="SCENARIO-001",
                    failure_target="Plan Workspace Task, Approve Plan, or Workspace Boundary",
                ),
                inputs=common_inputs,
                assertions=[TestAssertion(path=["answer"], operator="exists", structural=True)],
                required_node_types=["model_turn", "permission_gate", "sandbox_boundary", "loop"],
                mandatory=True,
                structural_only=True,
                capability_ids=["G.permission_boundary", "X.workspace"],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H2,
                    environment=EvidenceEnvironment.sandbox,
                    expected_status=VerificationStatus.component_verified,
                    claim_scope="Visible permission and workspace boundary declarations.",
                ),
            ),
            WorkflowTestCase(
                id="codex_workspace_customer_result",
                name="Customer receives one final workspace result",
                requirement="The workflow must end in a customer-facing answer rather than engineering-only JSON.",
                frame=TestFrameSpec(
                    title="Customer result flow",
                    category="content",
                    purpose="Prove the editable engineering graph terminates in the Customer Runtime result surface.",
                    reviewer_guidance="Evaluate usefulness separately when a real model/environment profile is selected.",
                    reference="SCENARIO-001",
                    failure_target="Workspace Result",
                ),
                inputs=common_inputs,
                assertions=[TestAssertion(path=["answer"], operator="min_length", expected=5, structural=True)],
                required_node_types=["loop", "event_recorder", "answer"],
                mandatory=True,
                structural_only=True,
                capability_ids=["F.workspace_result"],
                evidence_target=AcceptanceEvidenceTarget(
                    level=EvidenceLevel.H2,
                    environment=EvidenceEnvironment.sandbox,
                    expected_status=VerificationStatus.component_verified,
                    claim_scope="Customer result shape inside the deterministic replay.",
                ),
            ),
        ]
