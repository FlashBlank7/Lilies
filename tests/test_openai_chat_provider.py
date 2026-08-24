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


def test_non_streaming_mode_translates_full_completion() -> None:
    """streaming=False：整包响应合成同一事件契约；工具入参整段下发无分片拼装。

    背景：vLLM 0.8.5 hermes 流式解析器丢参数片段（上游 #19056），把小模型的
    完好嵌套 JSON 拼成残品——非流式路径彻底绕开该族 bug。
    """

    completion = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "reasoning_content": "先想一下",
                "content": "正在添加节点",
                "tool_calls": [{
                    "id": "call_0",
                    "type": "function",
                    "function": {
                        "name": "draft_add_node",
                        "arguments": json.dumps({"node": {
                            "id": "start", "type": "start", "title": "输入",
                            "config": {"inputs": [{"name": "name", "example": "Ada"}]},
                        }}, ensure_ascii=False),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=completion)

    provider = OpenAIChatProvider(
        api_key="test-key",
        base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(handler),
        egress_enabled=False,
        streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="Qwen/tiny", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="hi")])],
            tools=[ToolDefinition(name="draft_add_node", description="d",
                                  input_schema={"type": "object"})],
            max_output_tokens=256, thinking_enabled=False, effort="low",
        )
        return await collect_model_stream(stream, model="Qwen/tiny", expose_thinking=True)

    result = asyncio.run(run())
    # 请求体明确非流式
    assert captured["payload"]["stream"] is False
    assert "stream_options" not in captured["payload"]
    # 事件契约与流式路径一致：thinking + text + 完整嵌套的 tool_use
    kinds = [block.type for block in result.blocks]
    assert kinds == ["thinking", "text", "tool_use"]
    tool = result.blocks[2]
    assert tool.name == "draft_add_node"
    assert tool.input["node"]["id"] == "start"
    assert tool.input["node"]["config"]["inputs"][0]["example"] == "Ada"
    assert result.stop_reason == "tool_use"
    assert result.usage.input_tokens == 11 and result.usage.output_tokens == 7


def test_non_streaming_recovers_textified_tool_calls_and_strips_think() -> None:
    """真实事故复现：vLLM 0.8.5 下 <think> 带崩 hermes 解析器，模型的
    <tool_call> 以纯文本留在正文、tool_calls 为空、finish_reason=stop——
    循环判"收工"，构建 stopped before mandatory tests passed。
    兜底解析必须：剥思考、抽出全部调用、正文清干净、stop_reason 归 tool_use。"""

    content = (
        "<think>先分析一下，用 template_transform 替换。</think>\n"
        '<tool_call>\n{"name": "catalog_get", "arguments": {"type": "template_transform"}}\n</tool_call>\n'
        '<tool_call>\n{"name": "draft_add_node", "arguments": {"node": {"id": "t1", '
        '"type": "template_transform", "config": {"template": "Hello {{ name }}"}}}}\n</tool_call>'
    )
    completion = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": content, "tool_calls": []}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 9},
    }
    provider = OpenAIChatProvider(
        api_key="k", base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion)),
        egress_enabled=False, streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=True, effort="high",
        )
        return await collect_model_stream(stream, model="m", expose_thinking=True)

    result = asyncio.run(run())
    kinds = [b.type for b in result.blocks]
    assert kinds == ["thinking", "tool_use", "tool_use"], kinds
    assert "template_transform" in result.blocks[0].thinking  # 思考进思考块
    assert result.blocks[1].name == "catalog_get"
    assert result.blocks[2].name == "draft_add_node"
    assert result.blocks[2].input["node"]["config"]["template"] == "Hello {{ name }}"
    # 正文里不残留 <tool_call>/<think>
    assert not any(b.type == "text" for b in result.blocks)
    assert result.stop_reason == "tool_use"


def test_non_streaming_repairs_malformed_inline_tool_call() -> None:
    """4B 实测：文本化 <tool_call> 里的 JSON 少一个闭合括号（复杂上下文下高频）。
    裸 json.loads 会丢弃调用并清空正文，平台只看到"没有调用任何工具"、无从反馈。
    兜底解析必须走平台自带的 json_repair。"""

    broken = (
        '<tool_call>\n{"name": "draft_update_node", "arguments": {"node_id": "end", '
        '"changes": {"config": {"outputs": {"total": "number"}}}}\n</tool_call>'
    )  # 少一个 }
    completion = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": broken, "tool_calls": []}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 51},
    }
    provider = OpenAIChatProvider(
        api_key="k", base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion)),
        egress_enabled=False, streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=False, effort="low",
        )
        return await collect_model_stream(stream, model="m")

    result = asyncio.run(run())
    calls = [b for b in result.blocks if b.type == "tool_use"]
    assert calls, "缺闭合括号的调用必须被修复回来"
    assert calls[0].name == "draft_update_node"
    assert calls[0].input["node_id"] == "end"
    assert result.stop_reason == "tool_use"


def test_non_streaming_keeps_text_when_salvage_fails() -> None:
    """一个都救不回来时保留原文——宁可让模型看到"格式坏了"，也不要静默空轮。"""

    completion = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "<tool_call>完全不是JSON</tool_call>",
                                 "tool_calls": []}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 5},
    }
    provider = OpenAIChatProvider(
        api_key="k", base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion)),
        egress_enabled=False, streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=False, effort="low",
        )
        return await collect_model_stream(stream, model="m")

    result = asyncio.run(run())
    texts = [b for b in result.blocks if b.type == "text"]
    assert texts and "完全不是JSON" in (texts[0].text or "")


def test_non_streaming_repairs_malformed_inline_tool_call() -> None:
    """4B 实测：文本化 <tool_call> 里的 JSON 少一个闭合括号（复杂上下文下高频）。
    裸 json.loads 会丢弃调用并清空正文，平台只看到"没有调用任何工具"、无从反馈。
    兜底解析必须走平台自带的 json_repair。"""

    broken = (
        '<tool_call>\n{"name": "draft_update_node", "arguments": {"node_id": "end", '
        '"changes": {"config": {"outputs": {"total": "number"}}}}\n</tool_call>'
    )  # 少一个 }
    completion = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": broken, "tool_calls": []}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 51},
    }
    provider = OpenAIChatProvider(
        api_key="k", base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion)),
        egress_enabled=False, streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=False, effort="low",
        )
        return await collect_model_stream(stream, model="m")

    result = asyncio.run(run())
    calls = [b for b in result.blocks if b.type == "tool_use"]
    assert calls, "缺闭合括号的调用必须被修复回来"
    assert calls[0].name == "draft_update_node"
    assert calls[0].input["node_id"] == "end"
    assert result.stop_reason == "tool_use"


def test_non_streaming_keeps_text_when_salvage_fails() -> None:
    """一个都救不回来时保留原文——宁可让模型看到"格式坏了"，也不要静默空轮。"""

    completion = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "<tool_call>完全不是JSON</tool_call>",
                                 "tool_calls": []}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 5},
    }
    provider = OpenAIChatProvider(
        api_key="k", base_url="http://127.0.0.1:8001/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion)),
        egress_enabled=False, streaming=False,
    )

    async def run() -> Any:
        stream = provider.stream(
            model="m", system="s",
            messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text="q")])],
            tools=[], max_output_tokens=64, thinking_enabled=False, effort="low",
        )
        return await collect_model_stream(stream, model="m")

    result = asyncio.run(run())
    texts = [b for b in result.blocks if b.type == "text"]
    assert texts and "完全不是JSON" in (texts[0].text or "")
