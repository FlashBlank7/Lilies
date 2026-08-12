from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_platform.agent_core import (
    INVALID_TOOL_INPUT_JSON_KEY,
    add_usage,
    collect_model_stream,
    merge_usage_payload,
    redact_sensitive_fields,
)
from agent_platform.models import StreamEvent, Usage
from agent_platform.providers.base import ProviderError


async def _model_events() -> AsyncIterator[StreamEvent]:
    yield StreamEvent(
        type="message_start",
        data={
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 3},
                }
            }
        },
    )
    yield StreamEvent(
        type="content_block_start",
        data={
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "private:"},
        },
    )
    yield StreamEvent(
        type="content_block_delta",
        data={
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning"},
        },
    )
    yield StreamEvent(
        type="content_block_delta",
        data={"index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
    )
    yield StreamEvent(
        type="content_block_start",
        data={"index": 1, "content_block": {"type": "text", "text": "Hello"}},
    )
    yield StreamEvent(
        type="content_block_delta",
        data={"index": 1, "delta": {"type": "text_delta", "text": " world"}},
    )
    yield StreamEvent(
        type="content_block_start",
        data={
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "tool-1",
                "name": "Write",
                "input": {},
            },
        },
    )
    yield StreamEvent(
        type="content_block_delta",
        data={
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path" "x"}'},
        },
    )
    yield StreamEvent(type="content_block_stop", data={"index": 2})
    yield StreamEvent(
        type="message_delta",
        data={
            "delta": {"stop_reason": "tool_use"},
            "usage": {
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        },
    )


@pytest.mark.asyncio
async def test_collect_model_stream_hides_private_thinking_by_default() -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        emitted.append((kind, data))

    response = await collect_model_stream(
        _model_events(),
        emit=emit,
        model="provider/test-model",
        price_estimates_usd_per_million={"test-model": {"input_tokens": 1.0, "output_tokens": 2.0}},
    )

    assert [block.type for block in response.blocks] == ["text", "tool_use"]
    assert response.blocks[0].text == "Hello world"
    assert response.blocks[1].input
    assert INVALID_TOOL_INPUT_JSON_KEY in response.blocks[1].input
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.reasoning_tokens == 2
    # 缓存命中 token 按 input 费率 1/10 计入（3 tok cache_read）
    assert response.usage.cost_usd == pytest.approx(0.0000203)
    assert response.usage.cost_source == "estimated_configured_price"
    event_types = [kind for kind, _ in emitted]
    assert "model.thinking.delta" not in event_types
    assert "model.text.delta" in event_types
    assert "tool.requested" in event_types
    assert "tool.input_json.invalid" in event_types


@pytest.mark.asyncio
async def test_collect_model_stream_can_explicitly_expose_thinking() -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, data: dict[str, Any]) -> None:
        emitted.append((kind, data))

    response = await collect_model_stream(
        _model_events(),
        emit=emit,
        expose_thinking=True,
    )

    thinking = response.blocks[0]
    assert thinking.type == "thinking"
    assert thinking.thinking == "private:reasoning"
    assert thinking.signature == "sig"
    assert "model.thinking.delta" in [kind for kind, _ in emitted]


@pytest.mark.asyncio
async def test_collect_model_stream_timeout_is_retryable_and_observable() -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def slow_events() -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(1)
        yield StreamEvent(type="message_start")

    async def emit(kind: str, data: dict[str, Any]) -> None:
        emitted.append((kind, data))

    with pytest.raises(ProviderError, match="model stream timed out") as raised:
        await collect_model_stream(
            slow_events(),
            emit=emit,
            model="slow-model",
            timeout_seconds=0.001,
        )

    assert raised.value.retryable is True
    assert emitted == [
        (
            "model.timeout",
            {"model": "slow-model", "timeout_seconds": 0.001},
        )
    ]


def test_usage_payload_merge_and_total_addition() -> None:
    current = Usage()
    merge_usage_payload(
        current,
        {
            "input_tokens": 12.9,
            "output_tokens": 4,
            "reasoning_tokens": 3,
            "cost_usd": 0.25,
        },
    )
    merge_usage_payload(current, {"input_tokens": True, "output_tokens": "ignored"})
    total = Usage(
        input_tokens=2,
        reasoning_tokens=1,
        cost_usd=0.1,
        cost_source="estimated_configured_price",
        field_support={"cost_usd": "estimated"},
    )

    add_usage(total, current)

    assert total.input_tokens == 14
    assert total.output_tokens == 4
    assert total.reasoning_tokens == 4
    assert total.cost_usd == pytest.approx(0.35)
    assert total.cost_source == "provider_reported"
    assert total.field_support["input_tokens"] == "reported"
    assert total.field_support["cost_usd"] == "reported"


def test_redact_sensitive_fields_is_recursive_and_non_mutating() -> None:
    source = {
        "safe": [
            {"api_token": "abc", "nested": {"password": "pw", "visible": 7}},
            ("plain", {"Authorization": "Bearer secret"}),
        ],
        "credential_id": "credential-1",
    }

    redacted = redact_sensitive_fields(source)

    assert redacted == {
        "safe": [
            {"api_token": "***", "nested": {"password": "***", "visible": 7}},
            ("plain", {"Authorization": "***"}),
        ],
        "credential_id": "***",
    }
    assert source["credential_id"] == "credential-1"


def test_price_usage_charges_cached_tokens() -> None:
    """缓存命中 token 必须计价（缺省 1/10 费率），否则缓存纪律生效后账单漏记大头。"""

    from agent_platform.agent_core import price_usage
    from agent_platform.models import Usage

    usage = Usage(
        input_tokens=10_000,
        output_tokens=1_000,
        cache_read_input_tokens=100_000,
        field_support={"input_tokens": "reported", "output_tokens": "reported"},
    )
    price_usage(usage, "m", {"m": {"input_tokens": 1.0, "output_tokens": 2.0}})
    # 10k×1 + 100k×0.1 + 1k×2 = 22k / 1M = 0.022
    assert abs(usage.cost_usd - 0.022) < 1e-9

    # 显式配置缓存费率时按配置价
    usage2 = Usage(
        input_tokens=10_000,
        output_tokens=0,
        cache_read_input_tokens=100_000,
        field_support={"input_tokens": "reported", "output_tokens": "reported"},
    )
    price_usage(usage2, "m", {"m": {"input_tokens": 1.0, "output_tokens": 2.0, "cache_read_input_tokens": 0.5}})
    assert abs(usage2.cost_usd - 0.06) < 1e-9
