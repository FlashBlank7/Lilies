"""查手册硬门必须永远可满足，且角色→模型分工不能反。

两条都由真机构建 0826a87a 的事故逼出：

1. **不可满足的拒绝 = 死锁**。查手册硬门喊"先调用 catalog_get"，而 force_action
   同一时刻把查询工具收走了（架构师阶段重复查阅留下的 redundant_reads 没在
   建图阶段入口清零）。32B 建图手连续 9 轮原样重提，60 秒被反刍守卫判死。
   教学式拒绝的前提是**它做得到**——做不到的指令不是教学，是死锁。

2. **角色反了 = 测的是配置事故**。mechanical 曾把 coordinator 当提案者、
   teammate[0] 当升级模型，与经典引擎的字段语义相反：操作者按"统筹=32B、
   队友=4B"传参，实际是 4B 做架构选型、32B 填表，与 B v2 设计意图颠倒。
"""

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


ADD_START = ("draft_add_node", {"node": {
    "id": "start", "type": "start", "title": "输入",
    "config": {"inputs": [{"name": "name", "type": "string"}]},
}})


class NeverQueriesModel(ModelProvider):
    """建图手一次手册都不查，直接建图——硬门若不可满足，这个脚本必然卡死。

    架构师阶段故意重复查同一份手册（真实 4B 的行为），以复现 redundant_reads
    渗漏到建图阶段的那个死锁前提。
    """

    name = "scripted"

    ARCHITECT = [
        ("catalog_get", {"type": "template_transform"}),
        ("catalog_get", {"type": "template_transform"}),  # 重复查阅 → redundant_reads
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收姓名"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出 greeting"},
        ], "notes": "start→template→end"}),
    ]
    GRAPH_BUILDER = [
        ADD_START,          # 被硬门拦一次（机器代劳查手册），下一轮原样重提
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
            "id": "e1", "source": "start", "target": "template",
            "source_port": "output", "target_port": "input",
        }}),
        ("draft_connect", {"edge": {
            "id": "e2", "source": "template", "target": "end",
            "source_port": "text", "target_port": "input",
        }}),
        ("phase_done", {"summary": "图就绪"}),
    ]
    TEST_AUTHOR = [
        ("test_add", {"test": {
            "id": "greeting", "name": "问候语精确断言",
            "requirement": "Ada → Hello Ada", "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}],
            "mandatory": True,
        }}),
        ("phase_done", {"summary": "验收就绪"}),
    ]

    def __init__(self) -> None:
        self.pos_by_user: dict[str, int] = {}
        self.models_by_user: dict[str, list[str]] = {}
        self.user_texts: dict[str, list[str]] = {}

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self, *, model: str, system: str, messages: list[ChatMessage],
        tools: list[ToolDefinition], max_output_tokens: int, thinking_enabled: bool,
        effort: str, tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # 签名里刻意不收 temperature / **kwargs：提案竞争的温度阶梯会让一"步"
        # 里发生多次模型调用，脚本回放对不上步。竞争本身另有测试覆盖，这里要
        # 单独测硬门。
        assert user_id is not None
        self.models_by_user.setdefault(user_id, []).append(model)
        text = " ".join(
            block.text or "" for message in messages for block in message.content
            if getattr(block, "type", "") == "text"
        )
        self.user_texts.setdefault(user_id, []).append(text)
        # 真实模型被硬门拦下后会重提同一个提案；脚本也照此回退一格，
        # 否则测的是"脚本会不会跳步"，不是"硬门可不可满足"。
        index = self.pos_by_user.get(user_id, 0)
        if "还没读过积木" in text and index > 0:
            index -= 1
        self.pos_by_user[user_id] = index + 1
        if user_id.endswith("-architect"):
            script = self.ARCHITECT
        elif user_id.endswith("-graph-builder"):
            script = self.GRAPH_BUILDER
        elif user_id.endswith("-test-author"):
            script = self.TEST_AUTHOR
        else:
            raise AssertionError(f"unexpected actor: {user_id}")
        name, value = script[min(index, len(script) - 1)]
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"{user_id}-{index}",
                              "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def _build(tmp_path: Path) -> tuple[dict, NeverQueriesModel, str, Path]:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = NeverQueriesModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(), json={
            "name": "Gate", "requirement": "输入姓名，输出 Hello 问候语。",
        }).json()["id"]
        created = client.post(f"/api/v1/applications/{app_id}/builds", headers=headers(), json={
            "requirement": "输入姓名 name，输出 greeting，值为 Hello <name>。样例：Ada → Hello Ada。",
            "builder": "mechanical", "auto_publish": True,
            "max_turns": 30, "max_repair_cycles": 1,
            # 经典引擎的直觉传参：统筹是大脑，队友是执行者
            "coordinator_model": "scripted/big-32b",
            "teammate_models": ["scripted/tiny-4b"],
        }).json()
        build_id = created["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)
    return build, provider, build_id, tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"


def test_manual_gate_is_always_satisfiable(tmp_path: Path) -> None:
    """从不查手册的建图手也必须走到发布：硬门代劳查阅，同一类型最多拦一次。"""
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    # 没有队友模型 → 不走架构规划 → 没有方案可循，查手册硬门是唯一防线。
    # 有方案时方案内积木的手册由平台预载、选型偏离另有硬门拦，它轮不到出场。
    provider = NeverQueriesModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(), json={
            "name": "Gate", "requirement": "输入姓名，输出 Hello 问候语。",
        }).json()["id"]
        build_id = client.post(f"/api/v1/applications/{app_id}/builds", headers=headers(), json={
            "requirement": "输入姓名 name，输出 greeting，值为 Hello <name>。样例：Ada → Hello Ada。",
            "builder": "mechanical", "auto_publish": True,
            "max_turns": 30, "max_repair_cycles": 1,
            "coordinator_model": "scripted/tiny-4b",   # 无队友 → 无架构师 → 无方案
        }).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)
    transcript = tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"

    assert build["status"] == "published", build.get("error")

    turns = [json.loads(line) for line in transcript.read_text("utf-8").splitlines()]
    gate_rejections = [
        call
        for record in turns if record.get("kind") == "turn"
        for call in record.get("tool_calls") or []
        if call.get("is_error") and "还没读过积木" in str(call.get("result") or "")
    ]
    gated_types = [
        str((call["arguments"]["node"] or {}).get("type")) for call in gate_rejections
    ]
    assert gated_types, "无方案时查手册硬门必须出场"
    # 同一类型绝不能拦两次——拦两次就说明"已替你查好"是空话，模型无路可走
    assert len(gated_types) == len(set(gated_types)), gated_types

    for call in gate_rejections:
        text = str(call["result"])
        # 拒绝必须**带着手册原文**，而不是指使模型去调一个可能已被收走的工具
        assert "已替你查好" in text
        assert "先调用 catalog_get" not in text
        assert len(text) > 200, text[:300]


