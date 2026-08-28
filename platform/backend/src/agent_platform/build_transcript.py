"""Builder transcript — the full model conversation behind a build.

Event streams answer "what did the Builder do". They cannot answer "why did it
decide that", because they only carry tool names and truncated results. When a
build stalls, the reasoning, the exact tool arguments, and the exact tool
errors are the evidence you need, and they used to exist only in memory.

Each turn is appended as one JSON line under
``<data_dir>/build_transcripts/<build_id>.jsonl`` so a running build can be
followed live and a finished build stays inspectable.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_TOOL_RESULT_CHARS = 200_000
_SECRET_KEY_HINTS = ("secret", "token", "password", "api_key", "apikey", "credential")


def _is_metering_key(key: str) -> bool:
    """计量字段豁免：input_tokens/output_tokens 等是审计数据不是凭证。

    误伤它们会把 usage 全打成 ***，诊断被迫绕道数据库（已知缺陷 #6）。
    """

    return key.casefold().replace("-", "_").endswith("_tokens")
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:sk|api[_-]?key|bearer)[-_ ]?[A-Za-z0-9]{16,}",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> Any:
    """Drop credential-shaped keys and values without touching reasoning text."""

    if isinstance(value, dict):
        return {
            key: (
                "***"
                if not _is_metering_key(str(key))
                and any(hint in str(key).casefold() for hint in _SECRET_KEY_HINTS)
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


class BuildTranscriptStore:
    """Append-only per-build transcript files."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, build_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", build_id)[:100] or "build"
        return self.root / f"{safe}.jsonl"

    def append(self, build_id: str, record: dict[str, Any]) -> None:
        """Write one transcript record. Never raises into the build loop."""

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"recorded_at": _utc_now(), **record},
                ensure_ascii=False,
                default=str,
            )
            path = self._path(build_id)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
        except Exception as error:  # pragma: no cover - diagnostics must not break builds
            print(f"[build_transcript] {build_id}: {type(error).__name__}: {error}")

    def read(
        self,
        build_id: str,
        *,
        after_turn: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        path = self._path(build_id)
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(record.get("turn") or 0) <= after_turn:
                    continue
                records.append(record)
        return records[:limit]

    def summary(self, build_id: str) -> dict[str, Any]:
        records = self.read(build_id, limit=10_000)
        turns = [item for item in records if item.get("kind") == "turn"]
        tool_calls = sum(len(item.get("tool_calls") or []) for item in turns)
        failed = sum(
            1
            for item in turns
            for call in (item.get("tool_calls") or [])
            if call.get("is_error")
        )
        return {
            "build_id": build_id,
            "available": bool(records),
            "turn_count": len(turns),
            "tool_call_count": tool_calls,
            "failed_tool_call_count": failed,
            "actors": sorted({str(item.get("actor") or "") for item in turns} - {""}),
            "last_stop_reason": turns[-1].get("stop_reason") if turns else None,
        }


def turn_record(
    *,
    turn: int,
    actor: str,
    model: str,
    blocks: list[Any],
    tool_calls: list[dict[str, Any]],
    stop_reason: str | None,
    usage: Any,
    draft_revision: int,
    prompt: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one transcript record from a completed model turn.

    prompt：这一轮实际下发给模型的 system / user 原文。默认不存（体量大且含
    目录全文），置 LILIES_TRACE_PROMPTS=1 时由调用方传入——追踪"平台到底给
    模型看了什么"是复盘执行链的另一半，没有它只能看到模型做了什么、看不到
    它是被怎么问的。
    """

    thinking: list[str] = []
    text: list[str] = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "thinking" and getattr(block, "thinking", None):
            thinking.append(str(block.thinking))
        elif kind == "text" and getattr(block, "text", None):
            text.append(str(block.text))
    return {
        "kind": "turn",
        "turn": turn,
        "actor": actor,
        "model": model,
        "thinking": redact("\n".join(thinking)) if thinking else "",
        "text": redact("\n".join(text)) if text else "",
        "prompt": {k: redact(v)[:20_000] for k, v in (prompt or {}).items()} or None,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": usage.model_dump(mode="json") if hasattr(usage, "model_dump") else {},
        "draft_revision": draft_revision,
    }


def owner_record(*, text: str, draft_revision: int) -> dict[str, Any]:
    """Record one owner (human) message so the transcript shows both sides.

    ``turn`` is fixed at 1 so the default ``after_turn=0`` read filter keeps it;
    chronology comes from append order, and renderers key on ``kind``/``actor``.
    """

    return {
        "kind": "owner",
        "turn": 1,
        "actor": "owner",
        "model": "",
        "thinking": "",
        "text": redact(text),
        "tool_calls": [],
        "stop_reason": None,
        "usage": {},
        "draft_revision": draft_revision,
    }


def event_record(*, text: str, event: str, draft_revision: int = 0,
                 for_owner: bool = False) -> dict[str, Any]:
    """Record one platform milestone (发布/等待回复/取消) as a first-class stream item.

    Turn text can be missed or skimmed; milestones must be unmissable. Renderers
    key on ``kind == "event"`` and show these as centered system badges.

    ``for_owner`` 决定这条给不给业主看，**默认不给**。
    原因是量出来的：真机 76 份 transcript 里 kind=event 共 465 条，
    其中 368 条是 event="phase"（构建阶段的内部诊断），
    85 条带着模型名或成串英文——「架构规划：local2/Qwen/Qwen3-32B 选型」
    这种直接摆在业主面前。闸口把 kind=event 整类当白名单放行，
    所以这 85 条一条没挡。

    默认拒绝的方向是故意的：将来新加写入点忘了标注，
    业主少看一行内部日志，而不是多看一行模型名。
    """

    return {
        "kind": "event",
        "event": event,
        "for_owner": for_owner,
        "turn": 1,
        "actor": "platform",
        "model": "",
        "thinking": "",
        "text": redact(text),
        "tool_calls": [],
        "stop_reason": None,
        "usage": {},
        "draft_revision": draft_revision,
    }


def tool_call_record(
    *,
    name: str,
    arguments: dict[str, Any],
    result: str,
    is_error: bool,
) -> dict[str, Any]:
    trimmed = result[:MAX_TOOL_RESULT_CHARS]
    return {
        "tool": name,
        "arguments": redact(arguments),
        "result": redact(trimmed),
        "truncated": len(result) > MAX_TOOL_RESULT_CHARS,
        "is_error": is_error,
    }
