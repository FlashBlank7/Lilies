from __future__ import annotations

import json
import asyncio
import importlib.util
import shlex
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.platform_harness import PlatformHarness, PlatformHarnessViolation
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.sandbox import CommandResult
from agent_platform.storage import Storage
from agent_platform.tools import Tool, ToolContext, ToolResult
from agent_platform.worker_runner import PlatformHarnessWorkerRunner, build_platform_worker_handlers, run_worker_once
from agent_platform.workflow_runtime import WorkflowRuntime
from tests.test_runtime import ScriptedProvider


def load_live_builder_benchmark_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "live_builder_benchmark_suite.py"
    spec = importlib.util.spec_from_file_location("live_builder_benchmark_suite_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class IncrementalBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        operations = [
            ("draft_add_node", {"node": {"id": "start", "type": "start", "title": "Input", "config": {"inputs": [{"name": "name", "type": "string"}]}}}),
            ("draft_add_node", {"node": {"id": "template", "type": "template_transform", "title": "Greeting", "config": {"template": "Hello {{ name }}", "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}}}}}),
            ("draft_add_node", {"node": {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}}}}),
            ("draft_connect", {"edge": {"id": "a", "source": "start", "target": "template", "source_port": "output", "target_port": "input"}}),
            ("draft_connect", {"edge": {"id": "b", "source": "template", "target": "end", "source_port": "text", "target_port": "input"}}),
            ("test_add", {"test": {"name": "Greets", "requirement": "Greeting contains name", "inputs": {"name": "Ada"}, "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}]}}),
            ("draft_validate", {}),
            ("test_run", {}),
            ("draft_publish", {}),
        ]
        name, value = operations[min(self.calls, len(operations) - 1)]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "tool_use", "id": f"call-{self.calls}", "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})


class NoTestBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        operations = [
            ("draft_add_node", {"node": {"id": "start", "type": "start", "title": "Input", "config": {"inputs": [{"name": "name", "type": "string"}]}}}),
            ("draft_add_node", {"node": {"id": "template", "type": "template_transform", "title": "Greeting", "config": {"template": "Hello {{ name }}", "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}}}}}),
            ("draft_add_node", {"node": {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}}}}),
            ("draft_connect", {"edge": {"id": "a", "source": "start", "target": "template", "source_port": "output", "target_port": "input"}}),
            ("draft_connect", {"edge": {"id": "b", "source": "template", "target": "end", "source_port": "text", "target_port": "input"}}),
        ]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls <= len(operations):
            name, value = operations[self.calls - 1]
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"call-{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "done"}
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "done"}
        })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class PlanFirstBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            value = {"action": "set", "plan": {
                "goal": "Build a modular novel generator BlockFlow.",
                "strategy": "Plan outline, drafting, and review modules before mutating the draft.",
                "complexity": "complex",
                "reuse_depth": "shallow",
                "risks": ["content quality needs human review"],
                "modules": [
                    {
                        "id": "outline",
                        "title": "Outline and setting",
                        "purpose": "Create a story outline before drafting.",
                        "expected_blocks": ["start", "model_turn", "end"],
                    }
                ],
            }}
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {
                    "type": "tool_use", "id": "build-plan", "name": "build_plan", "input": {},
                },
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
        else:
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": "planned"}
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "planned"}
            })
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class ManualSkippingBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            value = {"node": {
                "id": "turn", "type": "model_turn", "title": "Turn",
                "config": {"input": "hello", "settings": {"prompt": "hello"}},
            }}
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {
                    "type": "tool_use", "id": "skip-manual", "name": "draft_add_node", "input": {},
                },
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
        else:
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": "done"}
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "done"}
            })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class InvalidRequiredNodeTestBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        operations = [
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Start",
                "config": {"inputs": [{"name": "text", "type": "string"}]},
            }}),
            ("draft_add_node", {"node": {
                "id": "end", "type": "end", "title": "End",
                "config": {"outputs": {"ok": True}},
            }}),
            ("draft_connect", {"edge": {
                "id": "start-end", "source": "start", "target": "end",
                "source_port": "output", "target_port": "input",
            }}),
            ("test_add", {"test": {
                "id": "bad_required_node",
                "name": "Bad required node",
                "requirement": "This test incorrectly names a nonexistent extractor node.",
                "mandatory": True,
                "structural_only": True,
                "required_node_types": ["start", "extract_text", "end"],
                "assertions": [{"path": [], "operator": "exists", "structural": True}],
            }}),
        ]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls <= len(operations):
            name, value = operations[self.calls - 1]
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"invalid-test-{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "done"}
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "done"}
        })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class RepairConfirmationBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        operations = [
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Input",
                "config": {"inputs": [{"name": "text", "type": "string"}]},
            }}),
            ("draft_add_node", {"node": {
                "id": "answer", "type": "answer", "title": "Answer",
                "config": {"answer": {"$ref": {"node_id": "start", "path": ["text"]}}},
            }}),
            ("draft_connect", {"edge": {
                "id": "start-answer", "source": "start", "target": "answer",
                "source_port": "output", "target_port": "input",
            }}),
            ("test_add", {"test": {
                "id": "summary_missing",
                "name": "Wrong summary path",
                "requirement": "This first test intentionally points at the wrong output path.",
                "inputs": {"text": "hello"},
                "assertions": [{"path": ["summary"], "operator": "exists"}],
                "required_node_types": ["start", "answer"],
                "mandatory": True,
            }}),
            ("draft_validate", {}),
            ("test_run", {}),
            ("test_remove", {"test_id": "summary_missing"}),
            ("test_add", {"test": {
                "id": "answer_exists",
                "name": "Answer output exists",
                "requirement": "The repaired test points at the actual answer output.",
                "inputs": {"text": "hello"},
                "assertions": [{"path": ["answer"], "operator": "type", "expected": "string"}],
                "required_node_types": ["start", "answer"],
                "mandatory": True,
            }}),
            ("test_run", {}),
        ]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls <= len(operations):
            name, value = operations[self.calls - 1]
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"repair-confirm-{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "done"}
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "done"}
        })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class TemplateExpandBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            value = {
                "name": "claude_like_coding_agent",
                "prefix": "coding",
                "position": {"x": 0, "y": 0},
            }
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {
                    "type": "tool_use", "id": "expand-template", "name": "template_expand", "input": {},
                },
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
        else:
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": "template expanded"}
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "template expanded"}
            })
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


class PromptCaptureProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.prompts: list[str] = []

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
        prompt = "\n".join(block.text or "" for message in messages for block in message.content)
        self.prompts.append(prompt)
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": ""}
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "saw tool evidence"}
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}
        })


class SubagentCaptureProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        prompt = "\n".join(block.text or "" for message in messages for block in message.content)
        self.calls.append({
            "user_id": user_id,
            "prompt": prompt,
            "tools": [tool.name for tool in tools],
            "max_output_tokens": max_output_tokens,
        })
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 3}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": ""}
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "subagent evidence complete"}
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}
        })


class EvidenceWebSearchInput(BaseModel):
    query: str
    max_results: int = 3
    language: str = "ja"
    country: str = "JP"


class EvidenceWebSearchTool(Tool):
    name = "WebSearch"
    description = "Deterministic test WebSearch evidence provider."
    input_model = EvidenceWebSearchInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = EvidenceWebSearchInput.model_validate(data)
        query = args.query
        slug = query.split()[0].replace("日本", "idol") or "idol"
        results = [
            {
                "title": f"{query} 公式発表 {index}",
                "url": f"https://news.example.test/{slug}/{index}",
                "published_at": f"Wed, 24 Jun 2026 0{index}:00:00 GMT",
                "source": "Example Idol News",
            }
            for index in range(1, args.max_results + 1)
        ]
        return ToolResult(json.dumps({"query": query, "results": results}, ensure_ascii=False))


class LocalToolSandbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.commands: list[list[str]] = []

    async def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        max_output: int = 200_000,
    ) -> CommandResult:
        self.commands.append(argv)
        if argv[:2] == ["python", "-c"]:
            return self._python_tool(argv, stdin=stdin, max_output=max_output)
        if argv[:2] == ["bash", "-lc"]:
            return await self._bash(argv[2], stdin=stdin, timeout=timeout, max_output=max_output)
        return CommandResult(stdout="", stderr=f"unsupported local sandbox command: {argv}", exit_code=127)

    def _path(self, raw: str) -> Path:
        path = (self.workspace / raw).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ValueError("path escapes workspace")
        return path

    def _python_tool(self, argv: list[str], *, stdin: str | None, max_output: int) -> CommandResult:
        script = argv[2]
        path = self._path(argv[3])
        try:
            if "splitlines()" in script:
                offset, limit = int(argv[4]), int(argv[5])
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                selected = lines[offset: offset + limit]
                output = "\n".join(
                    f"{index + 1:6d}\t{line}"
                    for index, line in enumerate(selected, start=offset)
                )
                return CommandResult(stdout=output[:max_output], stderr="", exit_code=0)
            if "path.write_text(sys.stdin.read()" in script:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(stdin or "", encoding="utf-8")
                return CommandResult(stdout=f"wrote {path.relative_to(self.workspace)}\n", stderr="", exit_code=0)
            if "old_string" in script:
                payload = json.loads(stdin or "{}")
                text = path.read_text(encoding="utf-8")
                count = text.count(payload["old_string"])
                if count == 0:
                    return CommandResult(stdout="", stderr="old_string not found", exit_code=1)
                if count > 1 and not payload.get("replace_all"):
                    return CommandResult(stdout="", stderr=f"old_string has {count} matches", exit_code=1)
                new = text.replace(
                    payload["old_string"],
                    payload["new_string"],
                    -1 if payload.get("replace_all") else 1,
                )
                path.write_text(new, encoding="utf-8")
                replaced = count if payload.get("replace_all") else 1
                return CommandResult(stdout=f"replaced {replaced} occurrence(s)\n", stderr="", exit_code=0)
        except Exception as error:
            return CommandResult(stdout="", stderr=str(error), exit_code=1)
        return CommandResult(stdout="", stderr="unsupported python tool script", exit_code=127)

    async def _bash(
        self,
        command: str,
        *,
        stdin: str | None,
        timeout: float | None,
        max_output: int,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                timeout=timeout or 30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(stdout="", stderr=f"command timed out: {command}", exit_code=124)
        return CommandResult(
            stdout=stdout.decode("utf-8", errors="replace")[:max_output],
            stderr=stderr.decode("utf-8", errors="replace")[:max_output],
            exit_code=process.returncode or 0,
        )


class LocalToolSandboxes:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.sessions: dict[str, LocalToolSandbox] = {}

    def resolve_workspace(self, requested: str, *, create: bool = False) -> Path:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError("workspace must stay under test root")
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise ValueError(f"workspace does not exist: {resolved}")
        return resolved

    async def get_or_create(self, session_id: str, workspace_path: str, *_: Any) -> LocalToolSandbox:
        if session_id not in self.sessions:
            self.sessions[session_id] = LocalToolSandbox(self.resolve_workspace(workspace_path))
        return self.sessions[session_id]

    async def remove(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def close(self) -> None:
        self.sessions.clear()


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def test_draft_manual_delete_keeps_nodes_and_edges_consistent(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Manual edit", "requirement": "Edit nodes by hand."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "join", "type": "variable_aggregator", "title": "Join", "config": {
                "variables": [{"$ref": {"node_id": "start", "path": ["output"]}}],
                "mode": "array",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"value": {"$ref": {"node_id": "join", "path": ["output"]}}}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-join", "source": "start", "target": "join",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "join-end", "source": "join", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "remove_edge", {"edge_id": "start-join"})
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert [edge["id"] for edge in draft["snapshot"]["workflow"]["edges"]] == ["join-end"]

        revision = mutate(client, app_id, revision, "remove_node", {"node_id": "join"})
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert {node["id"] for node in draft["snapshot"]["workflow"]["nodes"]} == {"start", "end"}
        assert draft["snapshot"]["workflow"]["edges"] == []


def test_application_placeholder_name_is_derived_from_requirement(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "未命名智能体",
                "requirement": "搭建一个定时 8am 搜索偶像新闻并生成日报的智能体。",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["name"] == "定时 8am 搜索偶像新闻并生成日报的智能体"
        draft = client.get(f"/api/v1/applications/{body['id']}/draft", headers=headers()).json()
        assert draft["snapshot"]["name"] == body["name"]


def test_citation_gate_requires_every_output_url_to_come_from_tool_evidence() -> None:
    evidence = WorkflowRuntime._extract_urls(
        '{"results":[{"url":"https://news.example/one"},{"url":"https://news.example/two"}]}'
    )
    valid_output = WorkflowRuntime._extract_urls(
        {"report": "[one](https://news.example/one) and https://news.example/two"}
    )
    corrupted_output = WorkflowRuntime._extract_urls(
        {"report": "[one](https://news.example/one) and https://news.example/tw0"}
    )

    assert valid_output
    assert valid_output <= evidence
    assert corrupted_output - evidence == {"https://news.example/tw0"}


def test_run_suite_returns_readable_test_frame_report(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Readable tests", "requirement": "Return a readable test report."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Framework-visible acceptance",
            "requirement": "The workflow returns ok.",
            "frame": {
                "title": "Basic output contract",
                "category": "structure",
                "purpose": "Show that the generated BlockFlow has an inspectable output contract.",
                "reviewer_guidance": "Check this before reviewing content quality.",
                "reference": "backend handoff note: readable testing",
                "failure_target": "end node outputs",
            },
            "inputs": {},
            "assertions": [{"path": ["ok"], "operator": "equals", "expected": True}],
            "feedback_hints": ["Inspect the end node output mapping if this fails."],
        }})

        response = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["passed"] is True
        assert body["summary"]["total"] == 1
        assert body["summary"]["frames"] == [{
            "test_id": body["tests"][0]["test_id"],
            "title": "Basic output contract",
            "category": "structure",
            "status": "passed",
        }]
        result = body["tests"][0]
        assert result["frame"]["title"] == "Basic output contract"
        assert result["readable_report"]["purpose"].startswith("Show that")
        assert result["readable_report"]["status"] == "passed"
        assert result["readable_report"]["failure_target"] == "end node outputs"
        assert result["readable_report"]["feedback_hints"] == [
            "Inspect the end node output mapping if this fails."
        ]


def test_template_suggestions_include_reuse_depth_actions(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Novel Outline", "requirement": "Create a novel outline."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        created = client.post(
            f"/api/v1/applications/{app_id}/publish-template",
            headers=headers(),
            json={
                "title": "Novel Outline Template",
                "description": "Reusable outline workflow for novel generation.",
                "category": "content_creation",
                "tags": ["novel", "outline"],
            },
        )
        assert created.status_code == 201, created.text

        deep = client.get(
            "/api/v1/templates/suggestions?requirement=novel%20outline&reuse_depth=deep",
            headers=headers(),
        )
        assert deep.status_code == 200, deep.text
        body = deep.json()
        assert body[0]["reuse_depth"] == "deep"
        assert body[0]["recommended_action"] == "compose_modules"

        none = client.get(
            "/api/v1/templates/suggestions?requirement=novel%20outline&reuse_depth=none",
            headers=headers(),
        ).json()
        assert none == []


def test_platform_harness_tracks_test_suite_and_workflow_usage(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Harness tracked", "requirement": "Track a test run."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Returns ok",
            "requirement": "Workflow returns ok.",
            "inputs": {},
            "assertions": [{"path": ["ok"], "operator": "equals", "expected": True}],
        }})

        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True

        test_tasks = client.get(
            f"/api/v1/platform/harness/tasks?kind=test_suite&owner_id={app_id}",
            headers=headers(),
        ).json()
        assert len(test_tasks) == 1
        assert test_tasks[0]["status"] == "succeeded"

        workflow_tasks = client.get(
            f"/api/v1/platform/harness/tasks?kind=workflow_run&owner_id={app_id}",
            headers=headers(),
        ).json()
        assert len(workflow_tasks) == 1
        assert workflow_tasks[0]["parent_task_id"] == test_tasks[0]["id"]
        assert workflow_tasks[0]["usage_counts"]["node_execution"] == 2

        fetched = client.get(
            f"/api/v1/platform/harness/tasks/{workflow_tasks[0]['id']}",
            headers=headers(),
        ).json()
        assert fetched["id"] == workflow_tasks[0]["id"]