def test_architect_config_sketch_reaches_the_builder(tmp_path: Path) -> None:
    """架构师写死的配置骨架必须原样出现在执行者的上下文里。

    实测 4B 知道 sum_by 这个函数名却写不对参数（写成 sum_by(pluck(...),pluck(...))
    并发明 $ref 内联语法），同一提示下 32B 一次就对。判断留给架构师、执行者只
    照抄——这条通道断了，形态 B 就退回"小模型自己推导"。
    """
    build, provider, build_id, _ = _build(tmp_path)
    assert build["status"] == "published", build.get("error")

    prompts = provider.user_texts[f"{build_id}-graph-builder"]
    assert any('template="Hello {{ name }}"' in text for text in prompts), prompts[0][:400]
    assert any("关键配置（照抄，不要自己推导）" in text for text in prompts)


def test_roles_follow_classic_semantics(tmp_path: Path) -> None:
    """统筹模型做架构选型，队友模型做逐步提案——与经典引擎字段语义一致。"""
    build, provider, build_id, _ = _build(tmp_path)
    assert build["status"] == "published", build.get("error")

    architect = provider.models_by_user.get(f"{build_id}-architect")
    graph = provider.models_by_user.get(f"{build_id}-graph-builder")
    tester = provider.models_by_user.get(f"{build_id}-test-author")

    assert architect and set(architect) == {"scripted/big-32b"}, architect
    assert graph and set(graph) == {"scripted/tiny-4b"}, graph
    assert tester and set(tester) == {"scripted/tiny-4b"}, tester


