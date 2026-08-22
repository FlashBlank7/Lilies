"""现场回流报告：只读扫描、脱敏默认值、信号事件与工具错误率统计。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_platform.field_report import generate_report
from agent_platform.storage import Storage
from agent_platform.workflow_models import ApplicationCreateRequest
from agent_platform.workflow_storage import WorkflowStorage


def _seed(data_dir: Path) -> None:
    storage = Storage(data_dir)
    store = WorkflowStorage(storage)

    async def seed() -> None:
        await storage.initialize()
        await store.initialize()
        app_ok = await store.create_application(
            ApplicationCreateRequest(name="报表应用", requirement="每日销售报表")
        )
        app_bad = await store.create_application(
            ApplicationCreateRequest(name="对账应用", requirement="对账")
        )
        await store.create_build(
            "build-ok", app_ok["id"], "给珠宝店做一个每日销售报表工作流，输出各门店合计金额。",
            True, 36, 4, 480.0, "auto",
            builder="classic", coordinator_model="deepseek/deepseek-v4-pro",
            teammate_models=["local/tiny-4b"],
        )
        await store.update_build("build-ok", status="published")
        await store.create_build(
            "build-bad", app_bad["id"], "对账工作流。", False, 12, 2, 240.0, "auto",
        )
        await store.update_build(
            "build-bad", status="needs_attention",
            error="builder progress stalled: no durable draft progress for 6 consecutive turns",
        )
        await storage.append_event("build-ok", "build.published", {"version": 1})
        await storage.append_event("build-bad", "build.progress.stalled", {"turn": 9})
        await storage.append_event("build-bad", "build.needs_attention", {})

    asyncio.run(seed())

    transcripts = data_dir / "build_transcripts"
    transcripts.mkdir(exist_ok=True)
    records = [
        {"kind": "turn", "turn": 1, "actor": "coordinator", "model": "deepseek/deepseek-v4-pro",
         "stop_reason": "tool_use", "tool_calls": [
             {"tool": "draft_add_node", "arguments": {"node": {"id": "n1"}}, "result": "ok", "is_error": False},
             {"tool": "draft_add_node", "arguments": {"node": {}}, "result": "invalid node: id required", "is_error": True},
         ]},
        {"kind": "turn", "turn": 2, "actor": "schema-hand", "model": "local/tiny-4b",
         "stop_reason": "max_tokens", "tool_calls": []},
    ]
    (transcripts / "build-ok.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8"
    )


def test_report_aggregates_signals_and_redacts_by_default(tmp_path: Path) -> None:
    _seed(tmp_path)
    report = generate_report(tmp_path)

    assert report["meta"]["build_count"] == 2
    assert report["summary"]["builds_by_status"] == {"published": 1, "needs_attention": 1}
    assert report["summary"]["total_tool_calls"] == 2
    assert report["summary"]["total_tool_errors"] == 1
    assert report["summary"]["boundary_rejection_rate"] == 0.5
    assert report["summary"]["truncated_turns"] == 1
    assert report["summary"]["model_turns"] == {
        "deepseek/deepseek-v4-pro": 1, "local/tiny-4b": 1,
    }
    assert report["summary"]["signal_events"]["build.progress.stalled"] == 1

    by_id = {item["build_id"]: item for item in report["builds"]}
    ok = by_id["build-ok"]
    bad = by_id["build-bad"]

    # 脱敏默认：需求原文不出现，只有长度+哈希；工具参数/结果不落报告（错误摘要除外）
    assert "requirement_text" not in ok
    assert ok["requirement_len"] > 0 and len(ok["requirement_hash"]) == 12
    dumped = json.dumps(report, ensure_ascii=False)
    assert "珠宝店" not in dumped
    assert '"arguments"' not in dumped

    # per-actor 血缘与配置指纹进报告
    assert ok["builder"] == "classic"
    assert ok["team_state"]["coordinator_model"] == "deepseek/deepseek-v4-pro"
    assert ok["team_state"]["teammate_models"] == ["local/tiny-4b"]
    assert ok["transcript"]["tool_errors"] == {"draft_add_node": 1}
    assert ok["transcript"]["top_error_snippets"]["draft_add_node"] == ["invalid node: id required"]

    # 失败构建的错误摘要与信号事件
    assert "stalled" in bad["error"]
    assert bad["signal_events"] == {"build.progress.stalled": 1, "build.needs_attention": 1}
    assert bad["transcript"] == {"available": False}

    # 工具错误率表
    assert report["tool_stats"]["draft_add_node"] == {"calls": 2, "errors": 1, "error_rate": 0.5}


def test_report_can_include_requirement_text_explicitly(tmp_path: Path) -> None:
    _seed(tmp_path)
    report = generate_report(tmp_path, include_requirement_text=True)
    by_id = {item["build_id"]: item for item in report["builds"]}
    assert "珠宝店" in by_id["build-ok"]["requirement_text"]
    assert report["meta"]["requirement_text_included"] is True
