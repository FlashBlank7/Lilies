from __future__ import annotations

from pathlib import Path

from agent_platform.build_transcript import (
    BuildTranscriptStore,
    redact,
    tool_call_record,
    turn_record,
)


class _Block:
    def __init__(self, type: str, **fields: object) -> None:
        self.type = type
        self.text = fields.get("text")
        self.thinking = fields.get("thinking")


class _Usage:
    @staticmethod
    def model_dump(mode: str = "json") -> dict[str, int]:
        del mode
        return {"input_tokens": 12, "output_tokens": 7}


def test_transcript_round_trips_reasoning_tool_arguments_and_errors(tmp_path: Path) -> None:
    store = BuildTranscriptStore(tmp_path / "build_transcripts")
    store.append(
        "build-1",
        turn_record(
            turn=1,
            actor="coordinator",
            model="test-model",
            blocks=[
                _Block("thinking", thinking="The start node has no inputs yet."),
                _Block("text", text="Adding the start node."),
            ],
            tool_calls=[
                tool_call_record(
                    name="draft_add_node",
                    arguments={"node": {"id": "start", "type": "start"}},
                    result='{"revision": 1}',
                    is_error=False,
                ),
                tool_call_record(
                    name="draft_connect",
                    arguments={"edge": {"source": "start", "target": "ghost"}},
                    result="KeyError: node not found: ghost",
                    is_error=True,
                ),
            ],
            stop_reason="tool_use",
            usage=_Usage(),
            draft_revision=1,
        ),
    )

    records = store.read("build-1")
    assert len(records) == 1
    record = records[0]
    # Reasoning survives: this is the thing event streams could never show.
    assert record["thinking"] == "The start node has no inputs yet."
    assert record["text"] == "Adding the start node."
    assert record["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert record["stop_reason"] == "tool_use"

    # Exact tool arguments and the exact failure survive too.
    failed = record["tool_calls"][1]
    assert failed["tool"] == "draft_connect"
    assert failed["arguments"]["edge"]["target"] == "ghost"
    assert failed["result"] == "KeyError: node not found: ghost"
    assert failed["is_error"] is True

    summary = store.summary("build-1")
    assert summary["available"] is True
    assert summary["turn_count"] == 1
    assert summary["tool_call_count"] == 2
    assert summary["failed_tool_call_count"] == 1
    assert summary["actors"] == ["coordinator"]


def test_transcript_supports_incremental_follow(tmp_path: Path) -> None:
    store = BuildTranscriptStore(tmp_path / "build_transcripts")
    for turn in (1, 2, 3):
        store.append(
            "build-2",
            turn_record(
                turn=turn,
                actor="coordinator",
                model="test-model",
                blocks=[_Block("text", text=f"turn {turn}")],
                tool_calls=[],
                stop_reason="end_turn",
                usage=_Usage(),
                draft_revision=turn,
            ),
        )

    assert [item["turn"] for item in store.read("build-2")] == [1, 2, 3]
    assert [item["turn"] for item in store.read("build-2", after_turn=2)] == [3]


def test_transcript_redacts_credentials_without_touching_reasoning() -> None:
    payload = redact(
        {
            "api_key": "live-value",
            "note": "call with sk-ABCDEFGHIJKLMNOPQRST now",
            "reasoning": "The workflow needs an approval gate.",
        }
    )
    assert payload["api_key"] == "***"
    assert "sk-ABCDEFGHIJKLMNOPQRST" not in payload["note"]
    assert payload["reasoning"] == "The workflow needs an approval gate."


def test_missing_transcript_reports_unavailable_instead_of_failing(tmp_path: Path) -> None:
    store = BuildTranscriptStore(tmp_path / "build_transcripts")
    assert store.read("never-ran") == []
    assert store.summary("never-ran")["available"] is False


def test_metering_tokens_survive_redaction() -> None:
    """计量字段（*_tokens）是审计数据不是凭证——不许被脱敏成 ***（缺陷 #6）。"""

    from agent_platform.build_transcript import redact

    payload = {
        "usage": {"input_tokens": 12345, "output_tokens": 67, "cache_read_input_tokens": 999},
        "api_token": "sk-real-secret",
        "Authorization": "Bearer xxx",
    }
    result = redact(payload)
    assert result["usage"] == {"input_tokens": 12345, "output_tokens": 67, "cache_read_input_tokens": 999}
    assert result["api_token"] == "***"