class AlwaysDeclaresDoneModel(NeverQueriesModel):
    """建图手对着空草稿一路宣布完成——原先没有任何守卫管这条路径。

    phase_done 每次都"执行成功"，反刍计数管不着，模型可以一直宣布到建图预算
    烧光（脚本复现：16 次 phase_done）。预算烧光后报的是"scaffold budget
    exhausted"，把一个可诊断的空转伪装成资源不足。
    """

    ARCHITECT = [
        # 方案本身必须合法（方案的机械校验另有覆盖），这里测的是建图阶段的空转
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收姓名"},
            {"id": "end", "type": "end", "purpose": "输出问候语"},
        ], "notes": "略"}),
    ]
    GRAPH_BUILDER = [("phase_done", {"summary": "图就绪"})]


def test_declaring_done_against_unchanged_errors_escalates_then_stops(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = AlwaysDeclaresDoneModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(), json={
            "name": "Stall", "requirement": "输入姓名，输出问候语。",
        }).json()["id"]
        build_id = client.post(f"/api/v1/applications/{app_id}/builds", headers=headers(), json={
            "requirement": "输入姓名 name，输出 greeting。",
            "builder": "mechanical", "auto_publish": True,
            "max_turns": 40, "max_repair_cycles": 1,
            "coordinator_model": "scripted/big-32b",
            "teammate_models": ["scripted/tiny-4b"],
        }).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

    # 判停理由必须点名空转，而不是伪装成"预算不足"
    assert build["status"] == "needs_attention"
    assert "phase_done_unfixed" in (build.get("error") or ""), build.get("error")

    # 恢复手段优先于放弃手段：判停前必须先走过升级阶梯（里程碑落在转录里）
    transcript = tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"
    marks = "\n".join(
        str(json.loads(line).get("text") or "")
        for line in transcript.read_text("utf-8").splitlines()
        if json.loads(line).get("kind") == "event"
    )
    assert "升级到 scripted/big-32b" in marks, marks[-600:]

    # 空转不该烧光预算：升级前 3 步 + 升级后 3 步，远少于建图预算
    calls = sum(len(v) for k, v in provider.models_by_user.items() if k.endswith("-graph-builder"))
    assert calls <= 10, calls


class BadPlanThenGoodModel(NeverQueriesModel):
    """架构师先给一份**选型与配置矛盾**的方案，被机械校验退回后改对。

    真机构建 b44d3594：32B 架构师把 sum_by(sales,"store","amount") 挂在
    variable_aggregator 上。那个积木只合并分支值，公式会被当普通字符串吞掉——
    执行者照方案实现，最后死在下游。方案是自由文本，此前平台一个字都不检查。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "calc", "type": "variable_aggregator", "purpose": "分组求和",
             "config_sketch": 'sum_by(sales, "store", "amount")'},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "start→calc→end"}),
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "start→template→end"}),
    ]


class NonexistentBlockPlanModel(NeverQueriesModel):
    """架构师选了不存在的积木类型——执行者会对着它反复重试到判停。"""

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "sum", "type": "group_sum_aggregator", "purpose": "分组求和"},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "略"}),
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "start→template→end"}),
    ]


def _rejections(transcript: Path, actor: str) -> list[str]:
    out = []
    for line in transcript.read_text("utf-8").splitlines():
        record = json.loads(line)
        if record.get("kind") != "turn" or record.get("actor") != actor:
            continue
        out += [
            str(call.get("result") or "")
            for call in record.get("tool_calls") or [] if call.get("is_error")
        ]
    return out


def _run(provider: NeverQueriesModel, tmp_path: Path) -> tuple[dict, str, Path]:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(), json={
            "name": "Plan", "requirement": "输入姓名，输出 Hello 问候语。",
        }).json()["id"]
        build_id = client.post(f"/api/v1/applications/{app_id}/builds", headers=headers(), json={
            "requirement": "输入姓名 name，输出 greeting，值为 Hello <name>。样例：Ada → Hello Ada。",
            "builder": "mechanical", "auto_publish": True,
            "max_turns": 30, "max_repair_cycles": 1,
            "coordinator_model": "scripted/big-32b",
            "teammate_models": ["scripted/tiny-4b"],
        }).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)
    return build, build_id, tmp_path / "data" / "build_transcripts" / f"{build_id}.jsonl"


class GamedPlanThenGoodModel(NeverQueriesModel):
    """架构师被退回后**删掉公式**、保留错积木——检查若只看字面就被绕过去了。

    真机构建 310141fd：32B 拿到"公式不能挂在 variable_aggregator 上"的退回后，
    没有改积木类型，只是把 sum_by(...) 从 config_sketch 里删了，方案照样是错的。
    判据必须落在"这个节点要干什么"上，不是"它写了什么字"。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "calc", "type": "variable_aggregator", "purpose": "按门店分组求和销售额",
             "config_sketch": "aggregation_field = 'store'"},   # 公式已删，意图仍是算术
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "略"}),
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "start→template→end"}),
    ]