def test_platform_harness_node_budget_blocks_run(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_max_node_executions_per_task=1,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Harness budget", "requirement": "Budget should stop after one node."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)

        assert run["status"] == "failed", run
        assert "node execution budget exceeded" in run["error"]
        task = client.get(f"/api/v1/platform/harness/tasks/{run_id}", headers=headers()).json()
        assert task["status"] == "failed"
        assert task["usage_counts"]["node_execution"] == 2


def test_builder_benchmark_reports_missing_harness_nodes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "permission", "type": "permission_gate", "title": "Permission", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"auto_approve": True},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"ok": {"$ref": {"node_id": "permission", "path": ["output"]}}},
            }},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "permission", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    candidate = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=headers(),
            json={
                "name": "missing harness",
                "reference": reference,
                "candidate": candidate,
                "required_harness_nodes": ["permission_gate"],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"]["passed"] is False
        assert body["report"]["missing"]["harness_nodes"] == ["permission_gate"]
        task = client.get(
            f"/api/v1/platform/harness/tasks/{body['task_id']}",
            headers=headers(),
        ).json()
        assert task["kind"] == "benchmark"


def test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "permission", "type": "permission_gate", "title": "Permission", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"auto_approve": True},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"ok": {"$ref": {"node_id": "permission", "path": ["output"]}}},
            }},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "permission", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    missing_harness_candidate = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    case_base = {
        "reference": reference,
        "required_harness_nodes": ["permission_gate"],
        "tests": [
            {
                "name": "Permission gate is auditable",
                "requirement": "The workflow exposes permission control.",
                "frame": {
                    "title": "Harness coverage",
                    "category": "safety",
                    "purpose": "Show that a reviewer can inspect permission control.",
                },
            }
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/suites/evaluate",
            headers=headers(),
            json={
                "name": "harness suite",
                "minimum_score": 0.8,
                "minimum_pass_rate": 0.75,
                "baseline_scores": {
                    "complete harness": 0.9,
                    "missing harness": 0.7,
                },
                "cost": {
                    "model_calls": 0,
                    "tool_calls": 0,
                    "estimated_cost_usd": 0,
                    "notes": "deterministic suite smoke",
                },
                "cases": [
                    {
                        **case_base,
                        "name": "complete harness",
                        "candidate": reference,
                    },
                    {
                        **case_base,
                        "name": "missing harness",
                        "candidate": missing_harness_candidate,
                    },
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        report = body["report"]
        assert report["passed"] is False
        assert report["case_count"] == 2
        assert report["pass_rate"] == 0.5
        assert report["failed_cases"] == ["missing harness"]
        trends = {item["name"]: item for item in report["trends"]}
        assert trends["complete harness"]["direction"] == "improved"
        assert trends["missing harness"]["direction"] == "regressed"
        assert report["cost"]["notes"] == "deterministic suite smoke"

        task = client.get(
            f"/api/v1/platform/harness/tasks/{body['task_id']}",
            headers=headers(),
        ).json()
        assert task["kind"] == "benchmark"
        assert task["owner_id"] == "builder-benchmark-suite"
        assert task["metadata"]["case_count"] == 2
        assert task["usage_counts"]["node_execution"] == 2


def test_platform_harness_tasks_persist_across_app_instances(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    suite = {
        "name": "durable harness suite",
        "minimum_score": 0.8,
        "minimum_pass_rate": 1.0,
        "cases": [
            {
                "name": "durable task",
                "reference": reference,
                "candidate": reference,
            }
        ],
    }

    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/suites/evaluate",
            headers=headers(),
            json=suite,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]

    restarted_app = create_app(settings, ScriptedProvider())
    with TestClient(restarted_app) as client:
        fetched = client.get(
            f"/api/v1/platform/harness/tasks/{task_id}",
            headers=headers(),
        )
        assert fetched.status_code == 200, fetched.text
        task = fetched.json()
        assert task["id"] == task_id
        assert task["kind"] == "benchmark"
        assert task["status"] == "succeeded"
        assert task["owner_id"] == "builder-benchmark-suite"
        assert task["metadata"]["case_count"] == 1
        assert task["usage_counts"]["node_execution"] == 1

        listed = client.get(
            "/api/v1/platform/harness/tasks?kind=benchmark&owner_id=builder-benchmark-suite",
            headers=headers(),
        )
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == task_id for item in listed.json())


def test_builder_benchmark_history_survives_app_recreation(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    suite = {
        "name": "history suite",
        "minimum_score": 0.8,
        "minimum_pass_rate": 1.0,
        "cases": [
            {
                "name": "history task",
                "reference": reference,
                "candidate": reference,
            }
        ],
    }

    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/suites/evaluate",
            headers=headers(),
            json=suite,
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]

    restarted_app = create_app(settings, ScriptedProvider())
    with TestClient(restarted_app) as client:
        response = client.get(
            "/api/v1/builder-benchmark/history?owner_id=builder-benchmark-suite",
            headers=headers(),
        )
        assert response.status_code == 200, response.text
        history = response.json()
        item = next(record for record in history if record["id"] == task_id)
        assert item["status"] == "succeeded"
        assert item["resource_id"] == "history suite"
        assert item["metadata"]["case_count"] == 1
        assert item["metadata"]["score"] >= 0.8
        assert item["usage_counts"]["node_execution"] == 1


def test_platform_harness_owner_budget_blocks_cross_task_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        storage = Storage(data_dir)
        await storage.initialize()
        harness = PlatformHarness(
            storage=storage,
            max_model_calls_per_owner=1,
        )

        await harness.start_task(
            "owner-budget-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-1",
        )
        await harness.record_usage("owner-budget-1", "model_call")
        await harness.finish_task("owner-budget-1", status="succeeded")

        await harness.start_task(
            "owner-budget-2",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-2",
        )
        with pytest.raises(PlatformHarnessViolation, match="owner model call budget exceeded"):
            await harness.record_usage("owner-budget-2", "model_call")

        failed = await harness.get_task("owner-budget-2")
        assert failed.status == "failed"
        assert failed.usage_counts["model_call"] == 1
        assert "owner model call budget exceeded" in failed.error

        restarted_storage = Storage(data_dir)
        await restarted_storage.initialize()
        restarted = PlatformHarness(
            storage=restarted_storage,
            max_model_calls_per_owner=1,
        )
        persisted = await restarted.get_task("owner-budget-2")
        assert persisted.status == "failed"
        assert persisted.error == failed.error

    asyncio.run(scenario())


def test_platform_harness_reconciles_stale_active_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        storage = Storage(data_dir)
        await storage.initialize()
        harness = PlatformHarness(
            storage=storage,
            max_active_tasks=1,
            stale_active_task_seconds=0.001,
        )

        await harness.start_task(
            "stale-active-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-1",
        )
        await asyncio.sleep(0.01)
        fresh = await harness.start_task(
            "fresh-active-2",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-2",
        )

        stale = await harness.get_task("stale-active-1")
        assert stale.status == "failed"
        assert stale.metadata["stale_reconciled"] is True
        assert "platform harness active task stale" in stale.error
        assert fresh.status == "running"

        restarted_storage = Storage(data_dir)
        await restarted_storage.initialize()
        restarted = PlatformHarness(
            storage=restarted_storage,
            max_active_tasks=1,
            stale_active_task_seconds=0.001,
        )
        persisted = await restarted.get_task("stale-active-1")
        assert persisted.status == "failed"
        assert persisted.metadata["stale_reconciled"] is True

    asyncio.run(scenario())


def test_platform_harness_worker_lease_conflicts_and_persists(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        storage = Storage(data_dir)
        await storage.initialize()
        harness = PlatformHarness(
            storage=storage,
            worker_id="worker-a",
            worker_lease_seconds=60,
        )

        started = await harness.start_task(
            "lease-conflict-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-1",
        )
        assert started.worker_id == "worker-a"
        assert started.lease_expires_at
        assert started.lease_version == 1

        with pytest.raises(PlatformHarnessViolation, match="lease held by worker-a"):
            await harness.claim_task_lease(
                "lease-conflict-1",
                worker_id="worker-b",
                lease_seconds=60,
            )

        renewed = await harness.renew_task_lease(
            "lease-conflict-1",
            worker_id="worker-a",
            lease_seconds=120,
        )
        assert renewed.worker_id == "worker-a"
        assert renewed.lease_version == 2

        restarted_storage = Storage(data_dir)
        await restarted_storage.initialize()
        restarted = PlatformHarness(storage=restarted_storage, worker_id="worker-b")
        persisted = await restarted.get_task("lease-conflict-1")
        assert persisted.worker_id == "worker-a"
        assert persisted.lease_version == 2

        released = await restarted.release_task_lease(
            "lease-conflict-1",
            worker_id="worker-a",
        )
        assert released.worker_id is None
        assert released.lease_expires_at is None
        assert released.status == "queued"
        assert released.lease_version == 3

        with pytest.raises(PlatformHarnessViolation, match="no active worker lease"):
            await restarted.renew_task_lease(
                "lease-conflict-1",
                worker_id="worker-a",
                lease_seconds=60,
            )

    asyncio.run(scenario())


def test_platform_harness_reconciles_expired_worker_leases(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        storage = Storage(data_dir)
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_id="worker-a")

        await harness.start_task(
            "lease-expired-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-1",
            lease_seconds=0.001,
        )
        await asyncio.sleep(0.01)
        reconciled = await harness.reconcile_expired_task_leases()
        assert [item.id for item in reconciled] == ["lease-expired-1"]
        failed = await harness.get_task("lease-expired-1")
        assert failed.status == "failed"
        assert failed.metadata["worker_lease"]["expired"] is True
        assert "worker lease expired" in failed.error

        await harness.start_task(
            "lease-late-success-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-2",
            lease_seconds=0.001,
        )
        await asyncio.sleep(0.01)
        late = await harness.finish_task("lease-late-success-1", status="succeeded")
        assert late is not None
        assert late.status == "failed"
        assert "worker lease expired" in late.error

        restarted_storage = Storage(data_dir)
        await restarted_storage.initialize()
        restarted = PlatformHarness(storage=restarted_storage)
        persisted = await restarted.get_task("lease-late-success-1")
        assert persisted.status == "failed"
        assert persisted.metadata["worker_lease"]["expired"] is True

    asyncio.run(scenario())


def test_platform_harness_worker_lease_api(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        asyncio.run(app.state.services.harness.start_task(
            "api-lease-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-1",
        ))

        claimed = client.post(
            "/api/v1/platform/harness/tasks/api-lease-1/lease",
            headers=headers(),
            json={"worker_id": "api-worker-a", "lease_seconds": 60},
        )
        assert claimed.status_code == 200
        assert claimed.json()["worker_id"] == "api-worker-a"
        assert claimed.json()["lease_version"] == 1

        conflict = client.post(
            "/api/v1/platform/harness/tasks/api-lease-1/lease",
            headers=headers(),
            json={"worker_id": "api-worker-b", "lease_seconds": 60},
        )
        assert conflict.status_code == 409
        assert "lease held by api-worker-a" in conflict.text

        renewed = client.post(
            "/api/v1/platform/harness/tasks/api-lease-1/lease/renew",
            headers=headers(),
            json={"worker_id": "api-worker-a", "lease_seconds": 120},
        )
        assert renewed.status_code == 200
        assert renewed.json()["lease_version"] == 2

        released = client.post(
            "/api/v1/platform/harness/tasks/api-lease-1/lease/release",
            headers=headers(),
            json={"worker_id": "api-worker-a", "next_status": "queued"},
        )
        assert released.status_code == 200
        assert released.json()["worker_id"] is None
        assert released.json()["status"] == "queued"

        renew_without_lease = client.post(
            "/api/v1/platform/harness/tasks/api-lease-1/lease/renew",
            headers=headers(),
            json={"worker_id": "api-worker-a", "lease_seconds": 60},
        )
        assert renew_without_lease.status_code == 409
        assert "no active worker lease" in renew_without_lease.text

        asyncio.run(app.state.services.harness.start_task(
            "api-lease-expired-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-2",
            lease_seconds=0.001,
        ))
        time.sleep(0.01)
        reconciled = client.post(
            "/api/v1/platform/harness/leases/reconcile",
            headers=headers(),
        )
        assert reconciled.status_code == 200
        assert reconciled.json()[0]["id"] == "api-lease-expired-1"
        assert reconciled.json()[0]["status"] == "failed"


def test_platform_harness_worker_runner_completes_queued_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "runner-success-1",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease("runner-success-1", worker_id="producer", next_status="queued")

        async def handler(record):
            assert record.id == "runner-success-1"
            assert record.status == "running"
            return {"handled": True, "resource_id": record.resource_id}

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("runner-success-1", "succeeded")]
        finished = await harness.get_task("runner-success-1")
        assert finished.status == "succeeded"
        assert finished.worker_id == "worker-a"
        assert finished.metadata["worker_runner"]["result"]["handled"] is True
        assert finished.lease_version == 3

    asyncio.run(scenario())


def test_platform_harness_worker_runner_marks_handler_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "runner-failure-1",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease("runner-failure-1", worker_id="producer", next_status="queued")

        async def handler(_record):
            raise RuntimeError("handler failed deliberately")

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("runner-failure-1", "failed")]
        assert "handler failed deliberately" in results[0].error
        finished = await harness.get_task("runner-failure-1")
        assert finished.status == "failed"
        assert "handler failed deliberately" in finished.error
        assert finished.metadata["worker_runner"]["status"] == "failed"

    asyncio.run(scenario())


def test_platform_harness_worker_runner_skips_unsupported_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "runner-skip-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease("runner-skip-1", worker_id="producer", next_status="queued")

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers={},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("runner-skip-1", "skipped")]
        assert "no handler" in results[0].error
        fetched = await harness.get_task("runner-skip-1")
        assert fetched.status == "queued"
        assert fetched.worker_id is None
        assert fetched.lease_version == 2

    asyncio.run(scenario())


def test_platform_harness_worker_runner_renews_lease_for_long_handler(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=0.05)
        await harness.start_task(
            "runner-renew-1",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=0.05,
        )
        await harness.release_task_lease("runner-renew-1", worker_id="producer", next_status="queued")

        async def handler(_record):
            await asyncio.sleep(0.12)
            return {"handled": True}

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=0.05,
            renewal_interval_seconds=0.02,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("runner-renew-1", "succeeded")]
        finished = await harness.get_task("runner-renew-1")
        assert finished.status == "succeeded"
        assert finished.lease_version > 3
        assert finished.metadata["worker_runner"]["renewal_count"] >= 1
        assert finished.metadata["worker_runner"]["result"]["handled"] is True

    asyncio.run(scenario())


def test_platform_worker_runner_helper_imports() -> None:
    assert callable(run_worker_once)


def test_platform_worker_scheduler_manual_trigger_handler_runs_workflow(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Worker schedule", "requirement": "Run schedule through worker."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "schedule", "type": "schedule_trigger", "title": "08:00 JST", "config": {
                "timezone": "Asia/Tokyo", "hour": 8, "minute": 0, "inputs": {"topic": "idols"}
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "topic": {"$ref": {"node_id": "schedule", "path": ["topic"]}}
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "scheduled-end", "source": "schedule", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Scheduled inputs", "requirement": "Schedule defaults reach the result.",
            "inputs": {},
            "assertions": [{"path": ["topic"], "operator": "equals", "expected": "idols"}],
        }})
        assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200

        task_id = "worker-scheduler-manual-1"
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                task_id,
                kind="scheduler_manual_trigger",
                owner_id=app_id,
                resource_id=task_id,
                metadata={"inputs": {"topic": "worker-topic"}},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                task_id,
                worker_id="producer",
                next_status="queued",
            )

        client.portal.call(queue_task)

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [(task_id, "succeeded")]

        task = client.portal.call(harness.get_task, task_id)
        run_id = task.metadata["worker_runner"]["result"]["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert run["outputs"] == {"topic": "worker-topic"}
        assert task.metadata["worker_runner"]["result"]["application_id"] == app_id


def test_platform_harness_secret_policy_blocks_http_secret_headers(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Secret policy", "requirement": "Block secret headers."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "http", "type": "http_request", "title": "HTTP", "config": {
                "method": "GET",
                "url": "https://example.test/blocked",
                "headers": {"Authorization": "Bearer sk-test"},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-http", "source": "start", "target": "http",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "http-end", "source": "http", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)

        assert run["status"] == "failed", run
        assert "secret policy blocked" in run["error"]
        assert "headers.Authorization" in run["error"]


def test_platform_harness_secret_store_api_redacts_values(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/secrets",
            headers=headers(),
            json={
                "owner_id": "owner-a",
                "name": "api_token",
                "value": "sk-live-secret",
                "description": "test token",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["secret_ref"] == "secret://owner-a/api_token"
        assert created.json()["redacted"] is True
        assert "sk-live-secret" not in created.text
        assert "value" not in created.json()

        listed = client.get("/api/v1/platform/secrets?owner_id=owner-a", headers=headers())
        assert listed.status_code == 200
        assert listed.json()[0]["name"] == "api_token"
        assert "sk-live-secret" not in listed.text

        deleted = client.delete("/api/v1/platform/secrets/owner-a/api_token", headers=headers())
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get("/api/v1/platform/secrets?owner_id=owner-a", headers=headers()).json() == []


def test_platform_harness_secret_store_uses_envelope_at_rest(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_secret_envelope_key="unit-test-envelope-key",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/platform/secrets",
            headers=headers(),
            json={
                "owner_id": "owner-a",
                "name": "api_token",
                "value": "sk-envelope-secret",
                "description": "test secret",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["storage_mode"] == "encrypted_v1"
        assert created.json()["encrypted"] is True
        assert "sk-envelope-secret" not in created.text

        raw = asyncio.run(
            app.state.services.storage.get_platform_secret(owner_id="owner-a", name="api_token")
        )
        assert raw["value"].startswith("secret-envelope:v1:")
        assert "sk-envelope-secret" not in raw["value"]

        injected = asyncio.run(
            app.state.services.harness.inject_secret_references(
                owner_id="owner-a",
                payload={"Authorization": {"$secret": "api_token", "prefix": "Bearer "}},
            )
        )
        assert injected == {"Authorization": "Bearer sk-envelope-secret"}


def test_platform_harness_secret_envelope_reads_legacy_plaintext_rows(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_secret_envelope_key="unit-test-envelope-key",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app):
        asyncio.run(
            app.state.services.storage.save_platform_secret(
                owner_id="owner-a",
                name="legacy_token",
                value="sk-legacy-secret",
                description="legacy row",
            )
        )
        listed = asyncio.run(app.state.services.harness.list_secrets(owner_id="owner-a"))
        assert listed[0]["storage_mode"] == "legacy_plaintext"
        assert listed[0]["encrypted"] is False

        injected = asyncio.run(
            app.state.services.harness.inject_secret_references(
                owner_id="owner-a",
                payload={"Authorization": {"$secret": "legacy_token", "prefix": "Bearer "}},
            )
        )
        assert injected == {"Authorization": "Bearer sk-legacy-secret"}


def test_platform_harness_secret_reference_injects_http_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        is_error = False
        headers = {"content-type": "application/json"}
        text = '{"ok": true}'

        def json(self) -> dict[str, Any]:
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            requests.append({"method": method, "url": url, **kwargs})
            assert kwargs["headers"]["Authorization"] == "Bearer sk-http-secret"
            return FakeResponse()

    monkeypatch.setattr("agent_platform.workflow_runtime.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Secret reference", "requirement": "Use secret references safely."},
        ).json()["id"]
        secret = client.post(
            "/api/v1/platform/secrets",
            headers=headers(),
            json={"owner_id": app_id, "name": "api_token", "value": "sk-http-secret"},
        )
        assert secret.status_code == 201, secret.text
        assert "sk-http-secret" not in secret.text

        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "http", "type": "http_request", "title": "HTTP", "config": {
                "method": "GET",
                "url": "https://example.test/secret-ref",
                "headers": {"Authorization": {"$secret": "api_token", "prefix": "Bearer "}},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": "{{nodes.http.output.ok}}"}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-http", "source": "start", "target": "http",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "http-end", "source": "http", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "succeeded":
                break
            time.sleep(0.01)

        assert run["status"] == "succeeded", run
        assert requests and requests[0]["headers"]["Authorization"] == "Bearer sk-http-secret"
        events = client.get(f"/v1/streams/{run_id}", headers=headers()).text
        assert "sk-http-secret" not in events


def test_platform_harness_missing_secret_reference_fails(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Missing secret reference", "requirement": "Fail missing secrets."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "http", "type": "http_request", "title": "HTTP", "config": {
                "method": "GET",
                "url": "https://example.test/missing-secret",
                "headers": {"Authorization": {"$secret": "missing", "prefix": "Bearer "}},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-http", "source": "start", "target": "http",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "http-end", "source": "http", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)

        assert run["status"] == "failed", run
        assert "platform secret not found" in run["error"]


def test_platform_harness_network_egress_policy_blocks_http_requests(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_network_egress_policy="none",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Egress policy", "requirement": "Block outbound HTTP."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "http", "type": "http_request", "title": "HTTP", "config": {
                "method": "GET",
                "url": "https://example.test/blocked",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-http", "source": "start", "target": "http",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "http-end", "source": "http", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)

        assert run["status"] == "failed", run
        assert "network egress policy blocked" in run["error"]


def test_platform_harness_tool_egress_policy_blocks_websearch_tool(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_network_egress_policy="none",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Tool egress policy", "requirement": "Block WebSearch outbound network."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "search", "type": "tool", "title": "Search", "config": {
                "tool_name": "WebSearch",
                "input": {"query": "tokyo technology news"},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-search", "source": "start", "target": "search",
            "source_port": "output", "target_port": "input",
        }})
        mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "search-end", "source": "search", "target": "end",
            "source_port": "output", "target_port": "input",
        }})

        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)

        assert run["status"] == "failed", run
        assert "network egress policy blocked" in run["error"]
        assert "WebSearch" in run["error"]


def test_platform_harness_policy_controls_api_reports_stdio_mcp_decisions(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_network_egress_policy="full",
        platform_harness_network_egress_allowlist=["api.example.test"],
        platform_harness_secret_policy_enabled=True,
        platform_harness_worker_lease_seconds=60,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/harness/policy-controls", headers=headers())
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["network_egress_policy"] == "full"
        assert body["network_egress_allowlist"] == ["api.example.test"]
        assert body["secret_policy_enabled"] is True
        assert body["worker_lease_seconds"] == 60

        stdio = body["stdio_mcp"]
        assert stdio["sandboxed_no_network_supported"] is True
        assert stdio["allowlist_supported"] is False

        decisions = {item["id"]: item for item in stdio["decisions"]}
        assert decisions["sandboxed_no_network"]["allowed"] is True
        assert decisions["sandboxed_no_network"]["mode"] == "sandboxed_no_network"
        assert decisions["sandboxed_allowlist"]["allowed"] is False
        assert "allowlist-grade enforcement" in decisions["sandboxed_allowlist"]["reason"]
        assert decisions["sandboxed_allowlist"]["operator_action"]
        assert decisions["restricted_unsandboxed"]["allowed"] is False


def test_builder_benchmark_treats_llm_as_model_turn_equivalent(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "model", "type": "model_turn", "title": "Model", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"prompt": "Summarize."},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"summary": {"$ref": {"node_id": "model", "path": ["output"]}}},
            }},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "model", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "model", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }
    candidate = {
        **reference,
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "model", "type": "llm", "title": "LLM", "config": {
                "prompt": "Summarize.",
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"summary": {"$ref": {"node_id": "model", "path": ["text"]}}},
            }},
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=headers(),
            json={
                "name": "llm equivalent",
                "reference": reference,
                "candidate": candidate,
                "required_node_types": ["start", "model_turn", "end"],
            },
        )
        assert response.status_code == 200, response.text
        report = response.json()["report"]
        assert report["passed"] is True
        assert report["missing"]["node_types"] == []
        assert report["metrics"]["node_type_coverage"] == 1.0
        assert report["metrics"]["node_type_equivalences"]["model_turn"] == ["llm"]


