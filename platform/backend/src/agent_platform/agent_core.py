from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from typing import Any, TypeAlias

from .models import ContentBlock, ModelResponse, StreamEvent, Usage
from .providers.base import ProviderError


from .json_repair import parse_tool_input

INVALID_TOOL_INPUT_JSON_KEY = "_invalid_tool_input_json"

AgentCoreEventEmitter: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[None]]

_SENSITIVE_FIELD_TOKENS = (
    "key",
    "secret",
    "token",
    "password",
    "authorization",
    "cookie",
    "credential",
)


def redact_sensitive_fields(value: Any) -> Any:
    """Return a recursively redacted copy of JSON-like data."""
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if any(token in str(key).casefold() for token in _SENSITIVE_FIELD_TOKENS)
                else redact_sensitive_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_fields(item) for item in value)
    return value


def merge_usage_payload(usage: Usage, raw_usage: Any) -> None:
    """Merge one provider usage snapshot into a response usage record."""
    if not isinstance(raw_usage, dict):
        return
    integer_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    for field_name in integer_fields:
        value = raw_usage.get(field_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(usage, field_name, int(value))
            usage.field_support[field_name] = "reported"

    details = raw_usage.get("output_tokens_details")
    reasoning = raw_usage.get("reasoning_tokens")
    if reasoning is None and isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
    if isinstance(reasoning, (int, float)) and not isinstance(reasoning, bool):
        usage.reasoning_tokens = int(reasoning)
        usage.field_support["reasoning_tokens"] = "reported"

    input_details = raw_usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        cached = input_details.get("cached_tokens")
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            usage.cache_read_input_tokens = int(cached)
            usage.field_support["cache_read_input_tokens"] = "reported"

    cost = raw_usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage.cost_usd = float(cost)
        usage.cost_source = "provider_reported"
        usage.field_support["cost_usd"] = "reported"


def add_usage(total: Usage, current: Usage) -> None:
    """Add one response's usage to a session total in place."""
    total.input_tokens += current.input_tokens
    total.output_tokens += current.output_tokens
    total.cache_read_input_tokens += current.cache_read_input_tokens
    total.cache_creation_input_tokens += current.cache_creation_input_tokens
    if current.reasoning_tokens is not None:
        total.reasoning_tokens = (total.reasoning_tokens or 0) + current.reasoning_tokens
    total.cost_usd += current.cost_usd
    if current.cost_source == "provider_reported":
        total.cost_source = "provider_reported"
    elif current.cost_source == "estimated_configured_price" and total.cost_source == "unsupported":
        total.cost_source = "estimated_configured_price"
    for field_name, support in current.field_support.items():
        prior = total.field_support.get(field_name)
        if support == "reported" or prior is None:
            total.field_support[field_name] = support
        elif support == "estimated" and prior not in {"reported", "estimated"}:
            total.field_support[field_name] = support


def price_usage(
    usage: Usage,
    model: str,
    price_estimates_usd_per_million: Mapping[str, Mapping[str, float]],
) -> None:
    """Apply configured price estimates when a provider did not report cost."""
    if usage.cost_source == "provider_reported":
        return
    token_fields = ("input_tokens", "output_tokens")
    bare_model = model.split("/", 1)[-1]
    rates = price_estimates_usd_per_million.get(model) or price_estimates_usd_per_million.get(
        bare_model
    )
    valid_rates = isinstance(rates, Mapping) and all(
        isinstance(rates.get(field), (int, float))
        and not isinstance(rates.get(field), bool)
        and float(rates[field]) >= 0
        for field in token_fields
    )
    if (
        not all(usage.field_support.get(field) == "reported" for field in token_fields)
        or not valid_rates
    ):
        usage.cost_usd = 0.0
        usage.cost_source = "unsupported"
        usage.field_support["cost_usd"] = "unsupported"
        return
    assert rates is not None
    input_rate = float(rates["input_tokens"])
    output_rate = float(rates["output_tokens"])
    # 缓存命中部分单独计价：DeepSeek/Anthropic 的 input_tokens 都不含
    # cache_read——不计它就是漏账（缓存纪律生效后一次调用可有 10 万
    # cache_read token）。费率可配，缺省按行业惯例 input 的 1/10。
    cache_read_rate = rates.get("cache_read_input_tokens")
    cache_read_rate = (
        float(cache_read_rate)
        if isinstance(cache_read_rate, (int, float)) and not isinstance(cache_read_rate, bool)
        else input_rate * 0.1
    )
    cache_write_rate = rates.get("cache_creation_input_tokens")
    cache_write_rate = (
        float(cache_write_rate)
        if isinstance(cache_write_rate, (int, float)) and not isinstance(cache_write_rate, bool)
        else input_rate
    )
    usage.cost_usd = (
        usage.input_tokens * input_rate
        + usage.cache_read_input_tokens * cache_read_rate
        + usage.cache_creation_input_tokens * cache_write_rate
        + usage.output_tokens * output_rate
    ) / 1_000_000
    usage.cost_source = "estimated_configured_price"
    usage.field_support["cost_usd"] = "estimated"


async def collect_model_stream(
    stream: AsyncIterable[StreamEvent],
    *,
    emit: AgentCoreEventEmitter | None = None,
    event_prefix: str = "model",
    model: str = "",
    timeout_seconds: float | None = None,
    expose_thinking: bool = False,
    price_estimates_usd_per_million: Mapping[str, Mapping[str, float]] | None = None,
) -> ModelResponse:
    """Collect a provider event stream without depending on platform persistence services."""

    async def emit_event(kind: str, data: dict[str, Any]) -> None:
        if emit is not None:
            await emit(kind, data)

    async def collect() -> ModelResponse:
        blocks: dict[int, ContentBlock] = {}
        input_json: dict[int, str] = {}
        usage = Usage()
        stop_reason: str | None = None
        async for event in stream:
            if not isinstance(event, StreamEvent):
                raise TypeError("model stream yielded a non-StreamEvent value")
            data = event.data
            if event.type == "message_start":
                merge_usage_payload(usage, data.get("message", {}).get("usage", {}))
            elif event.type == "content_block_start":
                index = int(data.get("index", len(blocks)))
                raw = data.get("content_block", {})
                block_type = raw.get("type")
                if block_type == "text":
                    blocks[index] = ContentBlock(type="text", text=raw.get("text", ""))
                elif block_type == "thinking" and expose_thinking:
                    blocks[index] = ContentBlock(type="thinking", thinking=raw.get("thinking", ""))
                elif block_type == "tool_use":
                    blocks[index] = ContentBlock(
                        type="tool_use",
                        id=raw.get("id"),
                        name=raw.get("name"),
                        input=raw.get("input", {}),
                    )
                    input_json[index] = ""
                    await emit_event(
                        "tool.requested",
                        {"tool_use_id": raw.get("id"), "tool": raw.get("name")},
                    )
            elif event.type == "content_block_delta":
                index = int(data.get("index", 0))
                delta = data.get("delta", {})
                block = blocks.get(index)
                if delta.get("type") == "text_delta" and block:
                    value = delta.get("text", "")
                    block.text = (block.text or "") + value
                    await emit_event(f"{event_prefix}.text.delta", {"text": value})
                elif delta.get("type") == "thinking_delta" and block and expose_thinking:
                    value = delta.get("thinking", "")
                    block.thinking = (block.thinking or "") + value
                    await emit_event(f"{event_prefix}.thinking.delta", {"thinking": value})
                elif delta.get("type") == "signature_delta" and block and expose_thinking:
                    block.signature = (block.signature or "") + delta.get("signature", "")
                elif delta.get("type") == "input_json_delta":
                    input_json[index] = input_json.get(index, "") + delta.get("partial_json", "")
            elif event.type == "content_block_stop":
                index = int(data.get("index", 0))
                block = blocks.get(index)
                if block and block.type == "tool_use" and input_json.get(index):
                    raw = input_json[index]
                    parsed, repaired = parse_tool_input(raw)
                    if parsed is not None:
                        block.input = parsed
                        if repaired:
                            await emit_event(
                                "tool.input_json.repaired",
                                {
                                    "tool": block.name,
                                    "raw_length": len(raw),
                                },
                            )
                    else:
                        try:
                            json.loads(raw)
                            error_text = "tool input JSON is not an object"
                        except json.JSONDecodeError as error:
                            error_text = str(error)
                        block.input = {
                            INVALID_TOOL_INPUT_JSON_KEY: {
                                "error": error_text,
                                "raw_preview": raw[:2_000],
                                "raw_length": len(raw),
                            }
                        }
                        await emit_event(
                            "tool.input_json.invalid",
                            {
                                "tool": block.name,
                                "error": error_text,
                                "raw_length": len(raw),
                                "raw_preview": raw[:500],
                            },
                        )
            elif event.type == "message_delta":
                stop_reason = data.get("delta", {}).get("stop_reason", stop_reason)
                merge_usage_payload(usage, data.get("usage", {}))
            elif isinstance(data.get("usage"), dict):
                merge_usage_payload(usage, data["usage"])
            elif event.type == "error":
                raise ProviderError(str(data.get("error", data)))
        response = ModelResponse(
            blocks=[blocks[index] for index in sorted(blocks)],
            stop_reason=stop_reason,
            usage=usage,
        )
        price_usage(response.usage, model, price_estimates_usd_per_million or {})
        return response

    try:
        if timeout_seconds is not None and timeout_seconds > 0:
            async with asyncio.timeout(timeout_seconds):
                return await collect()
        return await collect()
    except TimeoutError as error:
        await emit_event(
            f"{event_prefix}.timeout",
            {"model": model, "timeout_seconds": timeout_seconds},
        )
        timeout_detail = (
            f" after {timeout_seconds:g}s"
            if timeout_seconds is not None and timeout_seconds > 0
            else ""
        )
        raise ProviderError(
            f"model stream timed out{timeout_detail}",
            retryable=True,
        ) from error
    except ProviderError as error:
        await emit_event(
            f"{event_prefix}.failed",
            {
                "model": model,
                "error": str(error),
                "error_type": type(error).__name__,
                "retryable": error.retryable,
                "status_code": error.status_code,
            },
        )
        raise