def test_plan_check_cannot_be_gamed_by_deleting_the_formula(tmp_path: Path) -> None:
    build, _, transcript = _run(GamedPlanThenGoodModel(), tmp_path)
    rejections = _rejections(transcript, "architect")
    assert any("不会做任何计算" in text for text in rejections), rejections
    # 白名单必须报出来，否则模型只能靠猜下一个类型（真机上它就换成了 tool）
    assert any("variable_assigner" in text for text in rejections)
    assert any("都不算改对" in text for text in rejections)
    assert build["status"] == "published", build.get("error")


class SwappedToRealButNonComputingModel(NeverQueriesModel):
    """第二种规避：换一个**真实存在**的积木类型，指向一个不存在的能力。

    真机构建 92af320c：32B 把分组求和挂到 type="tool"、config_sketch 写
    "function: sum_by_store"——类型存在、没有公式文本，黑名单式检查全过，
    但那个函数根本不存在，执行者只能对着它空转到判停。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "sum_by_store", "type": "tool", "purpose": "计算各门店的销售合计",
             "config_sketch": "function: sum_by_store, parameters: sales"},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "略"}),
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收输入"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出"},
        ], "notes": "start→template→end"}),
    ]


def test_plan_check_cannot_be_gamed_by_swapping_to_another_real_type(tmp_path: Path) -> None:
    build, _, transcript = _run(SwappedToRealButNonComputingModel(), tmp_path)
    rejections = _rejections(transcript, "architect")
    assert any("不会做任何计算" in text for text in rejections), rejections
    assert build["status"] == "published", build.get("error")


def test_plan_with_formula_on_non_computing_block_is_rejected(tmp_path: Path) -> None:
    build, _, transcript = _run(BadPlanThenGoodModel(), tmp_path)
    rejections = _rejections(transcript, "architect")
    assert any("只有 variable_assigner 会真正求值" in text for text in rejections), rejections
    # 退回后架构师改对，构建照样走完——校验是纠偏不是判死
    assert build["status"] == "published", build.get("error")


def test_plan_with_unknown_block_type_is_rejected_with_near_matches(tmp_path: Path) -> None:
    build, _, transcript = _run(NonexistentBlockPlanModel(), tmp_path)
    rejections = _rejections(transcript, "architect")
    assert any("不存在" in text for text in rejections), rejections
    assert build["status"] == "published", build.get("error")


class WeakThenAnchoredTestModel(NeverQueriesModel):
    """验收作者先写一条只验形状的测试——关卡必须不认，逼出锚定具体值的那条。

    只数"有没有 mandatory 测试"是假关卡：工作流算错了，exists 断言照样全绿。
    本项目已因此出过一次假成功（构建 55434ea9 绕过验收证据发布）。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收姓名"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出 greeting"},
        ], "notes": "start→template→end"}),
    ]
    TEST_AUTHOR = [
        ("test_add", {"test": {
            "id": "weak", "name": "只验形状", "requirement": "有 greeting 字段",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "exists"}],
            "mandatory": True,
        }}),
        ("phase_done", {"summary": "验收就绪"}),   # 关卡必须不认这一次
        ("test_add", {"test": {
            "id": "anchored", "name": "锚定具体值", "requirement": "Ada → Hello Ada",
            "inputs": {"name": "Ada"},
            "assertions": [
                {"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"},
            ],
            "mandatory": True,
        }}),
        ("phase_done", {"summary": "验收就绪"}),
    ]


