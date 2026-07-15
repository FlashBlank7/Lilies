from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from agent_platform.acceptance_repair import AcceptanceRepairPreviewer
from agent_platform.api import create_app
from agent_platform.capability_contracts import (
    AcceptanceEvidenceTarget,
    CarrierStatus,
    CarrierType,
    EvidenceEnvironment,
    EvidenceLevel,
    ExecutionEnvelope,
    RiskLevel,
    VerificationStatus,
    capability_contract_routing,
    evaluate_capability_contract,
    reference_capability_contract,
    reference_capability_contracts,
    render_workflow_build_plan,
)
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.workflow_models import (
    ApplicationCreateRequest,
    ApplicationSnapshot,
    BuildPlan,
    BuildPlanModule,
    BuildTeamState,
    WorkflowTestCase,
)


HEADERS = {"authorization": "Bearer workflow-test"}
ROOT = Path(__file__).resolve().parents[1]


class PayloadProvider(ModelProvider):
    name = "scripted-capability-intake"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, False, False, False, False, 100_000, 20_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({
            "system": system,
            "message": messages[0].content[0].text if messages else "",
            "max_output_tokens": max_output_tokens,
        })
        text = json.dumps(self.payload, ensure_ascii=False)
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 3}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 10},
        })


class PassiveProvider(ModelProvider):
    name = "passive-builder"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": "contract inspected"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": "contract inspected"},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        })


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def test_reference_contracts_produce_distinct_fgx_closures() -> None:
    contracts = reference_capability_contracts()
    assert [item.required_envelope for item in contracts] == [
        ExecutionEnvelope.E2,
        ExecutionEnvelope.E3,
        ExecutionEnvelope.E5,
    ]
    assert [item.risk_level.value for item in contracts] == ["medium", "medium", "high"]
    capability_sets = [{item.id for item in contract.capabilities} for contract in contracts]
    assert len({frozenset(items) for items in capability_sets}) == 3
    assert all(contract.source_requirement for contract in contracts)

    closures = [evaluate_capability_contract(item) for item in contracts]
    assert all(item.valid for item in closures)
    strict_closures = [
        evaluate_capability_contract(item, require_bound_carriers=True)
        for item in contracts
    ]
    assert all(not item.valid for item in strict_closures)
    assert "F.plan_act_observe" in strict_closures[0].unbound_required_capabilities
    assert "G.daily_schedule" in strict_closures[1].unbound_required_capabilities
    assert "G.tenant_isolation" in strict_closures[2].unbound_required_capabilities
    assert closures[0].unavailable_external_contracts == []
    assert closures[1].unavailable_external_contracts == ["X.site_access"]
    assert set(closures[2].unavailable_external_contracts) == {
        "X.customer_identity",
        "X.customer_schema",
        "X.customer_writeback",
        "X.deployment",
    }
    assert closures[1].claim_ceiling == VerificationStatus.blocked_by_environment
    assert closures[2].claim_ceiling == VerificationStatus.blocked_by_environment

    plans = [render_workflow_build_plan(item, locale="en") for item in contracts]
    assert "F.plan_act_observe" in plans[0]
    assert "G.daily_schedule" in plans[1]
    assert "X.customer_writeback" in plans[2]
    assert all("Functional capabilities (F)" in item for item in plans)


