import json

import httpx
import pytest

from agent_platform.models import ChatMessage, ContentBlock, ToolDefinition
from agent_platform.providers.deepseek import DeepSeekProvider


@pytest.mark.asyncio
async def test_deepseek_stream_and_reasoning_tool_roundtrip() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = "\n".join(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"done"}}',
                "",
                "event: message_delta",
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
                "",
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    provider = DeepSeekProvider(
        "test-key",
        "https://api.deepseek.test/anthropic",
        transport=httpx.MockTransport(handler),
    )
    messages = [
        ChatMessage(
            role="assistant",
            content=[
                ContentBlock(type="thinking", thinking="reason", signature="sig"),
                ContentBlock(type="tool_use", id="call-1", name="Read", input={"path": "a.py"}),
            ],
        ),
        ChatMessage(
            role="user",
            content=[ContentBlock(type="tool_result", tool_use_id="call-1", content="contents")],
        ),
    ]
    events = [
        event
        async for event in provider.stream(
            model="deepseek-v4-flash",
            system="system",
            messages=messages,
            tools=[ToolDefinition(name="Read", description="read", input_schema={"type": "object"})],
            max_output_tokens=1000,
            thinking_enabled=True,
            effort="high",
        )
    ]
    assert [event.type for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "message_delta",
        "message_stop",
    ]
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["messages"][0]["content"][0] == {
        "type": "thinking",
        "thinking": "reason",
        "signature": "sig",
    }
    assert captured["messages"][1]["content"][0]["tool_use_id"] == "call-1"