def test_structural_only_acceptance_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    provider = WeakThenAnchoredTestModel()
    build, build_id, transcript = _run(provider, tmp_path)
    assert build["status"] == "published", build.get("error")

    prompts = provider.user_texts[f"{build_id}-test-author"]
    # 第一次 phase_done 必须被顶回来，并说清楚为什么
    assert any("只验了形状" in text for text in prompts), prompts[-1][-500:]
    assert any("断言具体值" in text or "锚定具体值" in text for text in prompts)

    # 最终发布物带的是锚定具体值的那条
    turns = [json.loads(line) for line in transcript.read_text("utf-8").splitlines()]
    added = [
        call["arguments"]["test"]["id"]
        for record in turns if record.get("kind") == "turn"
        for call in record.get("tool_calls") or []
        if call.get("tool") == "test_add" and not call.get("is_error")
    ]
    assert "anchored" in added, added


def test_scaffold_budget_exhaustion_still_passes_when_the_gate_is_met(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """关卡是 draft_validate，不是"模型宣布完成"。

    真机构建 93430e0b：图建全、线连全，最后一条边刚落库就撞上建图预算上限，
    整单被当成"建图失败"判死——关卡明明已经达成。机械阶梯第 5 级"关卡达成即判"
    此前只落在验收阶段，这里补到建图阶段的预算出口上。
    """
    from agent_platform import mechanical_builder

    # 预算刚好卡在"图建完、还没来得及 phase_done"的位置
    monkeypatch.setattr(mechanical_builder, "SCAFFOLD_BUDGET", 5)

    provider = NeverQueriesModel()
    build, build_id, transcript = _run(provider, tmp_path)

    assert build["status"] == "published", build.get("error")
    marks = "\n".join(
        str(json.loads(line).get("text") or "")
        for line in transcript.read_text("utf-8").splitlines()
        if json.loads(line).get("kind") == "event"
    )
    assert "关卡达成即判" in marks, marks[-500:]


def test_state_machine_calls_do_not_eat_the_model_tool_budget(tmp_path: Path) -> None:
    """平台自己的工具调用不该从模型的工具预算里扣。

    工具预算约束的是**模型的行为**。状态机每一步都要 draft_inspect + draft_validate，
    查手册硬门还会代模型 catalog_get——这些都是平台的自查自纠，记进同一个额度就
    等于"机械阶梯越完善，模型能做的事越少"。真机构建 7d5ffa06 正是这么撞上
    tool_call 201/200 被判死的：它当时已经走到修复阶段，是这一路最远的一单。
    账照记（platform_tool_call），只是不占模型额度。
    """
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = NeverQueriesModel()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(), json={
            "name": "Budget", "requirement": "输入姓名，输出 Hello 问候语。",
        }).json()["id"]
        build_id = client.post(f"/api/v1/applications/{app_id}/builds", headers=headers(), json={
            "requirement": "输入姓名 name，输出 greeting，值为 Hello <name>。样例：Ada → Hello Ada。",
            "builder": "mechanical", "auto_publish": True,
            "max_turns": 30, "max_repair_cycles": 1,
            "coordinator_model": "scripted/big-32b",
            "teammate_models": ["scripted/tiny-4b"],
        }).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)
        assert build["status"] == "published", build.get("error")

        task = client.get(
            f"/api/v1/platform/harness/tasks/{build_id}", headers=headers()
        )
        if task.status_code != 200:   # 该只读端点未开放时退回直读 harness
            record = app.state.services.harness._tasks[build_id]
            counts = dict(record.usage_counts)
        else:
            counts = task.json().get("usage_counts") or {}

    # 状态机的活儿确实被记了账
    assert counts.get("platform_tool_call", 0) > 0, counts
    # 而模型的工具额度只花在模型自己的动作上（本脚本没有任何模型侧工具调用
    # 走 _execute，所以计数应当很小——关键是它不再随状态机的勤快程度膨胀）
    assert counts.get("tool_call", 0) < counts["platform_tool_call"], counts


