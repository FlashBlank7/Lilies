from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
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
        assert "做一个工作流可以像codex一样" in str(provider.calls[0]["message"])
        tasks = client.get("/api/v1/platform/harness/tasks", headers=headers(), params={"kind": "requirement_intake"}).json()
        assert tasks[0]["status"] == "succeeded"
        assert tasks[0]["usage_counts"]["model_call"] == 1


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
