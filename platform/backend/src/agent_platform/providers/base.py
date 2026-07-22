from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..models import ChatMessage, StreamEvent, ToolDefinition


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    thinking: bool
    tools: bool
    parallel_tools: bool
    prompt_caching: bool
    images: bool
    max_context_tokens: int
    max_output_tokens: int
    input_price_per_1m: float = 0.5
    output_price_per_1m: float = 1.0


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ModelProvider(ABC):
    name: str

    def provider_name_for(self, model: str) -> str:
        return self.name

    @abstractmethod
    def capabilities(self, model: str) -> ProviderCapabilities: ...

    @abstractmethod
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
    ) -> AsyncIterator[StreamEvent]: ...
