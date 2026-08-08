"""模型环节思考失控自愈：截断出空正文 → 关思考重试一次 → 还空才诚实失败。

idol 工作流的真实病例：deepseek 在"逐条评分"任务上思考烧光 16k 预算，
正文为空但节点"成功完成"——静默垃圾。现在第一次截断自动降级重试，
两次都空才抛可读中文错误。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_platform.models import ContentBlock, ModelResponse, Usage
from agent_platform.workflow_runtime import WorkflowRuntime


class _StubSelf:
    """只带 _model_text 所需成员的最小载体。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.stream_calls: list[dict[str, Any]] = []
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class _Provider:
            def stream(self, **kwargs: Any) -> Any:
                outer.stream_calls.append(kwargs)
                return object()

            def provider_name_for(self, model: str) -> str:
                return "stub"

        class _AgentRuntime:
            async def _collect_stream(self, run_id: str, stream: Any, prefix: str, model: str) -> ModelResponse:
                return outer._responses[len(outer.stream_calls) - 1]

        class _Harness:
            async def record_usage(self, *args: Any, **kwargs: Any) -> None: ...
            async def record_model_usage(self, *args: Any, **kwargs: Any) -> None: ...

        self.provider = _Provider()
        self.agent_runtime = _AgentRuntime()
        self.harness = _Harness()

    async def _emit(self, run_id: str, kind: str, data: dict[str, Any]) -> None:
        self.emitted.append((kind, data))


def _resp(text: str, stop_reason: str) -> ModelResponse:
    blocks = [ContentBlock(type="text", text=text)] if text else []
    return ModelResponse(blocks=blocks, stop_reason=stop_reason, usage=Usage())


def test_truncated_empty_text_retries_without_thinking() -> None:
    stub = _StubSelf([_resp("", "max_tokens"), _resp('{"ok":true}', "end_turn")])
    text, _ = asyncio.run(
        WorkflowRuntime._model_text(stub, "run-1", "m", "sys", "prompt", "analyze")  # type: ignore[arg-type]
    )
    assert text == '{"ok":true}'
    assert [c["thinking_enabled"] for c in stub.stream_calls] == [True, False]
    assert any(kind == "node.model.retry_no_thinking" for kind, _ in stub.emitted)


def test_healthy_first_attempt_does_not_retry() -> None:
    stub = _StubSelf([_resp("正文", "end_turn")])
    text, _ = asyncio.run(
        WorkflowRuntime._model_text(stub, "run-1", "m", "sys", "prompt", "analyze")  # type: ignore[arg-type]
    )
    assert text == "正文"
    assert len(stub.stream_calls) == 1


def test_double_truncation_raises_readable_chinese_error() -> None:
    stub = _StubSelf([_resp("", "max_tokens"), _resp("", "max_tokens")])
    with pytest.raises(RuntimeError, match="输出预算"):
        asyncio.run(
            WorkflowRuntime._model_text(stub, "run-1", "m", "sys", "prompt", "analyze")  # type: ignore[arg-type]
        )


def test_honest_empty_without_truncation_passes_through() -> None:
    # 空正文但不是截断（模型真的回了空）→ 保持原样返回，交给下游诚实处理
    stub = _StubSelf([_resp("", "end_turn")])
    text, _ = asyncio.run(
        WorkflowRuntime._model_text(stub, "run-1", "m", "sys", "prompt", "analyze")  # type: ignore[arg-type]
    )
    assert text == ""
    assert len(stub.stream_calls) == 1