class DeviatesFromPlanModel(NeverQueriesModel):
    """方案定了 variable_assigner，建图手转头加了 variable_aggregator。

    真机构建 e28708d3：方案校验两次把 variable_aggregator 顶回去、架构师改对了，
    建图手照样加了它——方案文本里"按此实现，不要另选积木"只是建议。那个积木
    不做算术，配置怎么改都算不出东西，于是修复阶段空转 48 轮（32/39 次提案被拒，
    全是"would not change the workflow"），最后耗尽轮数预算。
    形态 B 的分工是"强模型定选型、小模型照单填表"，选型偏离必须是硬门。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收姓名"},
            {"id": "calc", "type": "variable_assigner", "purpose": "拼问候语",
             "config_sketch": 'greeting = {"$concat": ["Hello ", start.name]}'},
            {"id": "end", "type": "end", "purpose": "输出 greeting"},
        ], "notes": "start→calc→end"}),
    ]
    GRAPH_BUILDER = [
        # 方案之外的积木——必须当场拒
        ("draft_add_node", {"node": {
            "id": "calc", "type": "variable_aggregator", "title": "聚合",
            "config": {"variables": [{"name": "greeting"}]},
        }}),
        ("draft_add_node", {"node": {
            "id": "start", "type": "start", "title": "输入",
            "config": {"inputs": [{"name": "name", "type": "string"}]},
        }}),
        ("draft_add_node", {"node": {
            "id": "calc", "type": "variable_assigner", "title": "拼问候语",
            "config": {"assignments": {"greeting": {"$concat": [
                "Hello ", {"$ref": {"node_id": "start", "path": ["name"]}},
            ]}}},
        }}),
        ("draft_add_node", {"node": {
            "id": "end", "type": "end", "title": "输出",
            "config": {"outputs": {"greeting": {"$ref": {
                "node_id": "calc", "path": ["output", "greeting"]}}}},
        }}),
        ("draft_connect", {"edge": {"id": "e1", "source": "start", "target": "calc",
                                     "source_port": "output", "target_port": "input"}}),
        ("draft_connect", {"edge": {"id": "e2", "source": "calc", "target": "end",
                                     "source_port": "output", "target_port": "input"}}),
        ("phase_done", {"summary": "图就绪"}),
    ]
    TEST_AUTHOR = [
        ("test_add", {"test": {
            "id": "greeting", "name": "锚定具体值", "requirement": "Ada → Hello Ada",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals",
                            "expected": "Hello Ada"}],
            "mandatory": True,
        }}),
        ("phase_done", {"summary": "验收就绪"}),
    ]


def test_graph_builder_cannot_use_blocks_outside_the_approved_plan(tmp_path: Path) -> None:
    provider = DeviatesFromPlanModel()
    build, build_id, transcript = _run(provider, tmp_path)

    rejections = _rejections(transcript, "graph-builder")
    assert any("不在架构方案里" in text for text in rejections), rejections
    # 拒绝要报出方案选定的积木，否则模型只能猜
    assert any("variable_assigner" in text for text in rejections)
    # 被拒之后照方案实现，构建照常走完——硬门是纠偏不是判死
    assert build["status"] == "published", build.get("error")

    types = {
        str((call["arguments"]["node"] or {}).get("type"))
        for record in (json.loads(line) for line in transcript.read_text("utf-8").splitlines())
        if record.get("kind") == "turn"
        for call in record.get("tool_calls") or []
        if call.get("tool") == "draft_add_node" and not call.get("is_error")
    }
    assert "variable_aggregator" not in types, types


class NoopUpdateLoopModel(NeverQueriesModel):
    """图建全后对着同一个节点反复提交**零变化**的更新，从不 phase_done。

    真机构建 82a730cd：图早就建全了，模型对着 end 反复提交一模一样的 outputs，
    反刍守卫（第 3 次）比"连续无进展即自动推进"（也是第 3 次）早一轮开火，
    把一单已经合格的建图判死。守卫是用来止损的——关卡已经达成就没有损可止。
    """

    GRAPH_BUILDER = [
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
            "config": {"outputs": {"greeting": {"$ref": {
                "node_id": "template", "path": ["text"]}}}},
        }}),
        ("draft_connect", {"edge": {"id": "e1", "source": "start", "target": "template",
                                     "source_port": "output", "target_port": "input"}}),
        ("draft_connect", {"edge": {"id": "e2", "source": "template", "target": "end",
                                     "source_port": "text", "target_port": "input"}}),
        # 之后永远是零变化的更新——脚本用尽后会一直重复最后一条
        ("draft_update_node", {"node_id": "end", "changes": {"config": {"outputs": {
            "greeting": {"$ref": {"node_id": "template", "path": ["text"]}},
        }}}}),
    ]


def test_gate_is_checked_before_the_perseveration_guard_kills_the_phase(
    tmp_path: Path,
) -> None:
    provider = NoopUpdateLoopModel()
    build, build_id, transcript = _run(provider, tmp_path)

    # 关键是**别把一单合格的建图判死**：图是全的、校验是绿的，状态机自己往前推，
    # 走哪条推进路径（守卫前判关卡 / 连续无进展自动推进）都算数。
    assert build["status"] == "published", build.get("error")
    assert "perseverating" not in (build.get("error") or "")
    marks = "\n".join(
        str(json.loads(line).get("text") or "")
        for line in transcript.read_text("utf-8").splitlines()
        if json.loads(line).get("kind") == "event"
    )
    assert ("关卡达成即判" in marks) or ("自动推进" in marks), marks[-600:]


def test_projection_tells_each_node_how_to_be_referenced() -> None:
    """投影要说清"这个节点能被怎么引用"——不同积木的产出形状不一样。

    start 直接给 {输入名: 值}，variable_assigner 包在 output 下，
    template_transform 给 {"text": ...}。这份知识此前完全不在投影里，模型只能
    靠记，真机里反复写错：把 start 的字段写成 ["output","sales"]（多一层）、
    把 variable_assigner 的产出写成 ["by_store"]（少一层）。
    路径按**真实输入语法**给出（分段数组），因为模型会照抄。
    """
    from agent_platform.mechanical_builder import referenceable_paths

    assert referenceable_paths(
        {"type": "start", "config": {"inputs": [{"name": "sales"}]}}
    ) == ['["sales"]']
    assert referenceable_paths(
        {"type": "variable_assigner", "config": {"assignments": {"by_store": 1}}}
    ) == ['["output", "by_store"]']
    assert referenceable_paths({"type": "template_transform", "config": {}}) == ['["text"]']
    # 终点节点不产出给别人引用的东西
    assert referenceable_paths({"type": "end", "config": {}}) == []


class RewritesSameTestForeverModel(NeverQueriesModel):
    """验收作者写好一条合格测试后，一遍遍重写它，从不宣布完成。

    真机构建 400a231e：关卡早已达成（mandatory + equals 2000），模型却在预算
    最后几步反复重写同一条测试，被报成"没有 mandatory 验收测试"判死。
    两处都得修：反复重写同一条**不算进展**；预算出口判死前先判一次关卡。
    """

    ARCHITECT = [
        ("architecture_plan", {"nodes": [
            {"id": "start", "type": "start", "purpose": "接收姓名"},
            {"id": "template", "type": "template_transform", "purpose": "拼问候语",
             "config_sketch": 'template="Hello {{ name }}"，name 绑 start.name'},
            {"id": "end", "type": "end", "purpose": "输出 greeting"},
        ], "notes": "start→template→end"}),
    ]
    TEST_AUTHOR = [
        ("test_add", {"test": {
            "id": "greeting", "name": "锚定具体值", "requirement": "Ada → Hello Ada",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals",
                            "expected": "Hello Ada"}],
            "mandatory": True,
        }}),
        # 之后永远重写同一条（脚本用尽后一直重复最后一条）
        ("test_add", {"test": {
            "id": "greeting", "name": "锚定具体值（再写一遍）",
            "requirement": "Ada → Hello Ada", "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals",
                            "expected": "Hello Ada"}],
            "mandatory": True,
        }}),
    ]


def test_rewriting_the_same_test_does_not_count_as_progress(tmp_path: Path) -> None:
    provider = RewritesSameTestForeverModel()
    build, build_id, transcript = _run(provider, tmp_path)

    assert build["status"] == "published", build.get("error")
    assert "without a mandatory acceptance test" not in (build.get("error") or "")
    marks = "\n".join(
        str(json.loads(line).get("text") or "")
        for line in transcript.read_text("utf-8").splitlines()
        if json.loads(line).get("kind") == "event"
    )
    assert ("自动推进到跑测阶段" in marks) or ("关卡达成即判" in marks), marks[-600:]