def test_builder_benchmark_treats_answer_as_terminal_end_equivalent(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    reference = {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "model", "type": "model_turn", "title": "Model", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"prompt": "Summarize."},
            }},
            {"id": "terminal", "type": "end", "title": "End", "config": {
                "outputs": {"summary": {"$ref": {"node_id": "model", "path": ["output"]}}},
            }},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "model", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "model", "target": "terminal", "source_port": "output", "target_port": "input"},
        ],
    }
    candidate = {
        **reference,
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "model", "type": "model_turn", "title": "Model", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"prompt": "Summarize."},
            }},
            {"id": "terminal", "type": "answer", "title": "Answer", "config": {
                "answer": {"$ref": {"node_id": "model", "path": ["output"]}},
            }},
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=headers(),
            json={
                "name": "answer terminal equivalent",
                "reference": reference,
                "candidate": candidate,
                "required_node_types": ["start", "model_turn", "end"],
            },
        )
        assert response.status_code == 200, response.text
        report = response.json()["report"]
        assert report["passed"] is True
        assert report["missing"]["node_types"] == []
        assert report["metrics"]["node_type_coverage"] == 1.0
        assert report["metrics"]["node_type_equivalences"]["end"] == ["answer"]


def test_live_builder_benchmark_case_registry_supports_complex_case() -> None:
    module = load_live_builder_benchmark_module()

    summary = module.get_benchmark_case("summary_smoke")
    complex_case = module.get_benchmark_case("complex_research_brief")

    assert summary.name == "summary_smoke"
    assert summary.required_node_types == ["start", "model_turn", "end"]
    assert complex_case.name == "complex_research_brief"
    assert set(complex_case.required_node_types) == {
        "start",
        "parameter_extractor",
        "question_classifier",
        "context_assembler",
        "model_turn",
        "template_transform",
        "event_recorder",
        "end",
    }
    assert complex_case.required_harness_nodes == ["event_recorder"]
    reference_types = {node["type"] for node in complex_case.reference["nodes"]}
    assert set(complex_case.required_node_types).issubset(reference_types)
    assert "复杂多模块研究简报 BlockFlow" in complex_case.requirement


