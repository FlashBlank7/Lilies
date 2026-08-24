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
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from ..json_repair import parse_tool_input
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
        streaming: bool = True,
    ) -> None:
        self.api_key = api_key or "local"
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=30.0)
        self.transport = transport
        # streaming=False：整包请求后本地合成事件流。vLLM 0.8.5 的 hermes 流式
        # 工具解析器会丢参数片段（上游 #19056，修复于 v0.11.0），把 4B 的完好
        # 输出拼成残 JSON——曾被错怪为"小模型拍平嵌套结构"。构建是后台任务，
        # 本地端点无流式体验需求，非流式一并绕开这族 bug。
        self.streaming = streaming
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
        temperature: float | None = None,
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
        }
        if self.streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        else:
            payload["stream"] = False
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
        if temperature is not None:
            # 提案竞争用：同一提示不同温度拿廉价多样性（实测多温度较单温度
            # +7.3 分，arXiv:2510.02611）。验证器裁决，第一个过关的落库。
            payload["temperature"] = float(temperature)
        if not thinking_enabled and "qwen3" in model.lower():
            # Qwen3 默认开思考：不关掉的话长推理会吃光 max_tokens，工具调用还没
            # 吐出来就截断（实测 32B 修理手连续 6 轮零调用，thinking 断在半句）。
            # 关闭走官方 chat_template_kwargs，比单纯加大预算省得多。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if temperature is not None:
            # 提案竞争用：同一提示不同温度拿廉价多样性（实测多温度较单温度
            # +7.3 分，arXiv:2510.02611）。验证器裁决，第一个过关的落库。
            payload["temperature"] = float(temperature)
        if not thinking_enabled and "qwen3" in model.lower():
            # Qwen3 默认开思考：不关掉的话长推理会吃光 max_tokens，工具调用还没
            # 吐出来就截断（实测 32B 修理手连续 6 轮零调用，thinking 断在半句）。
            # 关闭走官方 chat_template_kwargs，比单纯加大预算省得多。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if temperature is not None:
            # 提案竞争用：同一提示不同温度拿廉价多样性（实测多温度较单温度
            # +7.3 分，arXiv:2510.02611）。验证器裁决，第一个过关的落库。
            payload["temperature"] = float(temperature)
        if not thinking_enabled and "qwen3" in model.lower():
            # Qwen3 默认开思考：不关掉的话长推理会吃光 max_tokens，工具调用还没
            # 吐出来就截断（实测 32B 修理手连续 6 轮零调用，thinking 断在半句）。
            # 关闭走官方 chat_template_kwargs，比单纯加大预算省得多。
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                if not self.streaming:
                    response = await client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=payload
                    )
                    if response.status_code >= 400:
                        body = response.text[:2000]
                        raise ProviderError(
                            f"OpenAI-compatible API returned {response.status_code}: {body}",
                            retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
                            status_code=response.status_code,
                        )
                    for event in self._translate_completion(response.json()):
                        yield event
                    return
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

    def _translate_completion(self, data: dict[str, Any]) -> list[StreamEvent]:
        """把非流式 chat.completion 整包响应合成为内部事件序列。

        与 _translate_sse 产出同一契约：message_start → 各 content_block
        start/delta/stop → message_delta(stop_reason+usage)。工具入参整段作为
        单个 input_json_delta 下发——没有分片，也就没有分片拼装 bug。
        """

        events: list[StreamEvent] = [
            StreamEvent(type="message_start", data={"message": {"usage": {}}})
        ]
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        content = message.get("content")
        tool_calls = list(message.get("tool_calls") or [])
        inline_think = ""
        if isinstance(content, str) and content:
            # 思考剥离：无 qwen3 reasoning parser 的 vLLM 会把 <think> 全文混进
            # 正文——那是内部推理，绝不能进业主可见叙述。
            if "</think>" in content:
                think_part, _, content = content.partition("</think>")
                inline_think = think_part.replace("<think>", "").strip()
                content = content.strip()
            # 工具调用兜底解析（Qwen 官方明言"生产环境建议自行再解析"）：
            # 思考标签会带崩 hermes 解析器，模型的 <tool_call> 以纯文本留在正文，
            # 循环看到零调用即判收工——真实事故：五个调用全文本化，构建
            # "stopped before mandatory tests passed"。正文里有 <tool_call> 而
            # tool_calls 为空时，自行抽取为真调用。
            if not tool_calls and "<tool_call>" in content:
                # 分隔符非贪婪、内容整段捕获：早期写成 \{.*?\} 会在嵌套 JSON 的
                # 第一个 } 截断，解析失败后调用被丢弃、正文又被清空——平台只看到
                # "没有调用任何工具"（复杂任务里连丢 29 轮）。
                for position, raw in enumerate(
                    re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.S)
                ):
                    # 走平台自带的 JSON 修复：4B 实测会漏掉最外层闭合括号
                    # （复杂上下文下 29/44 轮如此），裸 json.loads 直接丢弃调用、
                    # 正文又被清空——平台只看到"没有调用任何工具"，无从反馈。
                    parsed, _ = parse_tool_input(raw)
                    if not isinstance(parsed, dict) or not parsed.get("name"):
                        continue
                    tool_calls.append({
                        "id": f"inline_call_{position}",
                        "function": {
                            "name": str(parsed.get("name") or ""),
                            "arguments": json.dumps(
                                parsed.get("arguments") or {}, ensure_ascii=False
                            ),
                        },
                    })
                if tool_calls:
                    content = re.sub(
                        r"<tool_call>.*?</tool_call>", "", content, flags=re.S
                    ).strip()
                # 一个都没救回来时保留原文：宁可让模型看到"你的工具调用格式坏了"，
                # 也不要静默变成空轮。
        index = 0
        opened: list[int] = []

        reasoning = message.get("reasoning_content")
        if not reasoning and inline_think:
            reasoning = inline_think
        if isinstance(reasoning, str) and reasoning:
            events.append(StreamEvent(type="content_block_start", data={
                "index": index, "content_block": {"type": "thinking", "thinking": ""},
            }))
            events.append(StreamEvent(type="content_block_delta", data={
                "index": index, "delta": {"type": "thinking_delta", "thinking": reasoning},
            }))
            opened.append(index)
            index += 1

        if isinstance(content, str) and content:
            events.append(StreamEvent(type="content_block_start", data={
                "index": index, "content_block": {"type": "text", "text": ""},
            }))
            events.append(StreamEvent(type="content_block_delta", data={
                "index": index, "delta": {"type": "text_delta", "text": content},
            }))
            opened.append(index)
            index += 1

        for position, call in enumerate(tool_calls):
            function = call.get("function") or {}
            events.append(StreamEvent(type="content_block_start", data={
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": call.get("id") or f"call_{position}",
                    "name": function.get("name") or "",
                    "input": {},
                },
            }))
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                events.append(StreamEvent(type="content_block_delta", data={
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                }))
            opened.append(index)
            index += 1

        for block_index in opened:
            events.append(StreamEvent(type="content_block_stop", data={"index": block_index}))
        stop_reason = _STOP_REASON_MAP.get(
            str(choice.get("finish_reason") or "stop"), "end_turn"
        )
        if tool_calls:
            # 兜底解析出的调用同样构成 tool_use 轮，finish_reason=stop 不作数
            stop_reason = "tool_use"
        usage = self._usage(data.get("usage") or {}) if isinstance(data.get("usage"), dict) else {}
        events.append(StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": stop_reason}, "usage": usage,
        }))
        return events

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
