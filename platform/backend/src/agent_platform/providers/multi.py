"""Multi-Provider — route model calls to different backends based on model prefix.

Supports the Lilies design principle that LLMs are replaceable executors:
  deepseek/deepseek-v4-pro    → DeepSeekProvider (Anthropic-compatible API)
  openai/gpt-4o               → OpenAIProvider (OpenAI-compatible API)
  anthropic/claude-sonnet-4   → AnthropicProvider (Anthropic native API)
  local/Qwen/Qwen3.5-4B       → OpenAIChatProvider (原生 chat-completions，本地小模型)

Usage in config:
  model_turn block:  model="deepseek/deepseek-v4-pro"  or  model="openai/gpt-4o"
  Agent spec:        provider_profile.model = "openai/gpt-4o"

No external dependencies beyond what's already installed (httpx).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base import ModelProvider, ProviderCapabilities, ProviderError
from .deepseek import DeepSeekProvider
from .openai_chat import OpenAIChatProvider
from ..models import ChatMessage, StreamEvent, ToolDefinition


# ── OpenAI Provider (Anthropic-compatible endpoint) ─────────────

class OpenAIProvider(ModelProvider):
    """OpenAI via Anthropic-compatible Messages API (beta)."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 600.0,
        egress_enabled: bool | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._deepseek = DeepSeekProvider(
            api_key,
            f"{self._base_url}/anthropic",
            timeout_seconds,
            egress_enabled=egress_enabled,
        )

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=True, tools=True, parallel_tools=True,
            prompt_caching=False, images=True,
            max_context_tokens=128_000, max_output_tokens=16_384,
        )

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return self._deepseek.stream(**kwargs)


# ── Anthropic Provider (native API) ─────────────────────────────

class AnthropicProvider(ModelProvider):
    """Anthropic via native Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 600.0,
        egress_enabled: bool | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._deepseek = DeepSeekProvider(
            api_key,
            f"{self._base_url}/v1/messages",
            timeout_seconds,
            egress_enabled=egress_enabled,
        )

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=True, tools=True, parallel_tools=True,
            prompt_caching=True, images=True,
            max_context_tokens=200_000, max_output_tokens=8_192,
        )

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        return self._deepseek.stream(**kwargs)


# ── Multi-Provider Router ──────────────────────────────────────

PROVIDER_REGISTRY: dict[str, type[ModelProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}

PROVIDER_CONFIGS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url_key": "DEEPSEEK_BASE_URL",
        "default_base": "https://api.deepseek.com/anthropic",
        "default_model": "deepseek-v4-pro",
    },
    "openai": {
        "base_url_key": "OPENAI_BASE_URL",
        "default_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "anthropic": {
        "base_url_key": "ANTHROPIC_BASE_URL",
        "default_base": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-6",
    },
    "local": {
        "base_url_key": "LOCAL_MODEL_BASE_URL",
        "default_base": "http://127.0.0.1:8001/v1",
        "default_model": "Qwen/Qwen3.5-4B",
    },
}


class MultiProvider(ModelProvider):
    """Route model calls to different backends by model name prefix.

    Model names use ``provider/model-id`` format:
      - ``deepseek/deepseek-v4-pro``
      - ``openai/gpt-4o``
      - ``anthropic/claude-sonnet-4-6``

    Falls back to ``deepseek/`` if no prefix is present (backward compat).
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
        local_base_url: str | None = None,
        local_api_key: str | None = None,
        timeout_seconds: float = 600.0,
        egress_enabled: bool | None = None,
    ) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._timeout = timeout_seconds
        self._egress_enabled = egress_enabled

        if deepseek_api_key:
            self._providers["deepseek"] = DeepSeekProvider(
                deepseek_api_key,
                deepseek_base_url,
                timeout_seconds,
                egress_enabled=self._egress_enabled,
            )
        if openai_api_key:
            self._providers["openai"] = OpenAIProvider(
                openai_api_key,
                openai_base_url,
                timeout_seconds,
                egress_enabled=self._egress_enabled,
            )
        if anthropic_api_key:
            self._providers["anthropic"] = AnthropicProvider(
                anthropic_api_key,
                anthropic_base_url,
                timeout_seconds,
                egress_enabled=self._egress_enabled,
            )
        if local_base_url:
            # 本地小模型端点（vLLM/SGLang/Ollama）：原生 chat-completions 协议，
            # 回环地址豁免 egress 开关（本地推理不是计费出口）。
            self._providers["local"] = OpenAIChatProvider(
                local_api_key,
                local_base_url,
                timeout_seconds,
                egress_enabled=self._egress_enabled,
            )
        if not self._providers:
            # Graceful degradation — health endpoint still works, API calls fail
            pass

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
        # Fallback: treat as DeepSeek model (backward compat)
        default = self._providers.get("deepseek") or next(iter(self._providers.values()), None)
        if default is None:
            raise ProviderError(
                "No provider configured. Set DEEPSEEK_API_KEY to enable model calls.",
                retryable=False,
            )
        return default, model

    def capabilities(self, model: str) -> ProviderCapabilities:
        provider, bare = self._resolve(model)
        return provider.capabilities(bare)

    def provider_name_for(self, model: str) -> str:
        provider, _ = self._resolve(model)
        return provider.name

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