def test_natural_language_draft_patch_preview_is_non_destructive(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Patch preview", "requirement": "Preview a node rename."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        before = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()

        response = client.post(
            f"/api/v1/applications/{app_id}/draft/preview-patch",
            headers=headers(),
            json={"instruction": "rename node end to Final Answer"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert body["intent"] == "rename_node"
        assert body["operations"][0]["op"] == "update_node"
        assert body["operations"][0]["expected_revision"] == before["revision"]

        after = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert after["revision"] == before["revision"]
        assert after["content_hash"] == before["content_hash"]


def test_validation_enforces_required_visible_blocks_and_tool_nodes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Visible gate", "requirement": "Require explicit WebSearch bricks."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Needs visible search",
            "requirement": "The workflow must expose WebSearch as a Tool block.",
            "inputs": {},
            "assertions": [{"path": ["ok"], "operator": "equals", "expected": True}],
            "required_node_types": ["tool"],
            "required_tool_nodes": ["WebSearch"],
        }})
        invalid = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert invalid["valid"] is False
        assert "missing required node types" in " ".join(invalid["errors"])
        assert "missing required tool nodes" in " ".join(invalid["errors"])

        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "search", "type": "tool", "title": "Search", "config": {
                "tool_name": "WebSearch",
                "input": {"query": "日本 女性アイドル ニュース", "max_results": 1},
            },
        }})
        revision = mutate(client, app_id, revision, "remove_edge", {"edge_id": "a"})
        for edge in [
            {"id": "s-search", "source": "start", "target": "search", "source_port": "output", "target_port": "input"},
            {"id": "search-end", "source": "search", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        valid = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert valid["valid"] is True, valid


def test_validation_warns_when_start_inputs_are_not_connected(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Input wiring", "requirement": "Warn about ignored user inputs."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {
                "inputs": [{"name": "topic", "label": "Topic", "type": "string"}],
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Outputs constant",
            "requirement": "Workflow returns an output.",
            "inputs": {"topic": "AI Agent"},
            "assertions": [{"path": ["ok"], "operator": "equals", "expected": True}],
        }})

        warning = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert warning["valid"] is True, warning
        assert "workflow inputs are not connected" in " ".join(warning["warnings"])
        assert "topic" in " ".join(warning["warnings"])

        revision = mutate(client, app_id, revision, "update_node", {"node_id": "end", "changes": {
            "config": {"outputs": {"topic": {"$ref": {"node_id": "$inputs", "path": ["topic"]}}}},
        }, "merge_config": False})
        connected_raw = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert connected_raw["warnings"] == []

        revision = mutate(client, app_id, revision, "update_node", {"node_id": "end", "changes": {
            "config": {"outputs": {"payload": {"$ref": {"node_id": "start", "path": ["output"]}}}},
        }, "merge_config": False})
        connected_start_output = client.post(
            f"/api/v1/applications/{app_id}/draft/validate", headers=headers()
        ).json()
        assert connected_start_output["warnings"] == []


def test_agent_architecture_blocks_execute_as_runtime_steps(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Claude-like blocks", "requirement": "Execute visible agent architecture blocks."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {
                "inputs": [{"name": "context", "type": "string"}],
            }},
            {"id": "compact", "type": "context_compactor", "title": "Compact", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["context"]}},
                "settings": {"max_chars": 32, "preserved_facts": ["tests failed"]},
            }},
            {"id": "trace", "type": "event_recorder", "title": "Trace", "config": {
                "input": {"$ref": {"node_id": "compact", "path": ["output"]}},
                "settings": {"label": "compaction trace"},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {
                    "summary": {"$ref": {"node_id": "compact", "path": ["output", "summary"]}},
                    "state": {"$ref": {"node_id": "trace", "path": ["state", "recorded"]}},
                }
            }},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "compact", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "compact", "target": "trace", "source_port": "output", "target_port": "input"},
            {"id": "c", "source": "trace", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Visible architecture blocks execute",
            "requirement": "Context compaction and event recording run as workflow steps.",
            "inputs": {"context": "tests failed because the calculator subtract function returns the wrong value"},
            "assertions": [{"path": ["state"], "operator": "equals", "expected": True}],
            "required_node_types": ["context_compactor", "event_recorder"],
        }})
        validation = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert validation["valid"] is True, validation
        tested = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert tested.status_code == 200, tested.text
        assert tested.json()["passed"] is True, tested.text
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200

        run_id = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {"context": "tests failed because the calculator subtract function returns the wrong value"}},
        ).json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert record["status"] == "succeeded", record
        assert record["outputs"]["state"] is True
        assert "[compacted]" in record["outputs"]["summary"]


def test_incremental_workflow_test_publish_restore(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "Greeting workflow",
                "description": "A real editable workflow",
                "requirement": "Return a greeting containing the supplied name.",
                "mode": "workflow",
            },
        )
        assert created.status_code == 201, created.text
        app_id = created.json()["id"]
        revision = 0
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "start", "type": "start", "title": "Input", "position": {"x": 0, "y": 0},
            "config": {"inputs": [{"name": "name", "label": "Name", "type": "string"}]},
        }})
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "template", "type": "template_transform", "title": "Greeting",
            "position": {"x": 300, "y": 0},
            "config": {
                "template": "Hello, {{ name }}!",
                "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
            },
        }})
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "end", "type": "end", "title": "Output", "position": {"x": 600, "y": 0},
            "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}},
        }})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-template", "source": "start", "target": "template",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "template-end", "source": "template", "target": "end",
            "source_port": "text", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Greets Ada",
            "requirement": "The output must greet the supplied name.",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello, Ada!"}],
            "mandatory": True,
        }})

        validation = client.post(
            f"/api/v1/applications/{app_id}/draft/validate", headers=headers()
        )
        assert validation.json()["valid"] is True, validation.text
        tested = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert tested.status_code == 200, tested.text
        assert tested.json()["passed"] is True, tested.text
        published = client.post(f"/api/v1/applications/{app_id}/versions", headers=headers())
        assert published.status_code == 200, published.text
        assert published.json()["version"] == 1
        platform_tools = client.get("/api/v1/tools", headers=headers()).json()
        assert any(item["name"] == f"workflow:{app_id}" for item in platform_tools)

        caller_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Workflow caller", "requirement": "Call a published workflow as a Tool block."},
        ).json()["id"]
        caller_revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Input", "config": {"inputs": [{"name": "name", "type": "string"}]}},
            {"id": "call", "type": "tool", "title": "Call greeting workflow", "config": {
                "tool_name": f"workflow:{app_id}",
                "input": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"nested": {"$ref": {"node_id": "call", "path": ["output"]}}}
            }},
        ]:
            caller_revision = mutate(client, caller_id, caller_revision, "add_node", {"node": node})
        for edge in [
            {"id": "caller-a", "source": "start", "target": "call", "source_port": "output", "target_port": "input"},
            {"id": "caller-b", "source": "call", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            caller_revision = mutate(client, caller_id, caller_revision, "add_edge", {"edge": edge})
        caller_revision = mutate(client, caller_id, caller_revision, "add_test", {"test": {
            "name": "Nested workflow tool",
            "requirement": "A published workflow can be called by another workflow Tool node.",
            "inputs": {"name": "Lin"},
            "assertions": [{"path": ["nested", "greeting"], "operator": "equals", "expected": "Hello, Lin!"}],
            "required_node_types": ["tool"],
            "required_tool_nodes": [f"workflow:{app_id}"],
        }})
        nested_report = client.post(f"/api/v1/applications/{caller_id}/tests/run", headers=headers())
        assert nested_report.status_code == 200, nested_report.text
        assert nested_report.json()["passed"] is True, nested_report.text

        run = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {"name": "Grace"}, "workspace_path": "."},
        )
        assert run.status_code == 202, run.text
        run_id = run.json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert record["status"] == "succeeded", record
        assert record["outputs"] == {"greeting": "Hello, Grace!"}

        restored = client.post(
            f"/api/v1/applications/{app_id}/versions/1/restore", headers=headers()
        )
        assert restored.status_code == 200
        assert restored.json()["revision"] == revision + 1
        republish = client.post(f"/api/v1/applications/{app_id}/versions", headers=headers())
        assert republish.status_code == 409


def test_human_input_pauses_and_resumes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Approval", "requirement": "Ask a human to approve a request."},
        ).json()
        app_id, revision = created["id"], 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "human", "type": "human_input", "title": "Approval", "config": {
                "title": "Approve?", "fields": [{"name": "approved", "label": "Approved", "type": "boolean"}]
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"approved": {"$ref": {"node_id": "human", "path": ["approved"]}}}
            }},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "human", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "human", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Approval resume", "requirement": "Human approval reaches the output.",
            "inputs": {"__human__": {"human": {"approved": True}}},
            "assertions": [{"path": ["approved"], "operator": "equals", "expected": True}],
        }})
        assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200
        run_id = client.post(
            f"/api/v1/applications/{app_id}/runs", headers=headers(), json={"inputs": {}}
        ).json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "paused":
                break
            time.sleep(0.01)
        assert record["status"] == "paused"

    # A fresh FastAPI/WorkflowRuntime instance must recover the serialized graph
    # state and continue after the process that created the run has gone away.
    restarted = create_app(settings, ScriptedProvider())
    with TestClient(restarted) as client:
        resumed = client.post(
            f"/api/v1/runs/{run_id}/resume", headers=headers(), json={"values": {"approved": True}}
        )
        assert resumed.status_code == 200, resumed.text
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert record["outputs"] == {"approved": True}


def test_permission_gate_and_mailbox_wait_wake_pause_and_resume(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Permission mailbox", "requirement": "Pause for permission, then mailbox wake."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "permission", "type": "permission_gate", "title": "Permission", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"reason": "Allow workspace test repair."},
            }},
            {"id": "mailbox", "type": "mailbox_wait_wake", "title": "Mailbox", "config": {
                "input": {"$ref": {"node_id": "permission", "path": ["output"]}},
                "settings": {},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "approved": {"$ref": {"node_id": "permission", "path": ["state", "approved"]}},
                "messages": {"$ref": {"node_id": "mailbox", "path": ["state", "messages"]}},
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "permission", "target": "mailbox", "source_port": "output", "target_port": "input"},
            {"id": "c", "source": "mailbox", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Preset permission and mailbox",
            "requirement": "Permission and mailbox can be pre-seeded for automated acceptance.",
            "inputs": {"__permissions__": {"permission": True}, "__mailbox__": {"mailbox": ["wake"]}},
            "assertions": [
                {"path": ["approved"], "operator": "equals", "expected": True},
                {"path": ["messages"], "operator": "equals", "expected": ["wake"]},
            ],
            "required_node_types": ["permission_gate", "mailbox_wait_wake"],
        }})
        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True, report.text
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200

        run_id = client.post(
            f"/api/v1/applications/{app_id}/runs", headers=headers(), json={"inputs": {}}
        ).json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "paused":
                break
            time.sleep(0.01)
        assert record["status"] == "paused"
        assert record["state"]["waiting_node_id"] == "permission"

        resumed = client.post(
            f"/api/v1/runs/{run_id}/resume", headers=headers(), json={"values": {"behavior": "allow"}}
        )
        assert resumed.status_code == 200, resumed.text
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "paused" and record["state"]["waiting_node_id"] == "mailbox":
                break
            time.sleep(0.01)
        assert record["status"] == "paused"
        assert record["state"]["waiting_node_id"] == "mailbox"

        resumed = client.post(
            f"/api/v1/runs/{run_id}/resume",
            headers=headers(),
            json={"values": {"messages": ["triage complete"]}},
        )
        assert resumed.status_code == 200, resumed.text
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert record["outputs"] == {"approved": True, "messages": ["triage complete"]}
        events = client.get(f"/v1/streams/{run_id}", headers=headers()).json()
        event_types = [event["type"] for event in events]
        assert "permission.requested" in event_types
        assert "mailbox.waiting" in event_types
        assert "mailbox.woke" in event_types


