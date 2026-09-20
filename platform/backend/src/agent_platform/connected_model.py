"""Raw LLM API inference; agent orchestration belongs to the platform."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

from .models import StreamEvent
from .providers.base import ModelProvider, ProviderCapabilities, ProviderError
from .providers.deepseek import DeepSeekProvider
from .providers.openai_chat import OpenAIChatProvider, _is_loopback


async def validate_kimi_cli(executable: str):
    process = await asyncio.create_subprocess_exec(executable, '--help', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or not all(flag in out.decode(errors='replace') for flag in ('--print', '--thinking', '--agent-file', '--mcp-config')):
        raise ValueError('当前接入支持 Python kimi-cli 的 print 接口；此版本接口不同，请安装 kimi-cli 或使用 Kimi 的 API Key 接入')


def completion_events(blocks, usage=None, stop_reason="end_turn"):
    yield StreamEvent(type="message_start", data={"message": {"usage": usage or {}}})
    for index, block in enumerate(blocks):
        yield StreamEvent(type="content_block_start", data={"index": index, "content_block": block})
        yield StreamEvent(type="content_block_stop", data={"index": index})
    yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": stop_reason}, "usage": usage or {}})


class ConnectedModel(ModelProvider):
    def __init__(self, connection, runtime_dir: Path, *, transport=None, supports_images=False, egress_enabled=None):
        self.connection, self.runtime_dir, self.transport = connection, runtime_dir, transport
        self.supports_images = supports_images
        self.name = connection.provider
        if egress_enabled is None:
            configured = os.getenv('LILIES_MODEL_EGRESS_ENABLED', os.getenv('MODEL_EGRESS_ENABLED', 'false'))
            egress_enabled = configured.strip().lower() in {'1', 'true', 'yes', 'on'}
        self.egress_enabled = bool(egress_enabled)

    def capabilities(self, model):
        return ProviderCapabilities(thinking=True, tools=True, parallel_tools=False,
            prompt_caching=False, images=self.supports_images, max_context_tokens=128_000, max_output_tokens=16_384)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None, **kwargs):
        if self.connection.provider != "api":
            raise ProviderError("模型积木仅支持原始 LLM API，不能调用 Codex、Claude Code、Kimi 等智能体会话")
        async for event in self.api_stream(system, messages, tools, max_output_tokens, tool_choice):
            yield event

    async def api_stream(self, system, messages, tools, max_tokens, tool_choice):
        c = self.connection
        if not self.egress_enabled and not _is_loopback(c.base_url):
            raise ProviderError('模型出口已关闭；仅在获准的真实调用中启用 MODEL_EGRESS_ENABLED')
        key = c.api_key.get_secret_value() if c.api_key else ""
        if not key:
            raise ProviderError("请填写项目 API Key")
        headers = {"content-type": "application/json"}
        base = c.base_url.rstrip("/")
        if c.protocol == "openai":
            headers["authorization"] = "Bearer " + key
            payload = {"model": c.model, "messages": OpenAIChatProvider._chat_messages(system, messages), "stream": False}
            reasoning_model = c.model.startswith(("gpt-5", "o1", "o3", "o4"))
            payload["max_completion_tokens" if reasoning_model else "max_tokens"] = max_tokens
            if tools:
                payload["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools]
                payload["tool_choice"] = OpenAIChatProvider._tool_choice(tool_choice)
            if c.thinking != "default":
                payload["reasoning_effort"] = "none" if c.thinking == "off" else c.thinking
            url = base + "/chat/completions"
        else:
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            payload = {"model": c.model, "max_tokens": max_tokens, "system": system,
                       "messages": [DeepSeekProvider._message_payload(m) for m in messages]}
            if tools:
                payload["tools"] = [t.model_dump(mode="json") for t in tools]
            if tool_choice and tool_choice.get("type") != "none":
                payload["tool_choice"] = tool_choice
            if c.thinking == "off":
                payload["thinking"] = {"type": "disabled"}
            elif c.thinking != "default":
                if c.model.startswith("deepseek"):
                    payload["thinking"] = {"type": "enabled"}
                else:
                    payload["thinking"] = {"type": "adaptive"}
                if c.thinking != "enabled":
                    payload["output_config"] = {"effort": c.thinking}
            url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30), transport=self.transport) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.is_error:
                    # Never persist provider bodies: some compatible services echo request credentials.
                    message = {
                        402: "模型服务账户额度不足或计费不可用（HTTP 402），请检查模型账户",
                        429: "模型服务暂时限流（HTTP 429），请降低并发后重试",
                    }.get(response.status_code, f"模型 API 返回 HTTP {response.status_code}，请检查地址、模型、密钥和思考选项")
                    raise ProviderError(message,
                        status_code=response.status_code, retryable=response.status_code in {408, 429, 502, 503})
                data = response.json()
        except httpx.HTTPError as error:
            raise ProviderError("模型 API 连接失败或超时，请检查服务地址", retryable=True) from error
        except ValueError as error:
            raise ProviderError("模型 API 未返回有效 JSON") from error
        if c.protocol == "openai":
            if not data.get("choices"):
                raise ProviderError("模型 API 响应中没有 choices")
            adapter = OpenAIChatProvider(None, base)
            for event in adapter._translate_completion(data):
                yield event
        else:
            if not isinstance(data.get("content"), list):
                raise ProviderError("模型 API 响应中没有 content")
            for event in completion_events(data["content"], data.get("usage"), data.get("stop_reason", "end_turn")):
                yield event
