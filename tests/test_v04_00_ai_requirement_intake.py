from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import (
    RequirementIntakeRequest,
    _normalize_requirement_intake_payload,
    create_app,
)
from agent_platform.capability_contracts import (
    CapabilityBuildContract,
    evaluate_capability_contract,
    reference_capability_contract,
)
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


ROOT = Path(__file__).resolve().parents[1]


class IntakeProvider(ModelProvider):
    name = "scripted-intake"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, False, False, False, False, 100_000, 8_000)

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
            "model": model,
            "system": system,
            "message": messages[0].content[0].text if messages and messages[0].content else "",
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "effort": effort,
            "user_id": user_id,
        })
        text = json.dumps(self.payload, ensure_ascii=False)
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 11}}})
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 17}})


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def complaint_contract_payload() -> dict[str, object]:
    capability_ids = [
        "F.classify",
        "F.reply",
        "F.review_approve",
        "G.trace",
        "X.no_external_call",
    ]
    return {
        "schema_version": "1.0",
        "contract_id": "complaint-intake-contract",
        "generation_source": "model",
        "source_requirement": "placeholder",
        "target_user": "客服主管",
        "business_goal": "判断投诉类型并生成中文回复建议",
        "start_inputs": [{
            "name": "complaint_text",
            "label": "投诉内容",
            "value_type": "string",
            "required": True,
            "description": "一条客户投诉",
        }],
        "functional_capabilities": [
            {
                "id": "F.classify",
                "title": "投诉分类",
                "description": "判断紧急程度和问题类型",
                "required_envelope": "E1",
                "outputs": ["urgency", "issue_type"],
            },
            {
                "id": "F.reply",
                "title": "回复建议",
                "description": "生成中文回复建议和下一步",
                "required_envelope": "E1",
                "outputs": ["reply", "next_step"],
            },
            {
                "id": "F.review_approve",
                "title": "人工审核与确认",
                "description": "主管审核并确认结果后再输出",
                "required_envelope": "E1",
                "outputs": ["approved_result"],
            },
        ],
        "runtime_guarantees": [{
            "id": "G.trace",
            "title": "步骤追踪",
            "description": "记录处理步骤和结构化 step_log",
            "required_envelope": "E1",
            "guarantee_type": "audit",
        }],
        "external_contracts": [{
            "id": "X.no_external_call",
            "title": "无外部系统依赖",
            "description": "工作流不调用外部系统",
            "availability": "not_required",
            "availability_reason": "用户要求不调用外部系统",
        }],
        "required_envelope": "E1",
        "risk_level": "low",
        "risk_reasons": ["纯文本处理", "人工审核降低风险"],
        "carrier_decisions": [
            {
                "capability_id": "F.classify",
                "carrier_type": "atomic_block",
                "resource_hint": "model:classify",
                "rationale": "单独分类便于追踪",
            },
            {
                "capability_id": "F.reply",
                "carrier_type": "atomic_block",
                "resource_hint": "model:reply",
                "rationale": "单独生成回复",
            },
            {
                "capability_id": "F.review_approve",
                "carrier_type": "platform_control",
                "resource_hint": "platform:permission_gate",
                "rationale": "人工确认",
            },
            {
                "capability_id": "G.trace",
                "carrier_type": "platform_control",
                "resource_hint": "node:event_recorder",
                "rationale": "独立记录步骤",
            },
            {
                "capability_id": "X.no_external_call",
                "carrier_type": "connector_external_contract",
                "resource_hint": "contract:no_external",
                "rationale": "声明无外部依赖",
            },
        ],
        "platform_coverage": [
            {
                "capability_id": capability_id,
                "owner": (
                    "external_system"
                    if capability_id.startswith("X.")
                    else "platform_harness"
                    if capability_id in {"F.review_approve", "G.trace"}
                    else "workflow_runtime"
                ),
                "status": "partial",
                "surface": f"intake:{capability_id}",
            }
            for capability_id in capability_ids
        ],
        "evidence_plan": [{
            "capability_ids": capability_ids,
            "target_level": "H1",
            "environment": "sandbox",
            "expected_status": "component_verified",
            "required_evidence": ["输出字段完整", "步骤可追踪"],
            "claim_scope": "沙箱组件验收",
        }],
        "workflow_outline": [
            "接收投诉",
            "调用LLM执行 F.classify 判断类型",
            "调用LLM执行 F.reply 生成回复",
            "F.review_approve 人工审核确认",
            "输出结果",
        ],
        "runtime_interface": "输入投诉后自动处理，到达人工审核步骤时等待主管确认。",
        "claim_scope": {
            "ceiling": "component_verified",
            "verified": ["投诉分类", "人工审核确认"],
            "excluded": [],
        },
        "unresolved_decisions": [],
    }