def test_tool_result_can_feed_next_model_turn_context(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = PromptCaptureProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Tool feedback", "requirement": "Feed tool result into the next model turn."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [
                {"name": "tool_result", "type": "string"},
            ]}},
            {"id": "normalize", "type": "tool_result_normalizer", "title": "Normalize", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["tool_result"]}},
                "settings": {},
            }},
            {"id": "turn", "type": "model_turn", "title": "Model Turn", "config": {
                "input": {"$ref": {"node_id": "normalize", "path": ["output"]}},
                "settings": {
                    "prompt": {"$ref": {"node_id": "normalize", "path": ["output"]}},
                    "system": "Use the provided tool evidence.",
                },
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "answer": {"$ref": {"node_id": "turn", "path": ["text"]}},
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "normalize", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "normalize", "target": "turn", "source_port": "output", "target_port": "input"},
            {"id": "c", "source": "turn", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Tool evidence reaches model",
            "requirement": "A normalized tool result enters the next model turn.",
            "inputs": {"tool_result": "{\"file\":\"calculator.py\",\"failure\":\"subtract returned 5\"}"},
            "assertions": [{"path": ["answer"], "operator": "equals", "expected": "saw tool evidence"}],
            "required_node_types": ["tool_result_normalizer", "model_turn"],
        }})
        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True, report.text
        assert provider.prompts
        assert "calculator.py" in provider.prompts[-1]
        assert "subtract returned 5" in provider.prompts[-1]


def test_subagent_spawn_has_independent_context_tools_budget_and_events(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = SubagentCaptureProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        local_sandboxes = LocalToolSandboxes(settings.workspace_root)
        services = app.state.services
        services.sandboxes = local_sandboxes
        services.workflow_runtime.sandboxes = local_sandboxes
        services.runtime.sandboxes = local_sandboxes

        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Subagent runtime", "requirement": "Spawn a bounded subagent with its own context."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [
                {"name": "task", "type": "string"},
            ]}},
            {"id": "context", "type": "context_assembler", "title": "Context", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["task"]}},
                "settings": {"fragments": [{"$ref": {"node_id": "start", "path": ["task"]}}]},
            }},
            {"id": "subagent", "type": "subagent_spawn", "title": "Triage Subagent", "config": {
                "input": {"$ref": {"node_id": "context", "path": ["output"]}},
                "settings": {
                    "name": "test-triage-subagent",
                    "task": "Inspect the supplied failing-test context and return concise evidence.",
                    "tools": ["Read"],
                    "budget": {"max_rounds": 2, "max_cost_usd": 0.25},
                    "workspace_path": ".",
                },
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "result": {"$ref": {"node_id": "subagent", "path": ["output"]}},
                "session_id": {"$ref": {"node_id": "subagent", "path": ["state", "session_id"]}},
                "tools": {"$ref": {"node_id": "subagent", "path": ["state", "tools"]}},
                "max_turns": {"$ref": {"node_id": "subagent", "path": ["state", "max_turns"]}},
                "max_budget_usd": {"$ref": {"node_id": "subagent", "path": ["state", "max_budget_usd"]}},
                "usage": {"$ref": {"node_id": "subagent", "path": ["state", "usage"]}},
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "context", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "context", "target": "subagent", "source_port": "output", "target_port": "input"},
            {"id": "c", "source": "subagent", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Subagent isolation",
            "requirement": "Subagent has its own context, tool whitelist, budget, usage, and event stream.",
            "inputs": {"task": "The failing test says add(7, 5) returned 2."},
            "assertions": [
                {"path": ["result"], "operator": "contains", "expected": "subagent evidence complete"},
                {"path": ["tools"], "operator": "equals", "expected": ["Read"]},
                {"path": ["max_turns"], "operator": "equals", "expected": 2},
                {"path": ["max_budget_usd"], "operator": "equals", "expected": 0.25},
                {"path": ["usage", "input_tokens"], "operator": "equals", "expected": 3},
                {"path": ["usage", "output_tokens"], "operator": "equals", "expected": 4},
            ],
            "required_node_types": ["context_assembler", "subagent_spawn", "end"],
        }})

        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True, report.text
        run_id = report.json()["tests"][0]["run_id"]
        run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
        session_id = run["outputs"]["session_id"]
        assert session_id != run_id
        assert provider.calls
        assert provider.calls[0]["user_id"] == session_id
        assert provider.calls[0]["tools"] == ["Read"]
        assert "Inspect the supplied failing-test context" in provider.calls[0]["prompt"]

        workflow_events = client.get(f"/v1/streams/{run_id}", headers=headers()).json()
        workflow_event_types = [event["type"] for event in workflow_events]
        assert "subagent.started" in workflow_event_types
        assert "subagent.event" in workflow_event_types
        assert "subagent.completed" in workflow_event_types
        child_events = client.get(f"/v1/streams/{session_id}", headers=headers()).json()
        child_event_types = [event["type"] for event in child_events]
        assert "session.started" in child_event_types
        assert "turn.started" in child_event_types
        assert "turn.completed" in child_event_types


