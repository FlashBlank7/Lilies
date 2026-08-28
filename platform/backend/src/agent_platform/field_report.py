"""现场改进信息回流 — 离线扫描生产数据目录，产出一份脱敏的"现场报告"。

商业化前提（2026-08 讨论）：线上跑冻结版、本地做开发，改进循环靠数据回流驱动。
本模块是回流机制的 v1：一个只读脚本，不动线上进程（SQLite WAL 下并发读安全），
从 builds / events / build_transcripts 提取"有助于改进的信号"：

- 构建结局分布、回合数、修复循环、截断/停滞/超时事件
- 工具级调用量与边界拒绝率（schema/revision 硬门接住了多少错误——实验主指标）
- 每轮实际使用的模型（per-actor 血缘）、stop_reason 分布
- 失败构建的错误摘要（截断脱敏）

脱敏默认值（商业红线）：不导出需求原文（只留长度+哈希）、不导出工具调用的
参数与结果（只计数分类）、错误信息截断。--include-requirement-text 显式开启原文。

用法：
    python -m agent_platform.field_report --data-dir data --out field-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 对改进最有信息量的事件类型：失败形态、守卫触发、交付里程碑。
SIGNAL_EVENT_TYPES = (
    "build.progress.stalled",
    "build.progress.exploration_exhausted",
    "build.turn.truncated",
    "build.deadline.exceeded",
    "build.needs_attention",
    "build.published",
    "build.completed",
    "team.teammate.blocked",
    "team.teammate.stopped",
    "tests.completed",
)

ERROR_SNIPPET_LIMIT = 200
TOP_ERROR_SNIPPETS = 3


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    # 只读连接也要 busy_timeout：WAL 检查点期间读同样会撞锁
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def _team_state_extract(raw: str) -> dict[str, Any]:
    try:
        state = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    teammates = state.get("teammates") or {}
    return {
        "revision": state.get("revision"),
        "repair_cycles": state.get("repair_cycles"),
        "published_version": state.get("published_version"),
        "planning_mode": state.get("planning_mode"),
        "coordinator_model": state.get("coordinator_model"),
        "teammate_models": state.get("teammate_models"),
        "teammates": {
            name: {"model": item.get("model"), "status": item.get("status")}
            for name, item in teammates.items()
            if isinstance(item, dict)
        },
        "task_count": len(state.get("tasks") or []),
        "has_pending_question": bool(state.get("pending_question")),
    }


def _transcript_stats(path: Path) -> dict[str, Any]:
    """逐轮记录 → 轮次/工具/模型/停止原因统计。只计数分类，不携带参数与结果。"""

    turns = 0
    truncated_turns = 0
    models = Counter()
    actors = Counter()
    stop_reasons = Counter()
    tool_calls = Counter()
    tool_errors = Counter()
    error_snippets: dict[str, Counter] = {}
    if not path.is_file():
        return {"available": False}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("turn"):
            turns += 1
            actors[str(record.get("actor") or "?")] += 1
            if record.get("model"):
                models[str(record["model"])] += 1
            if record.get("stop_reason"):
                stop_reasons[str(record["stop_reason"])] += 1
            if record.get("stop_reason") in ("max_tokens", "length"):
                truncated_turns += 1
        for call in record.get("tool_calls") or []:
            tool = str(call.get("tool") or "?")
            tool_calls[tool] += 1
            if call.get("is_error"):
                tool_errors[tool] += 1
                snippet = str(call.get("result") or "")[:ERROR_SNIPPET_LIMIT]
                error_snippets.setdefault(tool, Counter())[snippet] += 1
    return {
        "available": True,
        "turns": turns,
        "truncated_turns": truncated_turns,
        "actors": dict(actors),
        "models": dict(models),
        "stop_reasons": dict(stop_reasons),
        "tool_calls": dict(tool_calls),
        "tool_errors": dict(tool_errors),
        "top_error_snippets": {
            tool: [text for text, _ in counter.most_common(TOP_ERROR_SNIPPETS)]
            for tool, counter in error_snippets.items()
        },
    }


def _signal_events(conn: sqlite3.Connection, build_id: str) -> dict[str, int]:
    placeholders = ",".join("?" for _ in SIGNAL_EVENT_TYPES)
    rows = conn.execute(
        f"SELECT event_type, COUNT(*) AS n FROM events "
        f"WHERE stream_id=? AND event_type IN ({placeholders}) GROUP BY event_type",
        (build_id, *SIGNAL_EVENT_TYPES),
    ).fetchall()
    return {row["event_type"]: row["n"] for row in rows}


def generate_report(
    data_dir: Path,
    *,
    days: float | None = None,
    include_requirement_text: bool = False,
) -> dict[str, Any]:
    db_path = data_dir / "agent_platform.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"platform database not found: {db_path}")
    transcripts_dir = data_dir / "build_transcripts"

    conn = _connect_readonly(db_path)
    try:
        query = "SELECT * FROM builds"
        params: tuple[Any, ...] = ()
        if days is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - days * 86_400
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            query += " WHERE updated_at >= ?"
            params = (cutoff_iso,)
        query += " ORDER BY updated_at DESC"
        build_rows = [dict(row) for row in conn.execute(query, params).fetchall()]

        builds: list[dict[str, Any]] = []
        status_counts = Counter()
        total_tool_calls = Counter()
        total_tool_errors = Counter()
        model_turns = Counter()
        signal_totals = Counter()
        total_turns = 0
        total_truncated = 0
        repair_cycle_values: list[int] = []

        for row in build_rows:
            build_id = str(row["id"])
            status = str(row["status"])
            status_counts[status] += 1
            requirement = str(row.get("requirement") or "")
            team = _team_state_extract(row.get("team_state_json") or "")
            if isinstance(team.get("repair_cycles"), int):
                repair_cycle_values.append(team["repair_cycles"])
            transcript = _transcript_stats(transcripts_dir / f"{build_id}.jsonl")
            if transcript.get("available"):
                total_turns += transcript["turns"]
                total_truncated += transcript["truncated_turns"]
                total_tool_calls.update(transcript["tool_calls"])
                total_tool_errors.update(transcript["tool_errors"])
                model_turns.update(transcript["models"])
            signals = _signal_events(conn, build_id)
            signal_totals.update(signals)

            entry: dict[str, Any] = {
                "build_id": build_id,
                "application_id_hash": _hash(str(row.get("application_id") or "")),
                "status": status,
                # 老库没有 builder 列：回落 classic，与运行时的注册表回落语义一致
                "builder": str(row.get("builder") or "classic"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "limits": {
                    "max_turns": row.get("max_turns"),
                    "max_repair_cycles": row.get("max_repair_cycles"),
                    "max_elapsed_seconds": row.get("max_elapsed_seconds"),
                    "auto_publish": bool(row.get("auto_publish")),
                },
                "requirement_len": len(requirement),
                "requirement_hash": _hash(requirement),
                "error": (str(row.get("error"))[:300] if row.get("error") else None),
                "team_state": team,
                "transcript": transcript,
                "signal_events": signals,
            }
            if include_requirement_text:
                entry["requirement_text"] = requirement
            builds.append(entry)

        tool_stats = {
            tool: {
                "calls": calls,
                "errors": total_tool_errors.get(tool, 0),
                "error_rate": round(total_tool_errors.get(tool, 0) / calls, 4),
            }
            for tool, calls in total_tool_calls.most_common()
        }
        boundary_total = sum(total_tool_calls.values())
        boundary_errors = sum(total_tool_errors.values())

        return {
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "data_dir": str(data_dir),
                "days_filter": days,
                "requirement_text_included": include_requirement_text,
                "build_count": len(builds),
            },
            "summary": {
                "builds_by_status": dict(status_counts),
                "total_turns": total_turns,
                "truncated_turns": total_truncated,
                "total_tool_calls": boundary_total,
                "total_tool_errors": boundary_errors,
                # 边界拒绝率：硬门接住的错误占比——"他律"实际工作量的直接度量
                "boundary_rejection_rate": (
                    round(boundary_errors / boundary_total, 4) if boundary_total else 0.0
                ),
                "repair_cycles": {
                    "max": max(repair_cycle_values, default=0),
                    "total": sum(repair_cycle_values),
                },
                "model_turns": dict(model_turns.most_common()),
                "signal_events": dict(signal_totals),
            },
            "tool_stats": tool_stats,
            "builds": builds,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="导出现场改进信息回流报告（只读、脱敏）")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=None, help="输出 JSON 路径（默认按日期命名）")
    parser.add_argument("--days", type=float, default=None, help="只包含最近 N 天更新的构建")
    parser.add_argument(
        "--include-requirement-text",
        action="store_true",
        help="包含需求原文（默认只留长度+哈希；客户数据回流前务必确认脱敏责任）",
    )
    args = parser.parse_args()

    report = generate_report(
        args.data_dir,
        days=args.days,
        include_requirement_text=args.include_requirement_text,
    )
    out = args.out or Path(f"field-report-{datetime.now(timezone.utc).date().isoformat()}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    print(f"现场报告已写入 {out}")
    print(f"构建 {report['meta']['build_count']} 个：{summary['builds_by_status']}")
    print(
        f"轮次 {summary['total_turns']}（截断 {summary['truncated_turns']}）· "
        f"工具调用 {summary['total_tool_calls']}（边界拒绝率 {summary['boundary_rejection_rate']:.1%}）"
    )
    if summary["signal_events"]:
        print(f"守卫/里程碑事件：{summary['signal_events']}")
    worst = [
        (tool, stats) for tool, stats in report["tool_stats"].items()
        if stats["errors"] and stats["calls"] >= 5
    ]
    worst.sort(key=lambda item: item[1]["error_rate"], reverse=True)
    if worst:
        print("错误率最高的工具（≥5 次调用）：")
        for tool, stats in worst[:5]:
            print(f"  {tool}: {stats['errors']}/{stats['calls']} = {stats['error_rate']:.1%}")


if __name__ == "__main__":
    main()