def test_v04_00_requirement_intake_asks_option_based_codex_like_questions(tmp_path: Path) -> None:
    provider = IntakeProvider({
        "status": "needs_input",
        "confidence": 0.72,
        "reasoning_summary": "The request names Codex but not the intended Codex-like behavior or user.",
        "detected_goal": "Build an editable workflow that behaves like selected Codex capabilities.",
        "missing": ["Codex-like capability boundary", "target user", "tool and permission scope"],
        "questions": [
            {
                "id": "codex_capability_scope",
                "label": "Codex-like capability",
                "question": "Which Codex behavior should this workflow reproduce: planning, workspace reading, code editing, tool execution, tests, or permissions?",
                "why": "Lilies must know which mechanism becomes editable workflow blocks.",
                "choice_type": "multi",
                "options": [
                    {
                        "id": "plan_and_clarify",
                        "label": "Plan and clarify first",
                        "description": "Ask selection-style questions before building.",
                        "impact": "Adds requirement intake and plan blocks before execution.",
                        "recommended": True,
                    },
                    {
                        "id": "permission_gates",
                        "label": "Permission gates",
                        "description": "Require approval before real tools or risky actions.",
                        "impact": "Adds permission and sandbox blocks.",
                        "recommended": False,
                    },
                    {
                        "id": "acceptance_evidence",
                        "label": "Acceptance evidence",
                        "description": "Run checks and show evidence before delivery.",
                        "impact": "Adds acceptance test and trace output blocks.",
                        "recommended": False,
                    },
                    {
                        "id": "workspace_context",
                        "label": "Workspace context",
                        "description": "Read project context before planning.",
                        "impact": "Adds context assembly blocks.",
                        "recommended": False,
                    },
                    {
                        "id": "tool_execution",
                        "label": "Tool execution",
                        "description": "Run external tools when permitted.",
                        "impact": "Adds tool gateway blocks.",
                        "recommended": False,
                    },
                    {
                        "id": "editable_blocks",
                        "label": "Editable blocks",
                        "description": "Represent behavior as editable workflow blocks.",
                        "impact": "Adds block-generation and canvas-editing steps.",
                        "recommended": False,
                    },
                ],
                "custom_allowed": True,
                "custom_placeholder": "Describe any other Codex-like behavior.",
            }
        ],
        "completed_requirement": None,
        "workflow_intent": {"core_steps": []},
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)
    with TestClient(app) as client:
        result = client.post("/api/v1/requirements/complete", headers=headers(), json={
            "requirement": "做一个工作流可以像codex一样",
            "locale": "zh",
            "max_questions": 5,
        })
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "needs_input"
        assert body["completed_requirement"] is None
        assert "Codex" in body["questions"][0]["question"]
        assert body["questions"][0]["choice_type"] == "multi"
        assert len(body["questions"][0]["options"]) == 5
        assert body["questions"][0]["options"][0]["recommended"] is True
        assert provider.calls
        assert "Claude Code plan-mode questioning" in str(provider.calls[0]["system"])
        assert "Every needs_input question must include 2 to 5 concrete selectable options" in str(provider.calls[0]["system"])
        assert "explicit absence of external dependencies is a claim-scope exclusion" in str(provider.calls[0]["system"])
        assert "Step traceability is supplied by workflow-runtime node events" in str(provider.calls[0]["system"])
        assert "never concatenate multiple capability ids into one field" in str(provider.calls[0]["system"])
        assert "做一个工作流可以像codex一样" in str(provider.calls[0]["message"])
        tasks = client.get("/api/v1/platform/harness/tasks", headers=headers(), params={"kind": "requirement_intake"}).json()
        assert tasks[0]["status"] == "succeeded"
        assert tasks[0]["usage_counts"]["model_call"] == 1


def test_v04_00_needs_input_ignores_premature_contract_and_keeps_interface_effects(
    tmp_path: Path,
) -> None:
    provider = IntakeProvider({
        "status": "needs_input",
        "confidence": 0.78,
        "reasoning_summary": "The runtime interface and target user still need a decision.",
        "detected_goal": "Build a complaint triage workflow.",
        "missing": ["runtime interface"],
        "questions": [{
            "id": "runtime_interface",
            "label": "运行界面",
            "question": "使用者从哪里启动并查看结果？",
            "decision_axis": "runtime_interface",
            "choice_type": "single",
            "options": [
                {
                    "id": "customer_runtime",
                    "label": "客户运行界面",
                    "description": "使用独立运行页面。",
                    "impact": "显示输入、进度和结果。",
                    "recommended": True,
                    "effects": [{
                        "axis": "runtime_interface",
                        "target_id": "runtime.customer",
                        "action": "configure",
                        "value": "customer_runtime",
                    }],
                },
                {
                    "id": "api_only",
                    "label": "仅 API",
                    "description": "由其他系统调用。",
                    "impact": "不提供客户页面。",
                    "recommended": False,
                    "effects": [{
                        "axis": "target_user",
                        "target_id": "user.integrator",
                        "action": "configure",
                        "value": "system_integrator",
                    }],
                },
            ],
        }],
        "completed_requirement": "This premature plan must be ignored.",
        "workflow_intent": None,
        "capability_build_contract": {},
        "capability_closure": {},
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)

    with TestClient(app) as client:
        response = client.post("/api/v1/requirements/complete", headers=headers(), json={
            "requirement": "为客服主管制作投诉分类和回复建议工作流",
            "locale": "zh",
        })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_input"
    assert body["completed_requirement"] is None
    assert body["capability_build_contract"] is None
    assert body["capability_closure"] is None
    assert body["workflow_intent"] == {}
    assert body["questions"][0]["options"][0]["effects"][0]["axis"] == "runtime_interface"
    assert body["questions"][0]["options"][1]["effects"][0]["axis"] == "target_user"


def test_v04_00_requirement_intake_returns_ready_completed_requirement(tmp_path: Path) -> None:
    completed = (
        "目标：为内部工程师创建 Codex-like 工作流。\n"
        "运行输入：任务描述和可选仓库上下文。\n"
        "步骤：澄清需求、读取上下文、生成计划、请求权限、执行可编辑积木、运行验收。\n"
        "验收：至少覆盖澄清、权限拒绝、测试通过三个场景。"
    )
    provider = IntakeProvider({
        "status": "ready",
        "confidence": 0.91,
        "reasoning_summary": "The answers define user, scope, interface, permissions, and acceptance.",
        "detected_goal": "Codex-like workflow editor assistant.",
        "missing": [],
        "questions": [],
        "completed_requirement": completed,
        "workflow_intent": {
            "target_user": "内部工程师",
            "runtime_input": "任务描述和仓库上下文",
            "runtime_output": "计划、变更摘要、验收证据",
            "permissions": ["修改文件前确认", "网络访问留证据"],
            "acceptance_cases": ["澄清缺失信息", "拒绝高风险动作", "运行测试"],
        },
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)
    with TestClient(app) as client:
        result = client.post("/api/v1/requirements/complete", headers=headers(), json={
            "requirement": "做一个工作流可以像codex一样",
            "locale": "zh",
            "answers": [{
                "question_id": "codex_capability_scope",
                "question": "Which Codex behavior should this workflow reproduce?",
                "choice_type": "multi",
                "selected_option_ids": ["plan_and_clarify", "permission_gates", "acceptance_evidence"],
                "selected_options": [
                    {
                        "id": "plan_and_clarify",
                        "label": "Plan and clarify first",
                        "description": "Ask selection-style questions before building.",
                        "impact": "Adds requirement intake and plan blocks before execution.",
                    },
                    {
                        "id": "permission_gates",
                        "label": "Permission gates",
                        "description": "Require approval before real tools or risky actions.",
                        "impact": "Adds permission and sandbox blocks.",
                    },
                    {
                        "id": "acceptance_evidence",
                        "label": "Acceptance evidence",
                        "description": "Run checks and show evidence before delivery.",
                        "impact": "Adds acceptance test and trace output blocks.",
                    },
                ],
                "custom_answer": "运行态输出要面向工作流使用者，而不是代码提交者。",
            }],
        })
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "ready"
        assert body["completed_requirement"].startswith("# 工作流搭建方案")
        assert "内部工程师" in body["completed_requirement"]
        assert "F.legacy_goal" in body["completed_requirement"]
        assert body["capability_build_contract"]["generation_source"] == "legacy_compatibility"
        assert body["capability_build_contract"]["source_requirement"] == "做一个工作流可以像codex一样"
        assert body["capability_closure"]["valid"] is True
        assert body["questions"] == []
        prompt = str(provider.calls[0]["message"])
        assert "selected_option_ids" in prompt
        assert "Plan and clarify first" in prompt
        assert "运行态输出要面向工作流使用者" in prompt
        assert "answered_question_ids" in prompt
        assert "answered_decision_axes" in prompt
        assert "prior_answers contains the cumulative selections" in prompt


def test_v04_00_ready_contract_completes_omitted_internal_scaffolding(tmp_path: Path) -> None:
    contract = reference_capability_contract("codex_like_workspace_agent").model_dump(mode="json")
    guarantee_id = contract["runtime_guarantees"][0]["id"]
    contract["carrier_decisions"] = [
        item for item in contract["carrier_decisions"]
        if item["capability_id"] != guarantee_id
    ]
    contract["platform_coverage"] = [
        item for item in contract["platform_coverage"]
        if item["capability_id"] != guarantee_id
    ]
    contract["evidence_plan"] = [
        item for item in contract["evidence_plan"]
        if guarantee_id not in item["capability_ids"]
    ]
    provider = IntakeProvider({
        "status": "ready",
        "confidence": 0.9,
        "reasoning_summary": "The declared workflow is ready to build.",
        "detected_goal": "Build a bounded workspace agent.",
        "missing": [],
        "questions": [],
        "completed_requirement": None,
        "workflow_intent": None,
        "capability_build_contract": contract,
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)

    with TestClient(app) as client:
        response = client.post("/api/v1/requirements/complete", headers=headers(), json={
            "requirement": "Build a Codex-like workflow inside a selected workspace.",
            "locale": "en",
        })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["capability_closure"]["valid"] is True
    assert guarantee_id not in body["capability_closure"]["missing_carrier_decisions"]
    assert guarantee_id not in body["capability_closure"]["missing_coverage"]
    assert guarantee_id not in body["capability_closure"]["missing_evidence_plan"]
    assert any(
        guarantee_id in decision
        for decision in body["capability_build_contract"]["unresolved_decisions"]
    )


def test_v04_11_no_external_intake_recommends_automatic_path() -> None:
    body = RequirementIntakeRequest(
        requirement="处理一条客户投诉并生成回复，不调用外部系统。",
        locale="zh",
    )
    payload = {
        "status": "needs_input",
        "workflow_intent": {},
        "questions": [{
            "id": "review_mode",
            "label": "运行方式",
            "question": "生成建议后如何交付？",
            "decision_axis": "runtime_guarantee",
            "choice_type": "single",
            "options": [
                {
                    "id": "human_review",
                    "label": "主管人工审核",
                    "description": "每次都等待主管确认。",
                    "impact": "增加审批门禁。",
                    "recommended": True,
                    "effects": [{
                        "axis": "runtime_guarantee",
                        "target_id": "G.review",
                        "action": "include",
                        "value": "human approval gate",
                    }],
                },
                {
                    "id": "automatic",
                    "label": "自动执行，无需人工审核",
                    "description": "直接展示结构化结果。",
                    "impact": "保持单次低风险文本处理。",
                    "recommended": False,
                    "effects": [{
                        "axis": "runtime_guarantee",
                        "target_id": "G.review",
                        "action": "exclude",
                        "value": "no human review",
                    }],
                },
            ],
        }],
    }

    normalized = _normalize_requirement_intake_payload(payload, body)

    options = normalized["questions"][0]["options"]
    assert options[0]["recommended"] is False
    assert options[1]["recommended"] is True
    assert sum(bool(option["recommended"]) for option in options) == 1


def test_v04_11_intake_normalizes_model_effect_protocol_synonyms(
    tmp_path: Path,
) -> None:
    provider = IntakeProvider({
        "status": "needs_input",
        "confidence": 0.68,
        "reasoning_summary": "The permission boundary needs a customer decision.",
        "detected_goal": "Build a governed workflow.",
        "missing": ["permission boundary"],
        "questions": [{
            "id": "permission_boundary",
            "label": "权限边界",
            "question": "工作流可以使用哪些权限？",
            "why": "权限会改变工作流的运行范围。",
            "decision_axis": "permission_boundary",
            "choice_type": "single",
            "options": [
                {
                    "id": "submitted_text_only",
                    "label": "仅处理提交文本",
                    "description": "不访问其他资源。",
                    "impact": "保持最低权限。",
                    "recommended": True,
                    "effects": [{
                        "axis": "permissions",
                        "target_id": "G.permission_boundary",
                        "action": "set",
                        "value": "submitted text only",
                    }],
                },
                {
                    "id": "no_network",
                    "label": "禁用网络",
                    "description": "明确排除网络访问。",
                    "impact": "增加网络排除边界。",
                    "recommended": False,
                    "effects": [{
                        "axis": "permission",
                        "target_id": "G.network",
                        "action": "disable",
                        "value": "network access",
                    }],
                },
            ],
        }],
        "workflow_intent": {},
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)

    with TestClient(app) as client:
        result = client.post(
            "/api/v1/requirements/complete",
            headers=headers(),
            json={
                "requirement": "创建一个需要明确权限边界的工作流",
                "locale": "zh",
                "max_questions": 5,
            },
        )

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["questions"][0]["decision_axis"] == "permission_boundary"
    effects = [
        option["effects"][0]
        for option in body["questions"][0]["options"]
    ]
    assert effects[0]["axis"] == "permission_boundary"
    assert effects[0]["action"] == "configure"
    assert effects[1]["axis"] == "permission_boundary"
    assert effects[1]["action"] == "exclude"


def test_v04_11_no_external_ready_contract_removes_inventions_and_shares_carrier() -> None:
    body = RequirementIntakeRequest(
        requirement=(
            "为客服主管创建工作流：输入投诉，判断紧急程度和问题类型，"
            "生成中文回复建议和下一步；不调用外部系统；输出完整且步骤可追踪。"
        ),
        locale="zh",
    )
    payload = {
        "status": "ready",
        "workflow_intent": {},
        "capability_build_contract": complaint_contract_payload(),
    }

    normalized = _normalize_requirement_intake_payload(payload, body)
    contract = normalized["capability_build_contract"]

    assert {item["id"] for item in contract["functional_capabilities"]} == {
        "F.classify",
        "F.reply",
    }
    assert contract["external_contracts"] == []
    assert all(
        "F.review_approve" not in item["capability_ids"]
        and "X.no_external_call" not in item["capability_ids"]
        for item in contract["evidence_plan"]
    )
    decisions = {
        item["capability_id"]: item
        for item in contract["carrier_decisions"]
    }
    assert decisions["F.classify"]["resource_hint"] == "shared:model_turn:structured_workflow_result"
    assert decisions["F.reply"]["resource_hint"] == decisions["F.classify"]["resource_hint"]
    assert decisions["G.trace"]["carrier_type"] == "runtime_service"
    assert decisions["G.trace"]["resource_hint"] == "runtime:workflow_runtime"
    assert all("人工审核" not in item for item in contract["workflow_outline"])
    assert sum("结构化模型步骤" in item for item in contract["workflow_outline"]) == 1
    assert "外部系统集成" in contract["claim_scope"]["excluded"]
    assert evaluate_capability_contract(CapabilityBuildContract.model_validate(contract)).valid


def test_v04_11_no_external_requirement_does_not_recommend_local_file_access() -> None:
    body = RequirementIntakeRequest(
        requirement="输入一条客户投诉并生成回复，不调用外部系统。",
        locale="zh",
    )
    payload = {
        "status": "needs_input",
        "workflow_intent": {},
        "questions": [{
            "id": "permission_boundary",
            "label": "权限边界",
            "question": "工作流需要哪些额外权限？",
            "why": "确认运行边界。",
            "decision_axis": "permission_boundary",
            "choice_type": "single",
            "custom_allowed": False,
            "options": [
                {
                    "id": "local_files",
                    "label": "允许读取本地文件",
                    "description": "工作流可以访问工作区文件。",
                    "impact": "增加文件读取权限。",
                    "recommended": True,
                },
                {
                    "id": "submitted_text_only",
                    "label": "仅处理提交文本",
                    "description": "只使用客户在运行界面提交的文本。",
                    "impact": "不增加文件、网络或工具权限。",
                    "recommended": False,
                },
            ],
        }],
    }

    normalized = _normalize_requirement_intake_payload(payload, body)
    options = normalized["questions"][0]["options"]

    assert options[0]["recommended"] is False
    assert options[1]["recommended"] is True


def test_v04_11_explicit_review_selection_is_preserved() -> None:
    body = RequirementIntakeRequest.model_validate({
        "requirement": "处理客户投诉并生成回复，不调用外部系统。",
        "locale": "zh",
        "answers": [{
            "question_id": "review_mode",
            "selected_option_ids": ["human_review"],
            "selected_options": [{
                "id": "human_review",
                "label": "主管人工审核与确认",
                "description": "输出前由主管审核。",
                "impact": "增加人工确认步骤。",
            }],
        }],
    })
    payload = {
        "status": "ready",
        "workflow_intent": {},
        "capability_build_contract": complaint_contract_payload(),
    }

    normalized = _normalize_requirement_intake_payload(payload, body)
    contract = normalized["capability_build_contract"]

    assert "F.review_approve" in {
        item["id"] for item in contract["functional_capabilities"]
    }
    assert any("人工审核" in item for item in contract["workflow_outline"])
    assert contract["external_contracts"] == []


def test_v04_11_combined_carrier_ids_are_split_and_duplicate_trace_f_is_removed() -> None:
    contract = complaint_contract_payload()
    contract["functional_capabilities"].append({
        "id": "F.step_trace",
        "title": "步骤追踪",
        "description": "把运行过程记录成 step_log",
        "required_envelope": "E0",
        "outputs": ["step_log"],
    })
    contract["carrier_decisions"] = [
        item
        for item in contract["carrier_decisions"]
        if item["capability_id"] not in {"F.classify", "F.reply"}
    ]
    contract["carrier_decisions"].append({
        "capability_id": "F.classify F.reply F.step_trace",
        "carrier_type": "atomic_block",
        "resource_hint": "single_llm_node",
        "rationale": "三个功能共享一个结构化模型节点",
    })
    contract["platform_coverage"].append({
        "capability_id": "F.step_trace",
        "owner": "workflow_runtime",
        "status": "available",
        "surface": "runtime step_log",
    })
    contract["evidence_plan"][0]["capability_ids"].append("F.step_trace")
    body = RequirementIntakeRequest(
        requirement="分析客户投诉并生成回复，不调用外部系统，步骤可追踪。",
        locale="zh",
    )

    normalized = _normalize_requirement_intake_payload(
        {
            "status": "ready",
            "workflow_intent": {},
            "capability_build_contract": contract,
        },
        body,
    )
    ready_contract = normalized["capability_build_contract"]
    capability_ids = {
        item["id"]
        for key in (
            "functional_capabilities",
            "runtime_guarantees",
            "external_contracts",
        )
        for item in ready_contract[key]
    }
    decision_ids = {
        item["capability_id"]
        for item in ready_contract["carrier_decisions"]
    }

    assert "F.step_trace" not in capability_ids
    assert "F.classify" in decision_ids
    assert "F.reply" in decision_ids
    assert decision_ids <= capability_ids
    assert all(" " not in capability_id for capability_id in decision_ids)
    assert evaluate_capability_contract(
        CapabilityBuildContract.model_validate(ready_contract)
    ).valid


def test_v04_11_explicit_trace_request_restores_omitted_runtime_guarantee() -> None:
    contract = complaint_contract_payload()
    contract["runtime_guarantees"] = []
    contract["carrier_decisions"] = [
        item
        for item in contract["carrier_decisions"]
        if item["capability_id"] != "G.trace"
    ]
    contract["platform_coverage"] = [
        item
        for item in contract["platform_coverage"]
        if item["capability_id"] != "G.trace"
    ]
    for evidence in contract["evidence_plan"]:
        evidence["capability_ids"] = [
            item
            for item in evidence["capability_ids"]
            if item != "G.trace"
        ]
    body = RequirementIntakeRequest(
        requirement="分析客户投诉并生成回复，不调用外部系统，输出完整且步骤可追踪。",
        locale="zh",
    )

    normalized = _normalize_requirement_intake_payload(
        {
            "status": "ready",
            "workflow_intent": {},
            "capability_build_contract": contract,
        },
        body,
    )
    ready_contract = normalized["capability_build_contract"]
    trace_guarantees = [
        item
        for item in ready_contract["runtime_guarantees"]
        if item["guarantee_type"] == "observability"
        and "追踪" in f"{item['title']} {item['description']}"
    ]

    assert len(trace_guarantees) == 1
    trace_id = trace_guarantees[0]["id"]
    decisions = {
        item["capability_id"]: item
        for item in ready_contract["carrier_decisions"]
    }
    assert decisions[trace_id]["carrier_type"] == "runtime_service"
    assert decisions[trace_id]["resource_hint"] == "runtime:workflow_runtime"
    assert evaluate_capability_contract(
        CapabilityBuildContract.model_validate(ready_contract)
    ).valid


def load_v04_script():
    module_path = ROOT / "scripts" / "v04_00_ai_requirement_intake.py"
    spec = importlib.util.spec_from_file_location("v04_00_ai_requirement_intake_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v04_00_static_evidence_passes(tmp_path: Path) -> None:
    module = load_v04_script()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    output = tmp_path / "evidence.json"
    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.4.1"
