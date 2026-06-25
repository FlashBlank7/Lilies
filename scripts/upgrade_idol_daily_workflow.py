from __future__ import annotations

import argparse
import asyncio
from typing import Any

from agent_platform.api import build_services
from agent_platform.config import Settings
from agent_platform.workflow_models import (
    ApplicationCreateRequest,
    ApplicationMode,
    ApplicationSnapshot,
    EdgeSpec,
    NodeSpec,
    Position,
    RetryPolicy,
    TestAssertion,
    WorkflowSpec,
    WorkflowTestCase,
)


APP_NAME = "日本女性アイドル Daily"
DEFAULT_REQUIREMENT = (
    "搭建一个定时每天东京时间 8:00，在网络上搜索最近资讯并整理，生成每日日本女性偶像团体活动、"
    "八卦和新信息新闻日报的智能体。工作流必须可人工微调：搜索类别、聚合、可信度判断、确认/传闻分支和输出均用显式积木表达。"
)


def ref(node_id: str, *path: str, optional: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {"node_id": node_id, "path": list(path)}
    if optional:
        value["optional"] = True
    return {"$ref": value}


def node(
    node_id: str,
    block_type: str,
    title: str,
    config: dict[str, Any],
    x: float,
    y: float,
    description: str = "",
) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        type=block_type,
        title=title,
        description=description,
        config=config,
        position=Position(x=x, y=y),
        retry=RetryPolicy(enabled=block_type in {"tool", "tool_executor", "parameter_extractor"}, max_attempts=2),
    )


def edge(edge_id: str, source: str, target: str, source_port: str = "output", branch: str | None = None) -> EdgeSpec:
    return EdgeSpec(
        id=edge_id,
        source=source,
        target=target,
        source_port=source_port,
        target_port="input",
        branch=branch,
    )


