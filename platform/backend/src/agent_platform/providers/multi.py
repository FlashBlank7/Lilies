"""Multi-Provider — route model calls to different backends based on model prefix.

Supports the Lilies design principle that LLMs are replaceable executors:
  deepseek/deepseek-v4-pro    → DeepSeekProvider (Anthropic-compatible API)
  openai/gpt-4o               → OpenAIProvider (OpenAI Chat Completions API)
  anthropic/claude-sonnet-4   → AnthropicProvider (Anthropic native Messages API)

Usage in config:
  model_turn block:  model="deepseek/deepseek-v4-pro"  or  model="openai/gpt-4o"
  Agent spec:        provider_profile.model = "openai/gpt-4o"

No external dependencies beyond what's already installed (httpx).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ModelProvider, ProviderCapabilities, ProviderError
from ..models import ChatMessage, StreamEvent, ToolDefinition


# ── OpenAI Provider ──────────────────────────────────────────────

class OpenAIProvider(ModelProvider):
    """OpenAI Chat Completions API provider (PLACEHOLDER).

    WARNING: Full format translation from the internal Anthropic-compatible
    message format to OpenAI's Chat Completions format is not yet implemented.
    This provider currently delegates to the DeepSeek-compatible path.
    Contributions welcome.

    Target endpoint: POST {base_url}/chat/completions
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 600.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=True, tools=True, parallel_tools=True,
            prompt_caching=False, images=True,
            max_context_tokens=128_000, max_output_tokens=16_384,
            input_price_per_1m=2.50, output_price_per_1m=10.00,
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
        raise ProviderError(
            "OpenAI provider is not yet implemented. "
            "Set DEEPSEEK_API_KEY to use the DeepSeek backend for now.",
        )


# ── Anthropic Provider ───────────────────────────────────────────

class AnthropicProvider(ModelProvider):
    """Anthropic native Messages API provider.

    Uses the standard Anthropic Messages endpoint with x-api-key auth.
    The message format is the same as what DeepSeek emulates, so this
    provider shares the same wire format.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 600.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=True, tools=True, parallel_tools=True,
            prompt_caching=True, images=True,
            max_context_tokens=200_000, max_output_tokens=8_192,
            input_price_per_1m=3.00, output_price_per_1m=15.00,
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
            raise ProviderError("ANTHROPIC_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [_message_payload(msg) for msg in messages],
            "stream": True,
        }
        if thinking_enabled:
            payload["thinking"] = {"type": "enabled"}
        if effort:
            payload["output_config"] = {"effort": effort}
        if tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in tools]
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if user_id:
            payload["metadata"] = {"user_id": _safe_user_id(user_id)}

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/v1/messages", headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:2000]
                        raise ProviderError(
                            f"Anthropic API returned {response.status_code}: {body}",
                            retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
                            status_code=response.status_code,
                        )
                    async for event in _parse_sse_stream(response):
                        yield event
        except httpx.TimeoutException as error:
            raise ProviderError("Anthropic request timed out", retryable=True) from error
        except httpx.NetworkError as error:
            raise ProviderError(f"Anthropic network error: {error}", retryable=True) from error


# ── Multi-Provider Router ──────────────────────────────────────

PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    "deepseek": "DeepSeekProvider",  # resolved at import time
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}

PROVIDER_CONFIGS: dict[str, dict[str, str]] = {
    "deepseek": {
        "env_key": "DEEPSEEK_API_KEY",
        "base_url_key": "DEEPSEEK_BASE_URL",
        "default_base": "https://api.deepseek.com/anthropic",
        "default_model": "deepseek-v4-pro",
    },
    "openai": {
        "env_key": "OPENAI_API_KEY",
        "base_url_key": "OPENAI_BASE_URL",
        "default_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "anthropic": {
        "env_key": "ANTHROPIC_API_KEY",
        "base_url_key": "ANTHROPIC_BASE_URL",
        "default_base": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-6",
    },
}


class MultiProvider(ModelProvider):
    """Route model calls to different backends by model name prefix.

    Model names use ``provider/model-id`` format:
      - ``deepseek/deepseek-v4-pro``
      - ``openai/gpt-4o``
      - ``anthropic/claude-sonnet-4-6``

    Falls back to the first configured provider if no prefix is present
    (backward compat with direct model names).
    """

    name = "multi"

    def __init__(
        self,
        deepseek_api_key: str | None = None,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        deepseek_base_url: str = "https://api.deepseek.com/anthropic",
        openai_base_url: str = "https://api.openai.com/v1",
        anthropic_base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 600.0,
    ) -> None:
        from .deepseek import DeepSeekProvider
        self._providers: dict[str, ModelProvider] = {}
        self._timeout = timeout_seconds

        if deepseek_api_key:
            self._providers["deepseek"] = DeepSeekProvider(
                deepseek_api_key, deepseek_base_url, timeout_seconds,
            )
        if openai_api_key:
            self._providers["openai"] = OpenAIProvider(
                openai_api_key, openai_base_url, timeout_seconds,
            )
        if anthropic_api_key:
            self._providers["anthropic"] = AnthropicProvider(
                anthropic_api_key, anthropic_base_url, timeout_seconds,
            )

    @property
    def configured_providers(self) -> list[str]:
        return sorted(self._providers)

    @property
    def configured_models(self) -> list[str]:
        models: list[str] = []
        for prefix, provider in self._providers.items():
            default = PROVIDER_CONFIGS.get(prefix, {}).get("default_model", "")
            if default:
                models.append(f"{prefix}/{default}")
        return models

    def _resolve(self, model: str) -> tuple[ModelProvider, str]:
        """Resolve ``provider/model`` to (provider_instance, bare_model)."""
        if "/" in model:
            prefix, bare = model.split("/", 1)
            provider = self._providers.get(prefix)
            if provider is not None:
                return provider, bare
        # Fallback: treat as the first configured provider
        default = next(iter(self._providers.values()), None)
        if default is None:
            first_env = PROVIDER_CONFIGS.get("deepseek", {}).get("env_key", "DEEPSEEK_API_KEY")
            raise ProviderError(
                f"No provider configured. Set {first_env} to enable model calls.",
                retryable=False,
            )
        return default, model

    def capabilities(self, model: str) -> ProviderCapabilities:
        provider, bare = self._resolve(model)
        return provider.capabilities(bare)

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
        provider, bare = self._resolve(model)
        async for event in provider.stream(
            model=bare,
            system=system,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            thinking_enabled=thinking_enabled,
            effort=effort,
            tool_choice=tool_choice,
            user_id=user_id,
        ):
            yield event


# ── Shared utilities ─────────────────────────────────────────────

def _message_payload(message: ChatMessage) -> dict[str, Any]:
    """Convert a ChatMessage to the Anthropic-compatible content block format."""
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text or ""})
        elif block.type == "thinking":
            entry: dict[str, Any] = {"type": "thinking", "thinking": block.thinking or ""}
            if block.signature:
                entry["signature"] = block.signature
            blocks.append(entry)
        elif block.type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input or {},
            })
        elif block.type == "tool_result":
            entry2: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content or "",
            }
            if block.is_error:
                entry2["is_error"] = True
            blocks.append(entry2)
    return {"role": message.role, "content": blocks}


def _safe_user_id(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_")[:512]


async def _parse_sse_stream(response: httpx.Response) -> AsyncIterator[StreamEvent]:
    """Parse Anthropic-compatible SSE event stream."""
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
                        raise ProviderError(f"invalid SSE payload: {error}") from error
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
