from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..models import ChatMessage, StreamEvent, ToolDefinition
from .base import ModelProvider, ProviderCapabilities, ProviderError


class DeepSeekProvider(ModelProvider):
    """DeepSeek adapter using its Anthropic-compatible Messages endpoint."""

    name = "deepseek"

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=30.0)
        self.transport = transport

    def capabilities(self, model: str) -> ProviderCapabilities:
        # Pricing per million tokens
        if "pro" in model:
            input_price, output_price = 0.435, 0.87
        else:
            input_price, output_price = 0.14, 0.28
        return ProviderCapabilities(
            thinking=True,
            tools=True,
            parallel_tools=True,
            prompt_caching=False,
            images=False,
            max_context_tokens=1_000_000,
            max_output_tokens=384_000,
            input_price_per_1m=input_price,
            output_price_per_1m=output_price,
        )

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
        if not self.api_key:
            raise ProviderError("DEEPSEEK_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [self._message_payload(message) for message in messages],
            "stream": True,
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "output_config": {"effort": effort},
        }
        if tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if user_id:
            payload["metadata"] = {"user_id": self._safe_user_id(user_id)}

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/v1/messages", headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:2000]
                        raise ProviderError(
                            f"DeepSeek API returned {response.status_code}: {body}",
                            retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
                            status_code=response.status_code,
                        )
                    event_name: str | None = None
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith(":"):
                            continue
                        if not line:
                            if data_lines:
                                raw = "\n".join(data_lines)
                                if raw != "[DONE]":
                                    try:
                                        data = json.loads(raw)
                                    except json.JSONDecodeError as error:
                                        raise ProviderError(f"invalid DeepSeek SSE payload: {error}") from error
                                    yield StreamEvent(type=event_name or data.get("type", "unknown"), data=data)
                            event_name, data_lines = None, []
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    if data_lines:
                        data = json.loads("\n".join(data_lines))
                        yield StreamEvent(type=event_name or data.get("type", "unknown"), data=data)
        except httpx.TimeoutException as error:
            raise ProviderError("DeepSeek request timed out", retryable=True) from error
        except httpx.NetworkError as error:
            raise ProviderError(f"DeepSeek network error: {error}", retryable=True) from error

    @staticmethod
    def _safe_user_id(value: str) -> str:
        return "".join(char for char in value if char.isalnum() or char in "-_")[:512]

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            value = block.model_dump(mode="json", exclude_none=True)
            if block.type == "text":
                value = {"type": "text", "text": block.text or ""}
            elif block.type == "thinking":
                value = {
                    "type": "thinking",
                    "thinking": block.thinking or "",
                    **({"signature": block.signature} if block.signature else {}),
                }
            elif block.type == "tool_use":
                value = {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input or {},
                }
            elif block.type == "tool_result":
                value = {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content or "",
                    **({"is_error": True} if block.is_error else {}),
                }
            blocks.append(value)
        return {"role": message.role, "content": blocks}
