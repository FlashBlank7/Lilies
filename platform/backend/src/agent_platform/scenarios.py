from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .blocks import BlockRegistry
from .connector_sdk import ConnectorService
from .workflow_models import TestAssertion, TestFrameSpec, WorkflowSpec, WorkflowTestCase


class ScenarioEvidenceProfile(BaseModel):
    profile_id: str
    selected_level: str
    status: str
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
    workflow: WorkflowSpec
    acceptance_cases: list[WorkflowTestCase]

    def summary(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"workflow", "acceptance_cases"},
        ) | {"acceptance_case_count": len(self.acceptance_cases)}


class ScenarioCatalog:
    def __init__(
        self,
        blocks: BlockRegistry,
        *,
        connectors: ConnectorService | None = None,
    ) -> None:
        self.blocks = blocks
        self.connectors = connectors

    def list(self) -> list[dict[str, Any]]:
        return [
            self.get("codex_like_workspace_agent").summary(),
            self.get("daily_web_collection").summary(),
            self.get("customer_system_embedding").summary(),
        ]

    def get(self, scenario_id: str) -> ScenarioDefinition:
        if scenario_id == "daily_web_collection":
            return self._daily_web_collection()
        if scenario_id == "customer_system_embedding":
            return self._customer_system_embedding()
        if scenario_id != "codex_like_workspace_agent":
            raise KeyError(f"unknown scenario: {scenario_id}")
        prefix = "codex"
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
            capability_set=[],
            required_envelope=(
                "E2 local interactive workspace; "
                "E3 only when durable cross-process execution is configured"
            ),
            external_contracts=[
                "workspace: sandboxed local workspace access",
                "model: provider-neutral model calls",
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
            workflow=workflow,
            acceptance_cases=self._codex_acceptance_cases(prefix),
        )

    def _daily_web_collection(self) -> ScenarioDefinition:
        prefix = "daily"
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
            capability_set=[],
            required_envelope="E3 local durable integration; E4 for multi-tenant production operation",
            external_contracts=[
                "customer_system: governed writeback through a versioned Connector contract",
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
                excluded_claims=[],
            ),
            workflow=workflow,
            acceptance_cases=self._daily_acceptance_cases(prefix),
        )

    def _customer_system_embedding(self) -> ScenarioDefinition:
        prefix = "customer"
        availability = (
            self.connectors.contract_availability("customer_system", 1)
            if self.connectors is not None
            else {
                "available": False,
                "manifest": False,
                "tenant_bindings": 0,
                "available_profiles": [],
                "claim_ceiling": "H0",
            }
        )
        environment_available = bool(availability["available"])
        level_number = (
            min(int(str(availability["claim_ceiling"])[1:]), 3)
            if environment_available
            else 1
        )
        selected_level = f"H{max(level_number, 1)}"
        selected_status = {
            "H1": "static_verified",
            "H2": "component_verified",
            "H3": "integration_verified",
        }[selected_level]
        workflow = self.blocks.expand_template(
            "customer_system_embedding",
            prefix=prefix,
        )
        validation_errors = self.blocks.validate_workflow(workflow)
        if validation_errors:
            raise ValueError("invalid built-in scenario: " + "; ".join(validation_errors))
        return ScenarioDefinition(
            id="customer_system_embedding",
            title="Governed Customer-System Embedding",
            description=(
                "Receive a signed tenant request, validate it against a versioned Connector contract, "
                "read authorized context, and expose a dry-run-to-preauthorized writeback path with "
                "ordered callbacks, compensation, and audit evidence."
            ),
            customer_outcome=(
                "Use the workflow from a customer system while operators can inspect the exact tenant, "
                "policy, receipt, callback, and recovery state without exposing credentials."
            ),
            capability_set=[],
            required_envelope=(
                "E5 contract shape; current evidence is capped at controlled local H3 and does not "
                "establish customer-production readiness"
            ),
            external_contracts=[
                "customer_system: governed writeback through a versioned Connector contract",
            ],
            runtime_inputs=[
                {
                    "name": "request",
                    "type": "object",
                    "required": True,
                    "default": {"case_id": "case-001"},
                    "customer_visible": True,
                },
                {
                    "name": "write_mode",
                    "type": "string",
                    "required": True,
                    "default": "dry_run",
                    "customer_visible": True,
                },
            ],
            structural_contract={
                "top_level_node_types": [
                    "start",
                    "variable_assigner",
                    "connector_action",
                    "llm",
                    "answer",
                ],
                "platform_controls": [
                    "signed identity and tenant mapping",
                    "immutable manifest and schema validation",
                    "domain policy and emergency stop",
                    "payload-bound preauthorization",
                    "idempotent writeback",
                    "ordered signed callback",
                    "explicit compensation",
                    "tenant-scoped audit",
                ],
                "environment_availability": availability,
            },
            evidence_profile=ScenarioEvidenceProfile(
                profile_id="customer_embedding_controlled_boundary",
                selected_level=selected_level,
                status=selected_status,
                model_boundary="configured runtime model; no general model-quality claim",
                tool_boundary=(
                    "versioned Connector operations through tenant policy, preauthorization, and "
                    "allowlisted egress"
                ),
                environment_boundary=(
                    "one controlled test tenant and HTTP fixture"
                    if environment_available
                    else "no eligible controlled-test tenant is configured"
                ),
                claim_scope=(
                    "Controlled H3 Connector integration for the configured test tenant"
                    if selected_level == "H3"
                    else "Editable Connector contract and workflow structure only"
                ),
                excluded_claims=[],
            ),
            workflow=workflow,
            acceptance_cases=self._customer_embedding_acceptance_cases(
                environment_available=environment_available,
            ),
        )

    @staticmethod
    def _customer_embedding_acceptance_cases(
        *,
        environment_available: bool,
    ) -> list[WorkflowTestCase]:
        cases = [
            WorkflowTestCase(
                id="customer_embedding_editable_contract",
                name="Embedding carriers remain editable and governed",
                requirement=(
                    "Identity, schema mapping, read, decision, writeback, receipt, and recovery "
                    "carriers must remain explicit."
                ),
                frame=TestFrameSpec(
                    title="Customer embedding contract carriers",
                    category="structure",
                    purpose="Prevent customer embedding from collapsing into an opaque webhook.",
                    reviewer_guidance=(
                        "Static structure does not prove a customer deployment or production tenant boundary."
                    ),
                    reference="SCENARIO-003 / ARCH-008",
                    failure_target="Read Tenant Context or Governed Customer Writeback",
                ),
                inputs={
                    "tenant_id": "test-tenant",
                    "actor_id": "test-operator",
                    "actor_roles": ["operator"],
                    "request": {"case_id": "case-001"},
                    "connector_profile_id": "test",
                    "connector_authorization_id": "",
                    "connector_idempotency_key": "scenario-controlled-dry-run",
                    "write_mode": "dry_run",
                },
                assertions=[TestAssertion(path=["answer"], operator="exists", structural=True)],
                required_node_types=[
                    "start",
                    "variable_assigner",
                    "connector_action",
                    "llm",
                    "answer",
                ],
                mandatory=True,
                structural_only=True,
                capability_ids=[
                    "F.embedded_request",
                    "F.governed_writeback",
                    "G.tenant_isolation",
                    "G.idempotent_write",
                    "G.compensation",
                    "G.audit",
                ],
            )
        ]
        if not environment_available:
            return cases
        cases.append(
            WorkflowTestCase(
                id="customer_embedding_controlled_dry_run",
                name="Controlled tenant dry-run returns a governed receipt",
                requirement=(
                    "A configured test tenant must execute the read path and preview the writeback "
                    "without applying a mutation."
                ),
                frame=TestFrameSpec(
                    title="Controlled embedding dry-run",
                    category="safety",
                    purpose="Prove tenant routing, schema validation, and adapter bypass for mutation preview.",
                    reviewer_guidance=(
                        "Inspect Connector events to confirm the write adapter was not called."
                    ),
                    reference="SCENARIO-003 / GOV-004",
                    failure_target="Governed Customer Writeback",
                ),
                inputs={
                    "tenant_id": "test-tenant",
                    "actor_id": "test-operator",
                    "actor_roles": ["operator"],
                    "request": {"case_id": "case-001"},
                    "connector_profile_id": "test",
                    "connector_authorization_id": "",
                    "connector_idempotency_key": "scenario-controlled-dry-run",
                    "write_mode": "dry_run",
                },
                assertions=[TestAssertion(path=["answer"], operator="exists", structural=True)],
                required_node_types=["connector_action", "answer"],
                mandatory=True,
                structural_only=False,
                capability_ids=[
                    "F.embedded_request",
                    "F.governed_writeback",
                    "X.customer_schema",
                    "X.customer_writeback",
                ],
            )
        )
        return cases

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
            ),
        ]
