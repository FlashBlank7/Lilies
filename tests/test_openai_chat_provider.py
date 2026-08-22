"""OpenAIChatProvider：chat-completions 流翻译成内部 Anthropic 形状事件、
消息格式互转、回环地址豁免 egress 开关。全部走 httpx.MockTransport，零真实流量。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent_platform.agent_core import collect_model_stream
from agent_platform.models import ChatMessage, ContentBlock, ToolDefinition
from agent_platform.providers.base import ProviderError
from agent_platform.providers.openai_chat import OpenAIChatProvider


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _provider(sse_body: bytes, captured: dict[str, Any] | None = None) -> OpenAIChatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
            captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    return OpenAIChatProvider(
        api_key="test-key",
        base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(handler),
        egress_enabled=False,  # 回环地址必须豁免
    )


def test_stream_translates_text_tool_calls_usage_and_stop_reason() -> None:
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "让我"}}]},
        {"choices": [{"index": 0, "delta": {"content": "加个节点。"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call_1",
            "function": {"name": "draft_add_node", "arguments": "{\"node\": {\"id\""},
        }]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": ": \"n1\"}}"},
        }]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {
            "prompt_tokens": 42,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 30},
        }},
    ]
    captured: dict[str, Any] = {}
    provider = _provider(_sse(chunks), captured)

    async def run():
        stream = provider.stream(
            model="Qwen/Qwen3.5-4B",
            system="你是构建者。",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="搭一个")])],
            tools=[ToolDefinition(name="draft_add_node", description="add", input_schema={"type": "object"})],
            max_output_tokens=1024,
            thinking_enabled=False,
            effort="high",
            tool_choice={"type": "auto"},
            user_id="build-1-coordinator",
        )
        return await collect_model_stream(stream, model="local/Qwen/Qwen3.5-4B")

    response = asyncio.run(run())

    assert response.stop_reason == "tool_use"
    text_blocks = [b for b in response.blocks if b.type == "text"]
    tool_blocks = [b for b in response.blocks if b.type == "tool_use"]
    assert text_blocks[0].text == "让我加个节点。"
    assert tool_blocks[0].name == "draft_add_node"
    assert tool_blocks[0].id == "call_1"
    assert tool_blocks[0].input == {"node": {"id": "n1"}}  # 分片 JSON 正确拼接解析
    assert response.usage.input_tokens == 42
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_input_tokens == 30

    # 请求体是标准 chat-completions 形状
    assert captured["url"].endswith("/v1/chat/completions")
    payload = captured["payload"]
    assert payload["messages"][0] == {"role": "system", "content": "你是构建者。"}
    assert payload["tools"][0]["type"] == "function"
    assert payload["stream_options"] == {"include_usage": True}


def test_chat_messages_translate_tool_use_and_tool_result() -> None:
    messages = [
        ChatMessage(role="user", content=[ContentBlock(type="text", text="开始")]),
        ChatMessage(role="assistant", content=[
            ContentBlock(type="thinking", thinking="内部思考不应出现"),
            ContentBlock(type="text", text="加节点"),
            ContentBlock(type="tool_use", id="call_9", name="draft_add_node", input={"node": {"id": "n1"}}),
        ]),
        ChatMessage(role="user", content=[
            ContentBlock(type="tool_result", tool_use_id="call_9", content=[{"type": "text", "text": "ok"}]),
            ContentBlock(type="text", text="继续"),
        ]),
    ]
    result = OpenAIChatProvider._chat_messages("sys", messages)

    assert result[0] == {"role": "system", "content": "sys"}
    assert result[1] == {"role": "user", "content": "开始"}
    assistant = result[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "加节点"
    assert "思考" not in json.dumps(assistant, ensure_ascii=False)
    assert assistant["tool_calls"][0]["id"] == "call_9"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"node": {"id": "n1"}}
    # tool_result → 独立 role=tool 消息，且排在同轮 user 文本之前
    assert result[3] == {"role": "tool", "tool_call_id": "call_9", "content": "ok"}
    assert result[4] == {"role": "user", "content": "继续"}


def test_reasoning_content_becomes_thinking_block() -> None:
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "想一想"}}]},
        {"choices": [{"index": 0, "delta": {"content": "答案"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    provider = _provider(_sse(chunks))

    async def run():
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=True, effort="high",
        )
        return await collect_model_stream(stream, model="local/m", expose_thinking=True)

    response = asyncio.run(run())
    assert response.stop_reason == "end_turn"
    assert [b.type for b in response.blocks] == ["thinking", "text"]
    assert response.blocks[0].thinking == "想一想"
    assert response.blocks[1].text == "答案"


def test_remote_endpoint_blocked_without_egress_but_loopback_exempt() -> None:
    remote = OpenAIChatProvider(
        api_key="k", base_url="https://api.example.com/v1", egress_enabled=False
    )

    async def drain(provider: OpenAIChatProvider):
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=8, thinking_enabled=False, effort="high",
        )
        return [event async for event in stream]

    with pytest.raises(ProviderError, match="egress is disabled"):
        asyncio.run(drain(remote))

    # 回环端点在 egress 关闭时照常工作（MockTransport 保证零真实流量）
    loopback = _provider(_sse([{"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]))
    events = asyncio.run(drain(loopback))
    assert any(event.type == "content_block_delta" for event in events)