def test_closure_separates_dependency_conflict_envelope_and_binding_failures() -> None:
    contract = reference_capability_contract("codex_like_workspace_agent").model_copy(deep=True)
    contract.required_envelope = ExecutionEnvelope.E1
    contract.functional_capabilities[0].requires.append("F.missing_dependency")
    contract.functional_capabilities[0].excludes.append("F.workspace_result")
    contract.carrier_decisions[0].status = CarrierStatus.proposed
    contract.carrier_decisions[0].implementation_refs = []
    contract.carrier_decisions[1].status = CarrierStatus.blocked_by_environment
    contract.carrier_decisions.append(
        contract.carrier_decisions[0].model_copy(
            update={"capability_id": "F.orphan_carrier"}
        )
    )
    contract.platform_coverage.append(
        contract.platform_coverage[0].model_copy(
            update={"capability_id": "F.orphan_coverage"}
        )
    )
    contract.evidence_plan[0].capability_ids.append("F.orphan_evidence")

    non_strict = evaluate_capability_contract(contract)
    strict = evaluate_capability_contract(contract, require_bound_carriers=True)

    assert non_strict.valid is False
    assert non_strict.envelope_sufficient is False
    assert non_strict.missing_dependencies == [{
        "capability_id": "F.plan_act_observe",
        "missing": "F.missing_dependency",
    }]
    assert non_strict.exclusion_conflicts == [["F.plan_act_observe", "F.workspace_result"]]
    assert non_strict.unknown_carrier_decisions == ["F.orphan_carrier"]
    assert non_strict.unknown_coverage_capabilities == ["F.orphan_coverage"]
    assert non_strict.unknown_evidence_capabilities == ["F.orphan_evidence"]
    assert non_strict.invalid_carrier_states == [
        "environment-blocked carrier is not an unavailable external contract: F.workspace_result"
    ]
    assert {
        "F.plan_act_observe",
        "F.workspace_result",
    }.issubset(non_strict.unbound_required_capabilities)
    assert not any("carrier is not bound" in item for item in non_strict.blocking_errors)
    assert any("carrier is not bound: F.plan_act_observe" == item for item in strict.blocking_errors)


def test_contract_routing_keeps_envelope_and_risk_orthogonal() -> None:
    codex = reference_capability_contract("codex_like_workspace_agent")
    routing = capability_contract_routing(codex, requested_planning_mode="disabled")
    assert routing["routing_source"] == "capability_build_contract"
    assert routing["required_envelope"] == "E2"
    assert routing["risk_level"] == "medium"
    assert routing["effective_planning_mode"] == "required"
    assert routing["legacy_complexity_router_used"] is False

    low_envelope_high_risk = codex.model_copy(deep=True)
    low_envelope_high_risk.required_envelope = ExecutionEnvelope.E0
    for capability in low_envelope_high_risk.capabilities:
        capability.required_envelope = ExecutionEnvelope.E0
    low_envelope_high_risk.risk_level = RiskLevel.high
    routed = capability_contract_routing(low_envelope_high_risk)
    assert routed["required_envelope"] == "E0"
    assert routed["risk_level"] == "high"
    assert routed["effective_planning_mode"] == "required"


