"""原生 OpenAI chat-completions 协议适配器 — 本地小模型的标准接入口。

动机（2026-08）：平台内部事件契约是 Anthropic Messages 形状（collect_model_stream
消费 content_block_* / message_delta），而本地推理生态（vLLM / SGLang / Ollama /
LM Studio）的通用协议是 OpenAI /v1/chat/completions。经典 builder 走 Anthropic
线上格式只是历史路径——协议抽象是我们自己的，后端说什么格式是后端的事。
本适配器把 chat-completions 的流式 chunk 翻译成内部事件流，小模型即插即用。

Egress 纪律：MODEL_EGRESS_ENABLED 的本意是防意外扣费。本地回环地址上的推理
不是计费出口，因此 127.0.0.1 / localhost 端点豁免该开关；远程端点照常拦截。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from ..models import ChatMessage, ContentBlock, StreamEvent, ToolDefinition
from .base import ModelProvider, ProviderCapabilities, ProviderError

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
}


def _is_loopback(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


class OpenAIChatProvider(ModelProvider):
    """任意 OpenAI 兼容端点（vLLM/SGLang/Ollama/官方 API）的流式适配器。"""

    name = "local"

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
        egress_enabled: bool | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.api_key = api_key or "local"
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=30.0)
        self.transport = transport
        if provider_name:
            self.name = provider_name
        if egress_enabled is None:
            configured = os.getenv(
                "LILIES_MODEL_EGRESS_ENABLED",
                os.getenv("MODEL_EGRESS_ENABLED", "false"),
            )
            egress_enabled = configured.strip().lower() in {"1", "true", "yes", "on"}
        self.egress_enabled = bool(egress_enabled)

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=False,
            tools=True,
            parallel_tools=True,
            prompt_caching=False,
            images=False,
            max_context_tokens=131_072,
            max_output_tokens=32_768,
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
        if not self.egress_enabled and not _is_loopback(self.base_url):
            raise ProviderError(
                "model egress is disabled; local loopback endpoints are exempt — "
                "set MODEL_EGRESS_ENABLED=true only for an authorized remote model run"
            )

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": self._chat_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = self._tool_choice(tool_choice)
        if user_id:
            payload["user"] = self._safe_user_id(user_id)

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:2000]
                        raise ProviderError(
                            f"OpenAI-compatible API returned {response.status_code}: {body}",
                            retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
                            status_code=response.status_code,
                        )
                    async for event in self._translate_sse(response):
                        yield event
        except httpx.TimeoutException as error:
            raise ProviderError("OpenAI-compatible request timed out", retryable=True) from error
        except httpx.NetworkError as error:
            raise ProviderError(f"OpenAI-compatible network error: {error}", retryable=True) from error

    async def _translate_sse(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        """把 chat.completion.chunk 序列翻译成内部 Anthropic 形状事件流。

        内部块索引与 OpenAI 的 tool_calls[].index 解耦：文本块、思考块各占一个
        内部索引，工具调用按到达顺序分配后续索引；所有 content_block_stop 在
        流末统一补发（collect_model_stream 在 stop 时才解析工具入参 JSON）。
        """

        yield StreamEvent(type="message_start", data={"message": {"usage": {}}})

        next_index = 0
        text_index: int | None = None
        thinking_index: int | None = None
        tool_indexes: dict[int, int] = {}
        opened: list[int] = []
        stop_reason: str | None = None
        usage_payload: dict[str, Any] = {}

        async for line in response.aiter_lines():
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ProviderError(f"invalid OpenAI-compatible SSE payload: {error}") from error

            if isinstance(chunk.get("usage"), dict):
                usage_payload = self._usage(chunk["usage"])
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if thinking_index is None:
                    thinking_index = next_index
                    next_index += 1
                    opened.append(thinking_index)
                    yield StreamEvent(
                        type="content_block_start",
                        data={
                            "index": thinking_index,
                            "content_block": {"type": "thinking", "thinking": ""},
                        },
                    )
                yield StreamEvent(
                    type="content_block_delta",
                    data={
                        "index": thinking_index,
                        "delta": {"type": "thinking_delta", "thinking": reasoning},
                    },
                )

            content = delta.get("content")
            if isinstance(content, str) and content:
                if text_index is None:
                    text_index = next_index
                    next_index += 1
                    opened.append(text_index)
                    yield StreamEvent(
                        type="content_block_start",
                        data={"index": text_index, "content_block": {"type": "text", "text": ""}},
                    )
                yield StreamEvent(
                    type="content_block_delta",
                    data={"index": text_index, "delta": {"type": "text_delta", "text": content}},
                )

            for call in delta.get("tool_calls") or []:
                call_index = int(call.get("index", 0))
                if call_index not in tool_indexes:
                    tool_indexes[call_index] = next_index
                    next_index += 1
                    opened.append(tool_indexes[call_index])
                    function = call.get("function") or {}
                    yield StreamEvent(
                        type="content_block_start",
                        data={
                            "index": tool_indexes[call_index],
                            "content_block": {
                                "type": "tool_use",
                                "id": call.get("id") or f"call_{call_index}",
                                "name": function.get("name") or "",
                                "input": {},
                            },
                        },
                    )
                arguments = (call.get("function") or {}).get("arguments")
                if isinstance(arguments, str) and arguments:
                    yield StreamEvent(
                        type="content_block_delta",
                        data={
                            "index": tool_indexes[call_index],
                            "delta": {"type": "input_json_delta", "partial_json": arguments},
                        },
                    )

            finish = choice.get("finish_reason")
            if finish:
                stop_reason = _STOP_REASON_MAP.get(str(finish), str(finish))

        for index in opened:
            yield StreamEvent(type="content_block_stop", data={"index": index})
        yield StreamEvent(
            type="message_delta",
            data={
                "delta": {"stop_reason": stop_reason or "end_turn"},
                "usage": usage_payload,
            },
        )

    @staticmethod
    def _usage(raw: dict[str, Any]) -> dict[str, Any]:
        usage: dict[str, Any] = {
            "input_tokens": int(raw.get("prompt_tokens") or 0),
            "output_tokens": int(raw.get("completion_tokens") or 0),
        }
        details = raw.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens"):
            usage["cache_read_input_tokens"] = int(details["cached_tokens"])
        completion_details = raw.get("completion_tokens_details")
        if isinstance(completion_details, dict) and completion_details.get("reasoning_tokens"):
            usage["reasoning_tokens"] = int(completion_details["reasoning_tokens"])
        return usage

    @staticmethod
    def _tool_choice(tool_choice: dict[str, str] | None) -> Any:
        kind = (tool_choice or {}).get("type", "auto")
        if kind == "any":
            return "required"
        if kind == "tool" and tool_choice and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        return "auto"

    @staticmethod
    def _safe_user_id(value: str) -> str:
        return "".join(char for char in value if char.isalnum() or char in "-_")[:512]

    @classmethod
    def _chat_messages(cls, system: str, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """内部 ChatMessage（Anthropic 形状块）→ OpenAI messages。

        - assistant 的 tool_use 块 → tool_calls；thinking 块丢弃（协议无槽位）。
        - user 消息里的 tool_result 块 → 独立的 role=tool 消息（OpenAI 约定），
          且必须排在同一消息的普通文本之前，紧跟上一条 assistant 的 tool_calls。
        """

        result: list[dict[str, Any]] = []
        if system.strip():
            result.append({"role": "system", "content": system})
        for message in messages:
            if message.role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in message.content:
                    if block.type == "text" and block.text:
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.id or f"call_{len(tool_calls)}",
                                "type": "function",
                                "function": {
                                    "name": block.name or "",
                                    "arguments": json.dumps(block.input or {}, ensure_ascii=False),
                                },
                            }
                        )
                entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                result.append(entry)
                continue
            text_parts = []
            for block in message.content:
                if block.type == "tool_result":
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.tool_use_id or "",
                            "content": cls._tool_result_text(block),
                        }
                    )
                elif block.type == "text" and block.text:
                    text_parts.append(block.text)
            if text_parts:
                result.append({"role": "user", "content": "\n".join(text_parts)})
        return result

    @staticmethod
    def _tool_result_text(block: ContentBlock) -> str:
        content = block.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            return "\n".join(parts)
        return ""
