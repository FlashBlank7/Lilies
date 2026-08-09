"""Test infrastructure — MockProvider, scripted responses, and test helpers.

Provides a MockProvider that replays pre-recorded tool-use sequences from
JSON files, enabling deterministic Builder tests without real model calls.

Usage in tests::

    from agent_platform.testing import MockProvider, scripted_tool_calls

    provider = MockProvider.from_script([
        ("draft_add_node", {"node": {...}}),
        ("draft_connect", {"edge": {...}}),
        ("test_run", {}),
        ("draft_publish", {}),
    ])

    # Or load from JSON
    provider = MockProvider.from_json("tests/fixtures/greeting_workflow.json")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from .models import ChatMessage, StreamEvent, ToolDefinition
from .providers.base import ModelProvider, ProviderCapabilities


class MockProvider(ModelProvider):
    """Replay a fixed sequence of tool calls for deterministic tests.

    Each call to ``stream()`` advances a cursor and replays one turn from the
    script. A script is a list of lists, where each inner list is one turn's
    tool calls. The final turn should contain no tool calls to trigger stop.

    Call ``reset()`` to rewind the cursor for test reuse.
    """

    name = "mock"

    def __init__(self) -> None:
        self._script: list[list[tuple[str, dict[str, Any]]]] = []
        self._cursor = 0
        self.calls = 0
        self.stream_calls: list[dict[str, Any]] = []  # record of each stream() call

    # ── factory methods ─────────────────────────────────────────────

    @classmethod
    def from_script(
        cls,
        turns: list[list[tuple[str, dict[str, Any]]]],
    ) -> "MockProvider":
        """Build a provider from an explicit multi-turn script.

        Each turn is a list of (tool_name, tool_input) tuples.
        The last turn should be empty (no tools) to trigger stop.

        Example::

            provider = MockProvider.from_script([
                [("draft_add_node", {"node": {...}}), ("draft_connect", {"edge": {...}})],
                [("test_run", {})],
                [],  # final turn: stop
            ])
        """
        instance = cls()
        instance._script = turns
        return instance

    @classmethod
    def from_json(cls, path: str | Path) -> "MockProvider":
        """Load a script from a JSON file.

        File format::

            {
              "turns": [
                [
                  {"tool": "draft_add_node", "input": {"node": {...}}},
                  {"tool": "draft_connect", "input": {"edge": {...}}}
                ],
                [{"tool": "test_run", "input": {}}],
                []
              ]
            }
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        turns: list[list[tuple[str, dict[str, Any]]]] = []
        for turn in data.get("turns", data if isinstance(data, list) else []):
            if isinstance(turn, list):
                turns.append([
                    (item["tool"], item.get("input", {}))
                    if isinstance(item, dict) else (item[0], item[1] if len(item) > 1 else {})
                    for item in turn
                ])
                # Allow trailing empty turn shortcut
                if not turns[-1]:
                    pass  # empty turn = stop
        instance = cls()
        instance._script = turns
        return instance

    @classmethod
    def from_single_flat_sequence(
        cls,
        operations: list[tuple[str, dict[str, Any]]],
    ) -> "MockProvider":
        """Build from a flat list where each operation is its own turn.

        The last turn is always "stop" (no tools).

        Example::

            provider = MockProvider.from_single_flat_sequence([
                ("draft_add_node", {"node": {...}}),
                ("draft_connect", {"edge": {...}}),
                ("test_run", {}),
            ])
            # → 4 turns: 3 tool calls + 1 stop
        """
        turns = [[op] for op in operations]
        turns.append([])  # final stop turn
        return cls.from_script(turns)

    # ── lifecycle ──────────────────────────────────────────────────

    def reset(self) -> None:
        """Rewind the cursor to the beginning."""
        self._cursor = 0
        self.calls = 0
        self.stream_calls.clear()

    def _current_turn(self) -> list[tuple[str, dict[str, Any]]]:
        if self._cursor < len(self._script):
            return self._script[self._cursor]
        return []

    # ── ModelProvider interface ─────────────────────────────────────

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            thinking=True,
            tools=True,
            parallel_tools=True,
            prompt_caching=False,
            images=False,
            max_context_tokens=128_000,
            max_output_tokens=16_384,
            input_price_per_1m=0.5,
            output_price_per_1m=1.0,
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
        """Replay one turn from the script."""
        self.calls += 1
        self.stream_calls.append({
            "call": self.calls,
            "cursor": self._cursor,
            "model": model,
            "message_count": len(messages),
            "tool_count": len(tools),
        })

        turn = self._current_turn()
        self._cursor += 1

        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 100}}},
        )

        if not turn:
            # Final turn: return text, no tool calls → agent loop stops
            yield StreamEvent(
                type="content_block_start",
                data={"index": 0, "content_block": {"type": "text", "text": ""}},
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Build completed successfully."},
                },
            )
            yield StreamEvent(
                type="message_delta",
                data={
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                },
            )
            return

        # Emit one content_block_start + delta + stop per tool call
        for i, (tool_name, tool_input) in enumerate(turn):
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"call-{self.calls}-{i}",
                        "name": tool_name,
                        "input": {},
                    },
                },
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(tool_input, ensure_ascii=False),
                    },
                },
            )
            yield StreamEvent(
                type="content_block_stop",
                data={"index": i},
            )

        yield StreamEvent(
            type="message_delta",
            data={
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 50},
            },
        )


def scripted_tool_calls(
    *operations: tuple[str, dict[str, Any]],
) -> MockProvider:
    """Shorthand: create a MockProvider where each call is one turn.

    Usage::

        provider = scripted_tool_calls(
            ("draft_add_node", {"node": {...}}),
            ("draft_connect", {"edge": {...}}),
            ("test_run", {}),
            ("draft_publish", {}),
        )
    """
    return MockProvider.from_single_flat_sequence(list(operations))