def test_ai_intake_questions_and_answers_carry_typed_effects(tmp_path: Path) -> None:
    provider = PayloadProvider({
        "status": "needs_input",
        "confidence": 0.6,
        "reasoning_summary": "The tool and environment boundaries are not selected.",
        "detected_goal": "Codex-like workspace workflow",
        "missing": ["tool scope"],
        "questions": [{
            "id": "tool_scope",
            "label": "Tool scope",
            "question": "Which workspace behavior should be included?",
            "why": "It changes F/G/X closure and the required envelope.",
            "decision_axis": "functional_capability",
            "choice_type": "multi",
            "options": [
                {
                    "id": "read_only",
                    "label": "Read and analyze",
                    "description": "Inspect workspace files without mutation.",
                    "impact": "Adds a bounded Read capability at E2.",
                    "recommended": True,
                    "effects": [{
                        "axis": "functional_capability",
                        "target_id": "F.workspace_read",
                        "action": "include",
                        "value": "read registered workspace files",
                    }],
                },
                {
                    "id": "edit_files",
                    "label": "Edit with approval",
                    "description": "Allow bounded file edits after approval.",
                    "impact": "Adds mutation and permission guarantees.",
                    "recommended": False,
                    "effects": [
                        {
                            "axis": "functional_capability",
                            "target_id": "F.workspace_edit",
                            "action": "include",
                            "value": "edit workspace files",
                        },
                        {
                            "axis": "runtime_guarantee",
                            "target_id": "G.permission_boundary",
                            "action": "require",
                            "value": "approval before mutation",
                        },
                    ],
                },
            ],
        }],
        "completed_requirement": None,
        "workflow_intent": {},
    })
    with TestClient(create_app(settings(tmp_path), provider)) as client:
        response = client.post(
            "/api/v1/requirements/complete",
            headers=HEADERS,
            json={"requirement": "做一个工作流可以像 Codex 一样", "locale": "zh"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        question = body["questions"][0]
        assert question["decision_axis"] == "functional_capability"
        assert question["options"][1]["effects"][1]["target_id"] == "G.permission_boundary"
        assert "functional capability (F)" in provider.calls[0]["system"]
        assert "CapabilityBuildContract JSON schema" in provider.calls[0]["system"]
        assert provider.calls[0]["max_output_tokens"] == 12_000


def test_ai_ready_response_renders_plan_from_model_contract(tmp_path: Path) -> None:
    contract = reference_capability_contract("codex_like_workspace_agent").model_dump(mode="json")
    provider = PayloadProvider({
        "status": "ready",
        "confidence": 0.94,
        "reasoning_summary": "Selections close the F/G/X decisions.",
        "detected_goal": "Codex-like workspace workflow",
        "missing": [],
        "questions": [],
        "completed_requirement": None,
        "workflow_intent": {},
        "capability_build_contract": contract,
    })
    requirement = "做一个工作流可以像 Codex 一样，并在修改前请求权限"
    with TestClient(create_app(settings(tmp_path), provider)) as client:
        response = client.post(
            "/api/v1/requirements/complete",
            headers=HEADERS,
            json={
                "requirement": requirement,
                "locale": "zh",
                "answers": [{
                    "question_id": "tool_scope",
                    "question": "Which scope?",
                    "choice_type": "multi",
                    "selected_option_ids": ["edit_files"],
                    "selected_options": [{
                        "id": "edit_files",
                        "label": "Edit with approval",
                        "effects": [{
                            "axis": "runtime_guarantee",
                            "target_id": "G.permission_boundary",
                            "action": "require",
                            "value": "approval before mutation",
                        }],
                    }],
                }],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["capability_build_contract"]["generation_source"] == "model"
        assert body["capability_build_contract"]["source_requirement"] == requirement
        assert body["capability_closure"]["valid"] is True
        assert body["completed_requirement"].startswith("# 工作流搭建方案")
        assert "F.plan_act_observe" in body["completed_requirement"]
        assert "E2" in body["completed_requirement"]
        assert "G.permission_boundary" in str(provider.calls[0]["message"])


def test_contract_persists_in_draft_version_and_drives_build_routing(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), PassiveProvider())
    contract = reference_capability_contract("daily_web_collection")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={
                "name": "Daily collection",
                "requirement": contract.source_requirement,
                "delivery_mode": "quick",
                "capability_build_contract": contract.model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        application_id = created.json()["id"]
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        original_hash = draft["content_hash"]
        assert draft["snapshot"]["capability_build_contract"]["contract_id"] == contract.contract_id

        read_contract = client.get(
            f"/api/v1/applications/{application_id}/capability-contract",
            headers=HEADERS,
        )
        assert read_contract.status_code == 200, read_contract.text
        assert read_contract.json()["closure"]["unavailable_external_contracts"] == ["X.site_access"]

        validated_contract = client.post(
            "/api/v1/capability-contracts/validate",
            headers=HEADERS,
            json=contract.model_dump(mode="json"),
        )
        assert validated_contract.status_code == 200, validated_contract.text
        assert validated_contract.json()["computed_envelope"] == "E3"

        reference_scenarios = client.get(
            "/api/v1/capability-contracts/reference-scenarios",
            headers=HEADERS,
        )
        assert reference_scenarios.status_code == 200, reference_scenarios.text
        assert [
            item["contract"]["required_envelope"]
            for item in reference_scenarios.json()
        ] == ["E2", "E3", "E5"]

        updated = contract.model_copy(deep=True)
        updated.business_goal += " Preserve a per-source provenance receipt."
        mutation = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
            json={
                "expected_revision": draft["revision"],
                "idempotency_key": "update-capability-contract",
                "op": "set_capability_build_contract",
                "data": {"contract": updated.model_dump(mode="json")},
            },
        )
        assert mutation.status_code == 200, mutation.text
        assert mutation.json()["content_hash"] != original_hash

        build = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={"requirement": updated.source_requirement, "auto_publish": False},
        )
        assert build.status_code == 202, build.text
        routing = build.json()["capability_routing"]
        assert build.json()["routing_source"] == "capability_build_contract"
        assert routing["required_envelope"] == "E3"
        assert routing["risk_level"] == "medium"
        assert routing["legacy_complexity_router_used"] is False
        build_record = client.get(
            f"/api/v1/builds/{build.json()['build_id']}",
            headers=HEADERS,
        )
        assert build_record.status_code == 200, build_record.text
        assert (
            build_record.json()["team_state"]["capability_build_contract"]["contract_id"]
            == contract.contract_id
        )
        assert build_record.json()["team_state"]["capability_routing"]["required_envelope"] == "E3"

        published = client.post(
            f"/api/v1/applications/{application_id}/versions",
            headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert published.status_code == 200, published.text
        database = sqlite3.connect(settings(tmp_path).data_dir / "agent_platform.db")
        database.row_factory = sqlite3.Row
        try:
            row = database.execute(
                "SELECT snapshot_json FROM application_versions WHERE application_id=? AND version=1",
                (application_id,),
            ).fetchone()
        finally:
            database.close()
        assert row is not None
        version_snapshot = json.loads(row["snapshot_json"])
        assert version_snapshot["capability_build_contract"]["business_goal"].endswith(
            "Preserve a per-source provenance receipt."
        )


def test_builder_binds_real_carriers_and_refuses_incomplete_contract(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = create_app(settings(tmp_path), PassiveProvider())
        services = app.state.services
        await services.storage.initialize()
        await services.workflow_store.initialize()
        scenario = services.scenarios.get("codex_like_workspace_agent")
        binding_refs = {
            item.capability_id: item.implementation_refs
            for item in scenario.capability_build_contract.carrier_decisions
        }
        contract = reference_capability_contract("codex_like_workspace_agent").model_copy(deep=True)
        application = await services.workflow_store.create_application(
            ApplicationCreateRequest(
                name="Builder contract",
                requirement=contract.source_requirement,
                capability_build_contract=contract,
            )
        )
        draft = await services.workflow_store.get_draft(application["id"])
        await services.applications.apply_operations_atomically(
            application["id"],
            expected_revision=draft["revision"],
            expected_content_hash=draft["content_hash"],
            idempotency_key="install-real-codex-workflow",
            change_context_operation="scenario_apply_test",
            operations=[{
                "op": "replace_workflow",
                "data": {"workflow": scenario.workflow.model_dump(mode="json")},
            }],
        )
        state = BuildTeamState(
            planning_mode="required",
            capability_build_contract=contract,
            capability_closure=evaluate_capability_contract(contract).model_dump(mode="json"),
            capability_routing=capability_contract_routing(contract),
        )
        builder = services.builder
        definitions = {item.name: item for item in builder._definitions(allow_team=False, planning_mode="required")}
        assert "capability_contract" in definitions
        assert "bind" in definitions["capability_contract"].input_schema["properties"]["action"]["enum"]

        inspected = await builder._execute(
            "build-contract-test",
            application["id"],
            state,
            "capability_contract",
            {"action": "get"},
            max_repair_cycles=2,
            auto_publish=False,
        )
        assert inspected["contract"]["contract_id"] == contract.contract_id
        assert any(
            item["reference"] == "codex_loop"
            for item in inspected["resource_inventory"]["workflow_nodes"]
        )

        plan = BuildPlan(
            goal="Build every required capability",
            strategy="Bind carrier before completion.",
            capability_contract_id=contract.contract_id,
            claim_scope="Local component boundary only.",
            modules=[BuildPlanModule(
                id="all-capabilities",
                title="Capability closure",
                capability_ids=[item.id for item in contract.capabilities if item.required],
                carrier_type=CarrierType.reusable_module,
            )],
        )
        await builder._execute(
            "build-contract-test",
            application["id"],
            state,
            "build_plan",
            {"action": "set", "plan": plan.model_dump(mode="json")},
            max_repair_cycles=2,
            auto_publish=False,
        )
        with pytest.raises(ValueError, match="not present in the draft or registered inventory"):
            await builder._execute(
                "build-contract-test",
                application["id"],
                state,
                "capability_contract",
                {
                    "action": "bind",
                    "capability_id": "F.plan_act_observe",
                    "status": "bound",
                    "implementation_refs": ["module:invented-but-not-registered"],
                },
                max_repair_cycles=2,
                auto_publish=False,
            )
        with pytest.raises(RuntimeError, match="carrier is not bound"):
            await builder._execute(
                "build-contract-test",
                application["id"],
                state,
                "capability_contract",
                {"action": "validate", "require_bound": True},
                max_repair_cycles=2,
                auto_publish=False,
            )

        for decision in contract.carrier_decisions:
            await builder._execute(
                "build-contract-test",
                application["id"],
                state,
                "capability_contract",
                {
                    "action": "bind",
                    "capability_id": decision.capability_id,
                    "status": "bound",
                    "implementation_refs": binding_refs[decision.capability_id],
                },
                max_repair_cycles=2,
                auto_publish=False,
            )
        validated = await builder._execute(
            "build-contract-test",
            application["id"],
            state,
            "capability_contract",
            {"action": "validate", "require_bound": True},
            max_repair_cycles=2,
            auto_publish=False,
        )
        assert validated["valid"] is True
        assert validated["closure"]["valid"] is True

    asyncio.run(exercise())


def test_acceptance_repair_preserves_capability_and_claim_context(tmp_path: Path) -> None:
    contract = reference_capability_contract("daily_web_collection")
    test = WorkflowTestCase(
        id="daily-provenance",
        name="Daily provenance",
        requirement="Every collected item has source provenance.",
        capability_ids=["G.provenance", "X.site_access"],
        evidence_target=AcceptanceEvidenceTarget(
            level=EvidenceLevel.H2,
            environment=EvidenceEnvironment.contract,
            expected_status=VerificationStatus.blocked_by_environment,
            claim_scope="Contract and provenance structure without live site access.",
        ),
        required_node_types=["start", "answer"],
    )
    snapshot = ApplicationSnapshot(
        name="Daily repair",
        description="Repair preview for a capability-bound daily collection workflow.",
        requirement=contract.source_requirement,
        capability_build_contract=contract,
        tests=[test],
    )
    preview = AcceptanceRepairPreviewer(
        create_app(settings(tmp_path), PassiveProvider()).state.services.blocks
    ).preview(
        snapshot,
        revision=2,
        content_hash="abc",
        report={"tests": [{"test_id": test.id, "name": test.name, "passed": False}]},
        test_id=test.id,
    )
    context = preview.repair_context
    assert context.capability_ids == ["G.provenance", "X.site_access"]
    assert context.evidence_target["expected_status"] == "blocked_by_environment"
    assert context.capability_contract_id == contract.contract_id
    assert context.required_envelope == "E3"
    assert context.claim_ceiling == "blocked_by_environment"
    assert context.external_contract_gaps == ["X.site_access"]
    assert "G.provenance" in preview.instruction


def test_frontend_carries_option_effects_and_contract_into_creation() -> None:
    home = (ROOT / "platform/frontend/app/page.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "platform/frontend/app/globals.css").read_text(encoding="utf-8")
    assert "effects: option.effects || []" in home
    assert "question.decision_axis" in home
    assert "data-capability-build-contract" in home
    assert "data-capability-kind={kind}" in home
    assert "data-capability-ownership=\"separated-harness-layers\"" in home
    assert "data-capability-coverage-owner={coverage.owner}" in home
    assert "data-capability-evidence-plan=\"scoped\"" in home
    assert "'workflow_runtime' | 'evaluation_harness' | 'platform_harness' | 'external_system'" in home
    assert "capability_build_contract: capabilityBuildContract" in home
    assert "isCodexWorkspaceRequirement(requirement) && !capabilityBuildContract" in home
    assert ".capability-build-contract" in styles
    assert ".capability-contract-ownership" in styles
    assert ".requirement-option-effects" in styles
