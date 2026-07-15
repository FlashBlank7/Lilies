from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .blocks import BlockRegistry
from .workflow_models import TestAssertion, TestFrameSpec, WorkflowSpec, WorkflowTestCase


class ScenarioEvidenceProfile(BaseModel):
    profile_id: str
    selected_level: Literal["H0", "H1", "H2", "H3", "H4", "H5"]
    status: Literal["design_only", "static_verified", "component_verified", "contract_verified", "live_verified", "production_observed"]
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
    def __init__(self, blocks: BlockRegistry) -> None:
        self.blocks = blocks

    def list(self) -> list[dict[str, Any]]:
        return [self.get("codex_like_workspace_agent").summary()]

    def get(self, scenario_id: str) -> ScenarioDefinition:
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
            capability_set=[
                "workspace_context",
                "planning",
                "registered_tools",
                "permission_boundary",
                "sandbox_boundary",
                "structured_feedback_loop",
                "stop_and_cancel",
                "checkpoint_and_trace",
                "customer_result",
            ],
            required_envelope="E2 local interactive workspace; E3 only when durable cross-process execution is configured",
            external_contracts=[
                "workspace path must resolve inside the configured workspace root",
                "tools must exist in the Lilies registry",
                "network access follows an explicit policy and platform egress controls",
                "mutating work starts only after the plan permission boundary is resolved",
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
            ),
        ]