def test_claude_architecture_blocks_fix_python_test_failure_without_legacy_agent(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    fixture = Path("examples/broken_python_project").resolve()
    workspace = settings.workspace_root / "broken-python"
    shutil.copytree(fixture, workspace)

    python = shlex.quote(sys.executable)
    pytest_command = f"{python} -m pytest -q"
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        local_sandboxes = LocalToolSandboxes(settings.workspace_root)
        services = app.state.services
        services.sandboxes = local_sandboxes
        services.workflow_runtime.sandboxes = local_sandboxes
        services.runtime.sandboxes = local_sandboxes

        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "Python test repair acceptance",
                "requirement": "Use architecture blocks to read, test, edit, and retest a Python project.",
            },
        ).json()["id"]
        revision = 0
        nodes = [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [
                {"name": "task", "type": "string"},
            ]}},
            {"id": "context", "type": "context_assembler", "title": "Context", "config": {
                "input": {"$ref": {"node_id": "start", "path": ["task"]}},
                "settings": {"fragments": [{"$ref": {"node_id": "start", "path": ["task"]}}]},
            }},
            {"id": "workspace", "type": "workspace_context_injector", "title": "Workspace", "config": {
                "input": {"$ref": {"node_id": "context", "path": ["output"]}},
                "settings": {"scope": "fixture", "files": ["calculator.py", "test_calculator.py"]},
            }},
            {"id": "compact", "type": "context_compactor", "title": "Compact", "config": {
                "input": {"$ref": {"node_id": "workspace", "path": ["output"]}},
                "settings": {"max_chars": 1200, "preserved_facts": ["read files", "pytest evidence"]},
            }},
            {"id": "permission", "type": "permission_gate", "title": "Permission", "config": {
                "input": {"$ref": {"node_id": "compact", "path": ["output"]}},
                "settings": {"auto_approve": True, "reason": "Allow local test repair."},
            }},
            {"id": "sandbox", "type": "sandbox_boundary", "title": "Sandbox", "config": {
                "input": {"$ref": {"node_id": "permission", "path": ["output"]}},
                "settings": {"network_policy": "none", "workspace": "broken-python"},
            }},
            {"id": "read_source", "type": "tool_executor", "title": "Read calculator.py", "config": {
                "input": {"$ref": {"node_id": "sandbox", "path": ["output"]}},
                "settings": {"tool_name": "Read", "workspace_path": "broken-python", "tool_input": {"path": "calculator.py"}},
            }},
            {"id": "run_failing_tests", "type": "tool_executor", "title": "Run failing tests", "error_strategy": "continue", "config": {
                "input": {"$ref": {"node_id": "read_source", "path": ["output"]}},
                "settings": {"tool_name": "Bash", "workspace_path": "broken-python", "tool_input": {"command": pytest_command, "timeout": 30}},
            }},
            {"id": "normalize_failure", "type": "tool_result_normalizer", "title": "Normalize failure", "config": {
                "input": {"$ref": {"node_id": "run_failing_tests", "path": ["error"]}},
                "settings": {},
            }},
            {"id": "edit_fix", "type": "tool_executor", "title": "Edit fix", "config": {
                "input": {"$ref": {"node_id": "normalize_failure", "path": ["output"]}},
                "settings": {"tool_name": "Edit", "workspace_path": "broken-python", "tool_input": {
                    "path": "calculator.py",
                    "old_string": "return left - right",
                    "new_string": "return left + right",
                }},
            }},
            {"id": "run_passing_tests", "type": "tool_executor", "title": "Run passing tests", "config": {
                "input": {"$ref": {"node_id": "edit_fix", "path": ["output"]}},
                "settings": {"tool_name": "Bash", "workspace_path": "broken-python", "tool_input": {"command": pytest_command, "timeout": 30}},
            }},
            {"id": "trace", "type": "event_recorder", "title": "Trace", "config": {
                "input": {"$ref": {"node_id": "run_passing_tests", "path": ["output"]}},
                "settings": {"label": "python_test_repair_acceptance"},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "source_before": {"$ref": {"node_id": "read_source", "path": ["output"]}},
                "initial_failure": {"$ref": {"node_id": "normalize_failure", "path": ["output", "text"]}},
                "edit_result": {"$ref": {"node_id": "edit_fix", "path": ["output"]}},
                "final_test": {"$ref": {"node_id": "run_passing_tests", "path": ["output"]}},
                "trace": {"$ref": {"node_id": "trace", "path": ["state", "recorded"]}},
            }}},
        ]
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            ("start", "context"),
            ("context", "workspace"),
            ("workspace", "compact"),
            ("compact", "permission"),
            ("permission", "sandbox"),
            ("sandbox", "read_source"),
            ("read_source", "run_failing_tests"),
            ("run_failing_tests", "normalize_failure"),
            ("normalize_failure", "edit_fix"),
            ("edit_fix", "run_passing_tests"),
            ("run_passing_tests", "trace"),
            ("trace", "end"),
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": {
                "id": f"{edge[0]}-{edge[1]}",
                "source": edge[0],
                "target": edge[1],
                "source_port": "output",
                "target_port": "input",
            }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Repairs Python tests",
            "requirement": "The workflow reads files, runs failing tests, edits code, and reruns passing tests.",
            "inputs": {"task": "Fix the failing Python tests."},
            "assertions": [
                {"path": ["source_before"], "operator": "contains", "expected": "return left - right"},
                {"path": ["initial_failure"], "operator": "contains", "expected": "[exit_code=1]"},
                {"path": ["edit_result"], "operator": "contains", "expected": "replaced 1 occurrence"},
                {"path": ["final_test"], "operator": "contains", "expected": "[exit_code=0]"},
                {"path": ["trace"], "operator": "equals", "expected": True},
            ],
            "required_node_types": [
                "context_assembler",
                "workspace_context_injector",
                "context_compactor",
                "permission_gate",
                "sandbox_boundary",
                "tool_executor",
                "tool_result_normalizer",
                "event_recorder",
            ],
            "required_tool_nodes": ["Read", "Bash", "Edit"],
            "minimum_tool_calls": 4,
        }})

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert "claude_agent" not in {node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
        validation = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert validation["valid"] is True, validation
        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["passed"] is True, report.text
        run_id = body["tests"][0]["run_id"]
        events = client.get(f"/v1/streams/{run_id}", headers=headers()).json()
        harness_signals = [
            event["data"]
            for event in events
            if event["type"] == "harness.signal"
        ]
        assert {
            (signal["block_type"], signal["signal_type"], signal["status"])
            for signal in harness_signals
        } >= {
            ("permission_gate", "permission", "allowed"),
            ("sandbox_boundary", "sandbox", "declared"),
            ("event_recorder", "event", "recorded"),
        }
        assert "return left + right" in (workspace / "calculator.py").read_text()


def test_idol_daily_workflow_uses_search_evidence_without_legacy_agent(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        services = app.state.services
        services.tools._tools["WebSearch"] = EvidenceWebSearchTool()  # noqa: SLF001

        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "日本女性アイドル Daily",
                "requirement": "每天 8am 搜索日本女性偶像团体资讯并生成带证据链接的日报。",
            },
        ).json()["id"]
        revision = 0
        search_specs = [
            ("search_news", "日本 女性アイドル グループ ニュース 発表"),
            ("search_events", "日本 女性アイドル ライブ イベント ツアー"),
            ("search_members", "日本 女性アイドル 新メンバー 卒業 加入"),
        ]
        nodes = [
            {"id": "schedule", "type": "schedule_trigger", "title": "08:00 JST", "config": {
                "timezone": "Asia/Tokyo",
                "hour": 8,
                "minute": 0,
                "inputs": {"topic": "日本女性偶像团体日报", "language": "zh-CN"},
            }},
            {"id": "context", "type": "context_assembler", "title": "Daily Context", "config": {
                "input": {"$ref": {"node_id": "schedule", "path": ["output"]}},
                "settings": {"fragments": [{"$ref": {"node_id": "schedule", "path": ["topic"]}}]},
            }},
            *[
                {"id": node_id, "type": "tool_executor", "title": f"WebSearch {node_id}", "config": {
                    "input": {"$ref": {"node_id": "context", "path": ["output"]}},
                    "settings": {"tool_name": "WebSearch", "tool_input": {
                        "query": query,
                        "max_results": 2,
                        "language": "ja",
                        "country": "JP",
                    }},
                }}
                for node_id, query in search_specs
            ],
            {"id": "aggregate", "type": "variable_aggregator", "title": "Aggregate Evidence", "config": {
                "variables": [
                    {"$ref": {"node_id": node_id, "path": ["output"]}}
                    for node_id, _ in search_specs
                ],
                "mode": "array",
            }},
            {"id": "compact", "type": "context_compactor", "title": "Compact Evidence", "config": {
                "input": {"$ref": {"node_id": "aggregate", "path": ["output"]}},
                "settings": {
                    "max_chars": 5000,
                    "preserved_facts": ["titles", "urls", "published_at", "source"],
                },
            }},
            {"id": "format", "type": "template_transform", "title": "Evidence-only Report", "config": {
                "template": (
                    "## 日本女性偶像团体日报\n"
                    "定时: 08:00 JST\n"
                    "1. {{ title1 }}\n{{ url1 }}\n"
                    "2. {{ title2 }}\n{{ url2 }}\n"
                    "3. {{ title3 }}\n{{ url3 }}\n"
                    "以上链接均来自本次 WebSearch 证据。"
                ),
                "variables": {
                    "title1": {"$ref": {"node_id": "search_news", "path": ["output", "results", "0", "title"]}},
                    "url1": {"$ref": {"node_id": "search_news", "path": ["output", "results", "0", "url"]}},
                    "title2": {"$ref": {"node_id": "search_events", "path": ["output", "results", "0", "title"]}},
                    "url2": {"$ref": {"node_id": "search_events", "path": ["output", "results", "0", "url"]}},
                    "title3": {"$ref": {"node_id": "search_members", "path": ["output", "results", "0", "title"]}},
                    "url3": {"$ref": {"node_id": "search_members", "path": ["output", "results", "0", "url"]}},
                },
            }},
            {"id": "trace", "type": "event_recorder", "title": "Trace", "config": {
                "input": {"$ref": {"node_id": "format", "path": ["text"]}},
                "settings": {"label": "idol_daily_evidence_report"},
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "report": {"$ref": {"node_id": "format", "path": ["text"]}},
                "topic": {"$ref": {"node_id": "schedule", "path": ["topic"]}},
                "trace": {"$ref": {"node_id": "trace", "path": ["state", "recorded"]}},
            }}},
        ]
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        edges = [
            ("schedule", "context"),
            ("context", "search_news"),
            ("context", "search_events"),
            ("context", "search_members"),
            ("search_news", "aggregate"),
            ("search_events", "aggregate"),
            ("search_members", "aggregate"),
            ("aggregate", "compact"),
            ("compact", "format"),
            ("format", "trace", "text"),
            ("trace", "end"),
        ]
        for edge in edges:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": {
                "id": f"{edge[0]}-{edge[1]}",
                "source": edge[0],
                "target": edge[1],
                "source_port": edge[2] if len(edge) > 2 else "output",
                "target_port": "input",
            }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Idol daily cites search evidence",
            "requirement": "The daily report must run WebSearch and cite only URLs returned by WebSearch.",
            "inputs": {},
            "assertions": [
                {"path": ["topic"], "operator": "equals", "expected": "日本女性偶像团体日报"},
                {"path": ["report"], "operator": "contains", "expected": "08:00 JST"},
                {"path": ["report"], "operator": "contains", "expected": "https://news.example.test"},
                {"path": ["trace"], "operator": "equals", "expected": True},
            ],
            "required_node_types": [
                "schedule_trigger",
                "context_assembler",
                "tool_executor",
                "variable_aggregator",
                "context_compactor",
                "template_transform",
                "event_recorder",
            ],
            "required_tool_nodes": ["WebSearch"],
            "required_tools": ["WebSearch"],
            "minimum_tool_calls": 3,
            "require_cited_tool_urls": True,
        }})

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert "claude_agent" not in {node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
        schedule = next(node for node in draft["snapshot"]["workflow"]["nodes"] if node["type"] == "schedule_trigger")
        assert schedule["config"]["timezone"] == "Asia/Tokyo"
        assert schedule["config"]["hour"] == 8
        validation = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=headers()).json()
        assert validation["valid"] is True, validation
        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["passed"] is True, report.text
        evidence = body["tests"][0]["tool_evidence"]
        assert evidence["citation_passed"] is True
        assert evidence["unverified_output_urls"] == []
        assert evidence["minimum_calls_passed"] is True


def test_builder_uses_incremental_brick_operations_and_publishes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = IncrementalBuilderProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Generated", "requirement": "Build a tested greeting workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a tested greeting workflow.", "auto_publish": True},
        ).json()["build_id"]
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"published", "needs_attention"}:
                break
            time.sleep(0.01)
        assert build["status"] == "published", build
        assert build["team_state"]["published_version"] == 1
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert [node["type"] for node in draft["snapshot"]["workflow"]["nodes"]] == [
            "start", "template_transform", "end"
        ]
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        tools = [event["data"].get("tool") for event in operation_events if event["type"] == "build.operation"]
        assert tools == [name for name, _ in [
            ("draft_add_node", {}), ("draft_add_node", {}), ("draft_add_node", {}),
            ("draft_connect", {}), ("draft_connect", {}), ("test_add", {}),
            ("draft_validate", {}), ("test_run", {}), ("draft_publish", {}),
        ]]


def test_builder_adds_preflight_smoke_test_when_model_omits_tests(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, NoTestBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Generated smoke", "requirement": "Build a greeting workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a greeting workflow.", "auto_publish": False, "max_turns": 8},
        ).json()["build_id"]
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"ready", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "ready", build
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        tests = draft["snapshot"]["tests"]
        assert [test["id"] for test in tests] == ["auto_smoke_acceptance"]
        assert tests[0]["mandatory"] is True
        assert tests[0]["frame"]["category"] == "structure"
        assert set(tests[0]["required_node_types"]) == {"start", "template_transform", "end"}
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        assert any(event["type"] == "build.preflight_test_added" for event in operation_events)


def test_builder_persists_plan_first_build_plan(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, PlanFirstBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Plan first", "requirement": "Build a modular novel generator."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a modular novel generator.", "auto_publish": False, "max_turns": 5},
        ).json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"ready", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        plan = build["team_state"]["build_plan"]
        assert plan["goal"] == "Build a modular novel generator BlockFlow."
        assert plan["complexity"] == "complex"
        assert plan["reuse_depth"] == "shallow"
        assert plan["modules"][0]["id"] == "outline"
        assert plan["modules"][0]["expected_blocks"] == ["start", "model_turn", "end"]

        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        tools = [event["data"].get("tool") for event in operation_events if event["type"] == "build.operation"]
        assert tools[0] == "build_plan"


def test_builder_planning_mode_required_blocks_mutation_before_plan(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, IncrementalBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Plan required", "requirement": "Build a tested greeting workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Build a tested greeting workflow.",
                "auto_publish": False,
                "max_turns": 5,
                "planning_mode": "required",
            },
        ).json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        assert build["team_state"]["planning_mode"] == "required"
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        results = [
            event["data"].get("result", "")
            for event in operation_events
            if event["type"] == "build.operation"
        ]
        assert any("build_plan required before draft_add_node" in result for result in results)