def build_snapshot() -> ApplicationSnapshot:
    categories = {
        "search_news": "日本 女性アイドル グループ ニュース OR 発表 OR 活動",
        "search_events": "日本 女性アイドル ライブ イベント ツアー 握手会 発表",
        "search_members": "日本 女性アイドル 新メンバー 卒業 加入 脱退",
        "search_social": "日本 女性アイドル SNS 話題 X Instagram TikTok",
        "search_gossip": "日本 女性アイドル 噂 熱愛 炎上 まとめ",
    }
    search_nodes = [
        node(
            node_id,
            "tool_executor",
            title,
            {
                "input": ref("context", "output"),
                "settings": {
                    "tool_name": "WebSearch",
                    "tool_input": {"query": query, "max_results": 8, "language": "ja", "country": "JP"},
                },
            },
            620,
            40 + index * 120,
            "显式 WebSearch 架构积木，可人工调整关键词、语言、国家和结果数。",
        )
        for index, (node_id, query) in enumerate(categories.items())
        for title in [node_id.replace("search_", "搜索：")]
    ]
    return ApplicationSnapshot(
        name=APP_NAME,
        description="每天 8:00 自动搜索日本女性偶像团体近况，并输出带证据链接的中文日报。",
        mode=ApplicationMode.workflow,
        requirement=DEFAULT_REQUIREMENT,
        agents={},
        workflow=WorkflowSpec(
            nodes=[
                node(
                    "schedule",
                    "schedule_trigger",
                    "每天 08:00 JST",
                    {
                        "timezone": "Asia/Tokyo",
                        "hour": 8,
                        "minute": 0,
                        "inputs": {"topic": "日本女性偶像团体日报", "language": "zh-CN"},
                    },
                    40,
                    260,
                    "发布版本后由后端调度器每天触发一次。",
                ),
                node(
                    "context",
                    "context_assembler",
                    "日报上下文",
                    {
                        "input": ref("schedule", "output"),
                        "settings": {
                            "fragments": [ref("schedule", "topic"), "只使用 WebSearch 返回的证据 URL。"],
                        },
                    },
                    330,
                    260,
                    "把触发输入和编辑边界组装为搜索上下文。",
                ),
                *search_nodes,
                node(
                    "aggregate_search",
                    "variable_aggregator",
                    "聚合五类搜索证据",
                    {
                        "variables": [ref(item.id, "output") for item in search_nodes],
                        "mode": "array",
                    },
                    940,
                    260,
                    "把新闻、活动、成员变动、社媒热度和传闻搜索结果汇总。",
                ),
                node(
                    "compact_evidence",
                    "context_compactor",
                    "压缩证据上下文",
                    {
                        "input": ref("aggregate_search", "output"),
                        "settings": {
                            "max_chars": 8000,
                            "preserved_facts": ["title", "url", "published_at", "source", "query"],
                        },
                    },
                    1240,
                    260,
                    "保留标题、链接、来源和发布时间，限制日报上下文大小。",
                ),
                node(
                    "format_report",
                    "template_transform",
                    "证据日报模板",
                    {
                        "template": (
                            "## 日本女性偶像团体日报\n"
                            "定时: 08:00 JST\n\n"
                            "### 重点资讯\n"
                            "1. {{ t1 }}\n{{ u1 }}\n"
                            "2. {{ t2 }}\n{{ u2 }}\n"
                            "3. {{ t3 }}\n{{ u3 }}\n"
                            "4. {{ t4 }}\n{{ u4 }}\n"
                            "5. {{ t5 }}\n{{ u5 }}\n\n"
                            "### 说明\n"
                            "以上链接均逐字来自本次 WebSearch 工具结果；未出现在工具证据中的 URL 不得进入日报。"
                        ),
                        "variables": {
                            "t1": ref("search_news", "output", "results", "0", "title"),
                            "u1": ref("search_news", "output", "results", "0", "url"),
                            "t2": ref("search_events", "output", "results", "0", "title"),
                            "u2": ref("search_events", "output", "results", "0", "url"),
                            "t3": ref("search_members", "output", "results", "0", "title"),
                            "u3": ref("search_members", "output", "results", "0", "url"),
                            "t4": ref("search_social", "output", "results", "0", "title"),
                            "u4": ref("search_social", "output", "results", "0", "url"),
                            "t5": ref("search_gossip", "output", "results", "0", "title"),
                            "u5": ref("search_gossip", "output", "results", "0", "url"),
                        },
                    },
                    1540,
                    260,
                    "确定性地把上游搜索返回的精确 URL 加到日报中，避免模型漏引或改写链接。",
                ),
                node(
                    "trace",
                    "event_recorder",
                    "记录日报 Trace",
                    {
                        "input": ref("format_report", "text"),
                        "settings": {"label": "idol_daily_evidence_report"},
                    },
                    1840,
                    260,
                ),
                node(
                    "answer",
                    "answer",
                    "日报输出",
                    {
                        "answer": {
                            "status": "evidence_cited",
                            "report": ref("format_report", "text"),
                            "trace": ref("trace", "state"),
                        }
                    },
                    2140,
                    260,
                ),
            ],
            edges=[
                edge("schedule-context", "schedule", "context"),
                *[edge(f"context-{item.id}", "context", item.id) for item in search_nodes],
                *[edge(f"{item.id}-aggregate", item.id, "aggregate_search") for item in search_nodes],
                edge("aggregate-compact", "aggregate_search", "compact_evidence"),
                edge("compact-format", "compact_evidence", "format_report"),
                edge("format-trace", "format_report", "trace", "text"),
                edge("trace-answer", "trace", "answer"),
            ],
            viewport={"x": 0, "y": 0, "zoom": 0.45},
        ),
        tests=[
            WorkflowTestCase(
                id="idol_daily_architecture_acceptance",
                name="日本女性偶像日报架构积木验收",
                requirement="必须用显式定时、WebSearch、聚合、压缩、模板和事件积木生成带证据链接的日报。",
                inputs={},
                assertions=[
                    TestAssertion(path=["answer", "report"], operator="type", expected="string"),
                    TestAssertion(path=["answer", "report"], operator="contains", expected="08:00 JST"),
                    TestAssertion(path=["answer", "report"], operator="contains", expected="https://"),
                    TestAssertion(path=["answer", "status"], operator="equals", expected="evidence_cited"),
                    TestAssertion(path=["answer", "trace", "recorded"], operator="equals", expected=True),
                ],
                required_node_types=[
                    "schedule_trigger",
                    "context_assembler",
                    "tool_executor",
                    "variable_aggregator",
                    "context_compactor",
                    "template_transform",
                    "event_recorder",
                    "answer",
                ],
                required_tool_nodes=["WebSearch"],
                required_tools=["WebSearch"],
                minimum_tool_calls=5,
                require_cited_tool_urls=True,
                mandatory=True,
            )
        ],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", default="b86beecf-2567-40c9-a71c-9f49f8ff046e")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    settings.prepare()
    services = build_services(settings)
    await services.storage.initialize()
    await services.workflow_store.initialize()

    snapshot = build_snapshot()
    try:
        draft = await services.workflow_store.get_draft(args.app_id)
        application_id = args.app_id
    except KeyError:
        created = await services.workflow_store.create_application(
            ApplicationCreateRequest(
                name=snapshot.name,
                description=snapshot.description,
                requirement=snapshot.requirement,
                mode=snapshot.mode,
            )
        )
        application_id = created["id"]
        draft = await services.workflow_store.get_draft(application_id)

    saved = await services.workflow_store.save_draft(
        application_id,
        snapshot,
        expected_revision=int(draft["revision"]),
        idempotency_key=f"upgrade-idol-daily:{snapshot.content_hash()}",
    )
    print({"application_id": application_id, **saved})

    validation = await services.applications.validate_draft(application_id)
    print({"validation": validation})
    if not validation["valid"]:
        raise SystemExit(1)

    if args.run_tests or args.publish:
        report = await services.workflow_runtime.run_test_suite(application_id)
        print({"tests": report})
        if not report["passed"]:
            raise SystemExit(2)
    if args.publish:
        published = await services.workflow_store.publish(application_id)
        print({"published": published})


if __name__ == "__main__":
    asyncio.run(main())
