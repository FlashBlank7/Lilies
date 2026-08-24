"""mechanical 引擎（形态 B）：状态机管阶段、小模型只提案、边界拒绝进反馈重试、
关卡机械可判、全绿自动发布、血缘照常落库。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


class ScriptedSmallModel(ModelProvider):
    """按角色回放固定提案序列，模拟小模型：建图手第一步犯真实 4B 犯过的错
    （嵌套 node 拍平），被边界拒绝后第二步起改为合法提案。"""

    name = "scripted"

    GRAPH_BUILDER = [
        # 0) 查过手册才准用（新纪律，见 mechanical_builder 的 inspected_types 硬门）
        ("catalog_get", {"type": "start"}),
        ("catalog_get", {"type": "template_transform"}),
        ("catalog_get", {"type": "end"}),
        # 1) 预期失败：嵌套结构拍平 —— 必须被 Pydantic 边界拒绝
        ("draft_add_node", {"node": "start", "type": "start", "title": "输入"}),
        # 2) 之后全部合法
        ("draft_add_node", {"node": {
            "id": "start", "type": "start", "title": "输入",
            "config": {"inputs": [{"name": "name", "type": "string"}]},
        }}),
        ("draft_add_node", {"node": {
            "id": "template", "type": "template_transform", "title": "问候",
            "config": {
                "template": "Hello {{ name }}",
                "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
            },
        }}),
        ("draft_add_node", {"node": {
            "id": "end", "type": "end", "title": "输出",
            "config": {"outputs": {
                "greeting": {"$ref": {"node_id": "template", "path": ["text"]}},
            }},
        }}),
        ("draft_connect", {"edge": {
            "id": "start-template", "source": "start", "target": "template",
            "source_port": "output", "target_port": "input",
        }}),
        ("draft_connect", {"edge": {
            "id": "template-end", "source": "template", "target": "end",
            "source_port": "text", "target_port": "input",
        }}),
        ("phase_done", {"summary": "节点与连线就绪"}),
    ]
    TEST_AUTHOR = [
        ("test_add", {"test": {
            "id": "greeting-test", "name": "问候语精确断言",
            "requirement": "返回 Hello Ada", "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}],
            "mandatory": True,
        }}),
        ("phase_done", {"summary": "验收测试已锚定具体输出"}),
    ]

    def __init__(self) -> None:
        self.calls_by_user: dict[str, int] = {}
        self.models_by_user: dict[str, list[str]] = {}
        self.user_texts: dict[str, list[str]] = {}

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

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
        assert user_id is not None
        self.models_by_user.setdefault(user_id, []).append(model)
        self.user_texts.setdefault(user_id, []).append(
            " ".join(
                block.text or ""
                for message in messages for block in message.content
                if getattr(block, "type", "") == "text"
            )
        )
        index = self.calls_by_user.get(user_id, 0)
        self.calls_by_user[user_id] = index + 1
        if user_id.endswith("-graph-builder"):
            script = self.GRAPH_BUILDER
        elif user_id.endswith("-test-author"):
            script = self.TEST_AUTHOR
        else:  # repairer 不应被调用：脚本一次成型
            raise AssertionError(f"unexpected actor call: {user_id}")
        name, value = script[min(index, len(script) - 1)]
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}", "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_mechanical_engine_builds_publishes_and_records_lineage(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = ScriptedSmallModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Mechanical", "requirement": "输入姓名，输出 Hello 问候语。"},
        ).json()["id"]
        created = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "输入姓名 name，输出 greeting 字段，值为 Hello <name>。样例：Ada → Hello Ada。",
                "builder": "mechanical",
                "auto_publish": True,
                "max_turns": 20,
                "max_repair_cycles": 1,
                "coordinator_model": "scripted/tiny-4b",
            },
        ).json()
        build_id = created["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 关卡全过：测试全绿 → 自动发布
        assert build["status"] == "published", build.get("error")
        assert build["builder"] == "mechanical"

        # 全部提案都用了按构建指定的小模型（血缘不漂移）
        for user, models in provider.models_by_user.items():
            assert set(models) == {"scripted/tiny-4b"}, (user, models)
        # 修理手从未被叫醒（一次成型），两个提案角色都上过场
        actors = {user.split(f"{build_id}-", 1)[1] for user in provider.models_by_user}
        assert actors == {"graph-builder", "test-author"}

        # 被拒记录常驻黑板：第 1 步拍平被拒后，第 2 步的上下文必须还看得到
        # 这条拒绝（不会被中间步骤冲掉）；draft_add_node 成功后随即清除
        gb_texts = provider.user_texts[f"{build_id}-graph-builder"]
        assert "尚未解决的被拒提案" in gb_texts[4]  # 三次查阅后的拍平被拒
        assert "draft_add_node" in gb_texts[4]
        assert "尚未解决的被拒提案" not in gb_texts[5]

        # 转录血缘：第一笔提案是被边界拒绝的拍平调用，记录为 is_error
        transcript_path = tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"
        records = [json.loads(line) for line in transcript_path.read_text("utf-8").splitlines()]
        turns = [r for r in records if r.get("kind") == "turn"]
        gb_turns = [r for r in turns if r.get("actor") == "graph-builder"]
        assert gb_turns[0]["model"] == "scripted/tiny-4b"
        # 前三轮是查阅（新纪律要求查过手册才准用），第四轮才是被拒的拍平调用
        flattened = gb_turns[3]["tool_calls"][0]
        assert flattened["tool"] == "draft_add_node" and flattened["is_error"]

        # 投影必须真实反映草稿（曾误读 snapshot 键路径，长期谎报"草稿是空的"，
        # 模型据此反复重加已存在节点——信息面撒谎比模型犯错更难查，加回归）
        gb_all = provider.user_texts[f"{build_id}-graph-builder"]
        after_two_nodes = gb_all[6]  # 三次查阅 + 拍平被拒 + 成功加 start、template
        assert "节点 2 个" in after_two_nodes, after_two_nodes[:200]
        assert "已存在的节点 id：start、template" in after_two_nodes

        # 状态机阶段事件按序出现
        events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        phases = [
            event["data"]["phase"] for event in events
            if event["type"] == "build.mechanical.phase"
        ]
        assert phases[:3] == ["scaffold", "test", "verify"]


class PerseveratingSmallModel(ScriptedSmallModel):
    """病理模型：永远重复同一个不存在积木类型的提案（复刻真实 4B 连续原地
    重试 17 轮的现场）。反刍守卫应在第三次相同被拒提案时判停。"""

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        user_id = kwargs["user_id"]
        self.models_by_user.setdefault(user_id, []).append(kwargs["model"])
        index = self.calls_by_user.get(user_id, 0)
        self.calls_by_user[user_id] = index + 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}", "name": "catalog_get", "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps({"type": "constant"})},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_mechanical_engine_halts_perseverating_model(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = PerseveratingSmallModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Perseverate", "requirement": "任意需求。"},
        ).json()["id"]
        created = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "反刍守卫验证用的占位需求，内容不重要。",
                "builder": "mechanical",
                "auto_publish": True,
                "max_turns": 20,
                "max_repair_cycles": 1,
                "coordinator_model": "scripted/tiny-4b",
            },
        ).json()
        build_id = created["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 第三次相同被拒提案触发判停，而不是烧光 18 轮预算
        assert build["status"] == "needs_attention"
        assert "perseverating" in str(build.get("error") or "")
        calls = provider.calls_by_user[f"{build_id}-graph-builder"]
        # 提案竞争后一步最多 3 个候选：3 个被拒步 × 3 候选 = 9 次模型调用
        assert calls == 9, calls


class DiscoveryLoopSmallModel(ScriptedSmallModel):
    """病理模型：永远成功地查同一个存在的积木（复刻真实 4B start/end 交替
    重查 18 轮零进展的现场）。成功型循环守卫应在连续 8 次只读后判停。"""

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        user_id = kwargs["user_id"]
        self.models_by_user.setdefault(user_id, []).append(kwargs["model"])
        index = self.calls_by_user.get(user_id, 0)
        self.calls_by_user[user_id] = index + 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}", "name": "catalog_get", "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps({"type": "start"})},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_mechanical_engine_halts_discovery_loop(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = DiscoveryLoopSmallModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "DiscoveryLoop", "requirement": "成功型循环守卫验证用需求。"},
        ).json()["id"]
        created = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "成功型循环守卫验证用的占位需求，内容不重要。",
                "builder": "mechanical",
                "auto_publish": True,
                "max_turns": 30,
                "max_repair_cycles": 1,
                "coordinator_model": "scripted/tiny-4b",
            },
        ).json()
        build_id = created["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 行为升级（2026-08-23）：不再等到连续 8 次只读才判停——连续 2 次只读后
        # 机械收走查询类工具，第 3 次调用即被工具面硬门拒绝，重复三次触发判停。
        # 机制从"记账+事后判停"变成"当场堵死只读循环"。
        assert build["status"] == "needs_attention"
        assert "perseverating" in str(build.get("error") or "")
        calls = provider.calls_by_user[f"{build_id}-graph-builder"]
        # 竞争后按"步"算仍远早于旧的 8 次判停（每步 ≤3 候选）
        assert calls <= 12, calls
        transcript = tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"
        records = [json.loads(line) for line in transcript.read_text("utf-8").splitlines()]
        blocked = [
            tc for r in records if r.get("kind") == "turn"
            for tc in (r.get("tool_calls") or [])
            if tc.get("is_error") and "不可用" in str(tc.get("result"))
        ]
        assert blocked, "收走只读工具后应出现工具面硬门拒绝"


class OutOfPhaseToolModel(ScriptedSmallModel):
    """病理模型：始终调用 test_add（建图阶段工具面之外的工具）。
    阶段工具面必须是硬门——实测 4B 会照着系统提示里出现过的工具名乱叫，
    收窄工具列表只是建议，执法必须在执行边界做。"""

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        user_id = kwargs["user_id"]
        self.models_by_user.setdefault(user_id, []).append(kwargs["model"])
        index = self.calls_by_user.get(user_id, 0)
        self.calls_by_user[user_id] = index + 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}", "name": "test_add", "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps({"test": {"id": "x"}})},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_mechanical_engine_blocks_out_of_phase_tools(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = OutOfPhaseToolModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "OutOfPhase", "requirement": "阶段工具面硬门验证用需求。"},
        ).json()["id"]
        created = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers(),
            json={
                "requirement": "阶段工具面硬门验证用的占位需求，内容不重要。",
                "builder": "mechanical", "auto_publish": True,
                "max_turns": 20, "max_repair_cycles": 1,
                "coordinator_model": "scripted/tiny-4b",
            },
        ).json()
        build_id = created["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 越界工具被边界拒绝（而非静默执行），三次同样提案触发判停
        assert build["status"] == "needs_attention"
        assert "perseverating" in str(build.get("error") or "")
        transcript = tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"
        records = [json.loads(line) for line in transcript.read_text("utf-8").splitlines()]
        first = next(r for r in records if r.get("kind") == "turn")
        tool_record = first["tool_calls"][0]
        assert tool_record["is_error"]
        assert "不可用" in tool_record["result"]
        # 草稿零污染：越界的 test_add 没有真的落库
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert not (draft.get("snapshot", {}).get("tests") or [])


class TemperatureAwareModel(ScriptedSmallModel):
    """支持温度的替身：低温给坏提案、升温后给好提案——验证提案竞争在同一步
    预算内换温度重提，而不是把坏提案重复到判停（框架重设计 L1）。"""

    def __init__(self) -> None:
        super().__init__()
        self.temperatures: list[float | None] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        user_id = kwargs["user_id"]
        temperature = kwargs.get("temperature")
        self.temperatures.append(temperature)
        self.models_by_user.setdefault(user_id, []).append(kwargs["model"])
        index = self.calls_by_user.get(user_id, 0)
        self.calls_by_user[user_id] = index + 1
        # 低温（第一候选）永远吐拍平的坏提案；升温后给合法提案
        if temperature is not None and temperature <= 0.3:
            name, value = "draft_add_node", {"node": "start", "type": "start"}
        else:
            name, value = "catalog_get", {"type": "start"}
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}", "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_proposal_competition_escalates_temperature_within_one_step(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = TemperatureAwareModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Competition", "requirement": "提案竞争验证用需求。"},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers(),
            json={"requirement": "提案竞争验证用的占位需求，内容不重要。",
                  "builder": "mechanical", "auto_publish": True,
                  "max_turns": 24, "max_repair_cycles": 1,
                  "coordinator_model": "scripted/tiny-4b"},
        ).json()["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 温度阶梯被真的用上了：同一步里先低温后升温
        assert provider.temperatures[0] == 0.2, provider.temperatures[:3]
        assert 0.7 in provider.temperatures, provider.temperatures[:6]
        # 竞争事件记录了胜出候选
        events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        competitions = [e["data"] for e in events
                        if e["type"] == "build.mechanical.competition"]
        assert competitions, "应记录提案竞争结果"
        assert any(c.get("winner_index") for c in competitions), competitions[:3]