def test_builder_planning_mode_disabled_rejects_build_plan_tool(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, PlanFirstBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Plan disabled", "requirement": "Build a modular workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Build a modular workflow.",
                "auto_publish": False,
                "max_turns": 5,
                "planning_mode": "disabled",
            },
        ).json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        assert build["team_state"]["planning_mode"] == "disabled"
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        results = [
            event["data"].get("result", "")
            for event in operation_events
            if event["type"] == "build.operation"
        ]
        assert any("build_plan is disabled" in result for result in results)


def test_builder_must_read_manual_before_agent_architecture_blocks(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ManualSkippingBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Manual gate", "requirement": "Build a Claude-like loop."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a Claude-like loop.", "auto_publish": True, "max_turns": 5},
        ).json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)
        assert build["status"] == "needs_attention", build
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        results = [
            event["data"].get("result", "")
            for event in operation_events
            if event["type"] == "build.operation"
        ]
        assert any("manual lookup required" in result for result in results)


def test_builder_rejects_tests_requiring_unavailable_node_types(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, InvalidRequiredNodeTestBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Invalid test", "requirement": "Build a tiny workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a tiny workflow.", "auto_publish": False, "max_turns": 6},
        ).json()["build_id"]
        for _ in range(200):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"ready", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "ready", build
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        failed_test_add = [
            event for event in operation_events
            if event["type"] == "build.operation"
            and event["data"].get("tool") == "test_add"
            and event["data"].get("success") is False
        ]
        assert failed_test_add
        result = failed_test_add[0]["data"]["result"]
        assert "test required unavailable node types" in result
        assert "extract_text" in result
        assert "available node types" in result

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        test_ids = [test["id"] for test in draft["snapshot"]["tests"]]
        assert "bad_required_node" not in test_ids
        assert "auto_smoke_acceptance" in test_ids


def test_builder_allows_confirmation_test_after_repair_revision(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, RepairConfirmationBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Repair confirmation", "requirement": "Repair a failing test then confirm."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Repair a failing test then confirm.",
                "auto_publish": False,
                "max_turns": 12,
                "max_repair_cycles": 1,
            },
        ).json()["build_id"]
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"ready", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "ready", build
        assert build["team_state"]["repair_cycles"] == 1
        assert build["team_state"]["last_failed_test_revision"] is None

        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        test_runs = [
            event for event in operation_events
            if event["type"] == "build.operation"
            and event["data"].get("tool") == "test_run"
        ]
        assert len(test_runs) >= 2
        assert '"passed": false' in test_runs[0]["data"]["result"]
        assert '"passed": true' in test_runs[-1]["data"]["result"]


def test_claude_like_coding_agent_template_is_valid_and_covers_architecture() -> None:
    registry = build_block_registry()
    template = registry.expand_template("claude_like_coding_agent", prefix="coding")
    errors = registry.validate_workflow(template)
    assert errors == []
    node_types = {node.type for node in template.nodes}
    nested_loop = next(node for node in template.nodes if node.type == "loop")
    nested_types = {node["type"] for node in nested_loop.config["workflow"]["nodes"]}
    required = {
        "context_assembler",
        "workspace_context_injector",
        "conversation_memory",
        "context_compactor",
        "model_turn",
        "tool_call_router",
        "tool_executor",
        "tool_result_normalizer",
        "permission_gate",
        "sandbox_boundary",
        "skill_loader",
        "mcp_gateway",
        "capability_registry",
        "subagent_spawn",
        "task_dispatcher",
        "mailbox_wait_wake",
        "dependency_gate",
        "budget_gate",
        "round_limit",
        "cancellation_point",
        "checkpoint_resume",
        "event_recorder",
    }
    assert required <= (node_types | nested_types)
    assert nested_loop.config["max_iterations"] > 1


def test_builder_can_expand_claude_like_template_into_editable_draft(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, TemplateExpandBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Template", "requirement": "Expand a Claude-like coding agent template."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Expand a Claude-like coding agent template.", "auto_publish": False, "max_turns": 5},
        ).json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)
        assert build["status"] == "needs_attention", build
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        node_types = {node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
        assert {"context_compactor", "loop", "permission_gate", "subagent_spawn", "mailbox_wait_wake"} <= node_types
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        expand_events = [
            event for event in operation_events
            if event["type"] == "build.operation" and event["data"].get("tool") == "template_expand"
        ]
        assert expand_events and expand_events[0]["data"]["success"] is True


def test_iteration_and_loop_execute_nested_workflows(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Containers", "requirement": "Map items and loop to an explicit exit."},
        ).json()["id"]
        nested_iteration = {
            "nodes": [
                {"id": "nested-start", "type": "start", "title": "Item", "config": {"inputs": [
                    {"name": "item", "type": "string"}, {"name": "index", "type": "number"}
                ]}},
                {"id": "nested-template", "type": "template_transform", "title": "Render", "config": {
                    "template": "Item {{ value }}", "variables": {
                        "value": {"$ref": {"node_id": "nested-start", "path": ["item"]}}
                    }
                }},
                {"id": "nested-end", "type": "end", "title": "Item output", "config": {
                    "outputs": {"value": {"$ref": {"node_id": "nested-template", "path": ["text"]}}}
                }},
            ],
            "edges": [
                {"id": "na", "source": "nested-start", "target": "nested-template", "source_port": "output", "target_port": "input"},
                {"id": "nb", "source": "nested-template", "target": "nested-end", "source_port": "text", "target_port": "input"},
            ],
        }
        nested_loop = {
            "nodes": [
                {"id": "loop-start", "type": "start", "title": "Iteration", "config": {"inputs": [
                    {"name": "iteration", "type": "number"}
                ]}},
                {"id": "loop-end", "type": "end", "title": "Counter", "config": {"outputs": {
                    "current": {"$ref": {"node_id": "loop-start", "path": ["iteration"]}}
                }}},
            ],
            "edges": [{"id": "lc", "source": "loop-start", "target": "loop-end", "source_port": "output", "target_port": "input"}],
        }
        nodes = [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "items", "type": "array"}]}},
            {"id": "iteration", "type": "iteration", "title": "Map", "config": {
                "items": {"$ref": {"node_id": "start", "path": ["items"]}},
                "workflow": nested_iteration, "item_name": "item", "output_node_id": "nested-end",
                "output_path": ["value"], "parallelism": 2,
            }},
            {"id": "loop", "type": "loop", "title": "Count", "config": {
                "workflow": nested_loop, "variables": {},
                "break_condition": {"value": 0, "operator": "gte", "expected": 2},
                "break_value": {"$ref": {"node_id": "loop-end", "path": ["current"]}},
                "max_iterations": 5, "output_node_id": "loop-end",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "mapped": {"$ref": {"node_id": "iteration", "path": ["items"]}},
                "counter": {"$ref": {"node_id": "loop", "path": ["output", "current"]}},
            }}},
        ]
        revision = 0
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "iteration", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "iteration", "target": "loop", "source_port": "items", "target_port": "input"},
            {"id": "c", "source": "loop", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Containers execute", "requirement": "Iteration maps and Loop exits.",
            "inputs": {"items": ["a", "b"]},
            "assertions": [
                {"path": ["mapped"], "operator": "equals", "expected": ["Item a", "Item b"]},
                {"path": ["counter"], "operator": "equals", "expected": 2},
            ],
        }})
        result = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert result.status_code == 200, result.text
        assert result.json()["passed"] is True, result.text


def test_branch_outputs_join_with_variable_aggregator(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Branch join", "requirement": "Join exactly one active branch."},
        ).json()["id"]
        nodes = [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "flag", "type": "boolean"}]}},
            {"id": "branch", "type": "if_else", "title": "Branch", "config": {
                "cases": [{"id": "yes", "conditions": [{"value": {"$ref": {"node_id": "start", "path": ["flag"]}}, "operator": "equals", "expected": True}]}],
                "default_branch": "no",
            }},
            {"id": "yes", "type": "variable_assigner", "title": "Yes", "config": {"assignments": {"value": "approved"}}},
            {"id": "no", "type": "variable_assigner", "title": "No", "config": {"assignments": {"value": "rejected"}}},
            {"id": "join", "type": "variable_aggregator", "title": "Join", "config": {
                "variables": [
                    {"$ref": {"node_id": "yes", "path": ["output", "value"], "optional": True}},
                    {"$ref": {"node_id": "no", "path": ["output", "value"], "optional": True}},
                ], "mode": "first_non_null",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "decision": {"$ref": {"node_id": "join", "path": ["output"]}}
            }}},
        ]
        revision = 0
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "s-b", "source": "start", "target": "branch", "source_port": "output", "target_port": "input"},
            {"id": "b-y", "source": "branch", "target": "yes", "source_port": "branch", "target_port": "input", "branch": "yes"},
            {"id": "b-n", "source": "branch", "target": "no", "source_port": "branch", "target_port": "input", "branch": "no"},
            {"id": "y-j", "source": "yes", "target": "join", "source_port": "output", "target_port": "input"},
            {"id": "n-j", "source": "no", "target": "join", "source_port": "output", "target_port": "input"},
            {"id": "j-e", "source": "join", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        for flag, expected in [(True, "approved"), (False, "rejected")]:
            revision = mutate(client, app_id, revision, "add_test", {"test": {
                "name": f"Decision {flag}", "requirement": "Only the active branch is returned.",
                "inputs": {"flag": flag},
                "assertions": [{"path": ["decision"], "operator": "equals", "expected": expected}],
            }})
        result = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert result.json()["passed"] is True, result.text


def test_daily_schedule_is_persisted_and_deduplicated(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Daily", "requirement": "Run every day at 08:00 Tokyo time."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "schedule", "type": "schedule_trigger", "title": "08:00 JST", "config": {
                "timezone": "Asia/Tokyo", "hour": 8, "minute": 0, "inputs": {"topic": "idols"}
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "topic": {"$ref": {"node_id": "schedule", "path": ["topic"]}}
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "scheduled-end", "source": "schedule", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Scheduled inputs", "requirement": "Schedule defaults reach the result.",
            "inputs": {},
            "assertions": [{"path": ["topic"], "operator": "equals", "expected": "idols"}],
        }})
        assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200
        schedules = client.get("/api/v1/schedules", headers=headers()).json()
        assert schedules[0]["timezone"] == "Asia/Tokyo"
        assert schedules[0]["hour"] == 8

        scheduler = client.app.state.services.scheduler
        assert client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 22, 59, tzinfo=timezone.utc)
        ) == []
        started = client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 23, 0, tzinfo=timezone.utc)
        )
        assert len(started) == 1
        assert client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 23, 30, tzinfo=timezone.utc)
        ) == []
        run_id = started[0]["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert run["outputs"] == {"topic": "idols"}
