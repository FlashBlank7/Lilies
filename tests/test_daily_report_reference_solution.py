"""日报基准的参照解：证明该基准在当前积木/公式能力下确实可解。

基准效度纪律（2026-08-23 事故教训）：拿去测模型的任务必须先有人工参照解——
此前日报基准在积木层实际无解（公式引擎无法对对象数组分组求和），形态 A 的
"成功"是硬编码门店名的作弊、形态 B 的 0/8 有一半是无解考卷的必然。
本测试即参照解的机器证明：能力若倒退（公式函数被删、积木语义变更），这里先红。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncIterator
import json

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class ReferenceSolutionScript(ModelProvider):
    """脚本化的参照解：与人类专家操作等价的草稿操作序列。"""

    name = "scripted"

    OPERATIONS: list[tuple[str, dict[str, Any]]] = [
        ("draft_add_node", {"node": {
            "id": "start", "type": "start", "title": "输入",
            "config": {"inputs": [{
                "name": "sales", "label": "门店销售流水", "type": "array",
                "example": [{"store": "A店", "amount": 1200}],
            }]},
        }}),
        ("draft_add_node", {"node": {
            "id": "calc", "type": "variable_assigner", "title": "分组合计",
            "config": {"assignments": {
                "by_store": {"$formula": {
                    "expression": 'sum_by(sales, "store", "amount")',
                    "vars": {"sales": {"$ref": {"node_id": "start", "path": ["sales"]}}},
                }},
                "total": {"$formula": {
                    "expression": 'sum(pluck(sales, "amount"))',
                    "vars": {"sales": {"$ref": {"node_id": "start", "path": ["sales"]}}},
                }},
            }},
        }}),
        ("draft_add_node", {"node": {
            "id": "report", "type": "template_transform", "title": "日报文本",
            "config": {
                "template": "各门店合计：{{ by_store }}；总计{{ total }}元。",
                "variables": {
                    "by_store": {"$ref": {"node_id": "calc", "path": ["output", "by_store"]}},
                    "total": {"$ref": {"node_id": "calc", "path": ["output", "total"]}},
                },
            },
        }}),
        ("draft_add_node", {"node": {
            "id": "end", "type": "end", "title": "输出",
            "config": {"outputs": {
                "by_store": {"$ref": {"node_id": "calc", "path": ["output", "by_store"]}},
                "total": {"$ref": {"node_id": "calc", "path": ["output", "total"]}},
                "report": {"$ref": {"node_id": "report", "path": ["text"]}},
            }},
        }}),
        ("draft_connect", {"edge": {"id": "e1", "source": "start", "target": "calc",
                                     "source_port": "output", "target_port": "input"}}),
        ("draft_connect", {"edge": {"id": "e2", "source": "calc", "target": "report",
                                     "source_port": "output", "target_port": "input"}}),
        ("draft_connect", {"edge": {"id": "e3", "source": "report", "target": "end",
                                     "source_port": "text", "target_port": "input"}}),
        ("test_add", {"test": {
            "id": "daily-report-numbers", "name": "数字锚定验收",
            "requirement": "样例数据算出 A店2000/B店3000/总计5000",
            "inputs": {"sales": [
                {"store": "A店", "amount": 1200},
                {"store": "A店", "amount": 800},
                {"store": "B店", "amount": 3000},
            ]},
            "assertions": [
                {"path": ["by_store", "A店"], "operator": "equals", "expected": 2000},
                {"path": ["by_store", "B店"], "operator": "equals", "expected": 3000},
                {"path": ["total"], "operator": "equals", "expected": 5000},
                {"path": ["report"], "operator": "contains", "expected": "5000"},
            ],
            "mandatory": True,
        }}),
        ("test_run", {}),
    ]

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        name, value = self.OPERATIONS[min(self.calls, len(self.OPERATIONS) - 1)]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": f"ref{self.calls}", "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


def test_daily_report_benchmark_has_reference_solution(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ReferenceSolutionScript())
    headers = {"Authorization": "Bearer workflow-test"}
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "参照解", "requirement": "门店销售日报汇总参照解。"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入门店销售流水 sales，输出 by_store/total/report。",
                  "auto_publish": True, "max_turns": 16, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(800):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)
        assert build["status"] == "published", build.get("error")

        # 照妖镜：未见门店的独立运行必须算对（假成功事故的判据）
        run = client.post(f"/api/v1/applications/{app_id}/runs", headers=headers,
                          json={"inputs": {"sales": [
                              {"store": "C店", "amount": 500},
                              {"store": "D店", "amount": 700},
                          ]}}).json()
        for _ in range(800):
            result = client.get(f"/api/v1/runs/{run['run_id']}", headers=headers).json()
            if result.get("status") in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert result["status"] == "succeeded", result.get("error")
        outputs = result["outputs"]
        assert outputs["by_store"] == {"C店": 500, "D店": 700}
        assert outputs["total"] == 1200
        assert "1200" in outputs["report"]


def test_unknown_assignment_operator_is_rejected_with_guidance() -> None:
    """拒绝即教学（A 方案 4.2）：32B 实测发明了 $sum_by 并与正确的 $formula
    写在同一个赋值对象里；运行时取第一个键就跑偏，报出"collection expression
    requires an array"这种风马牛的错误，模型完全无从修起。"""

    import pytest
    from agent_platform.workflow_runtime import WorkflowRuntime

    context = {"nodes": {}, "inputs": {}}
    with pytest.raises(ValueError, match="未知的赋值操作符"):
        WorkflowRuntime._resolve_assignment(
            {"$sum_by": {"collection": [], "key": "store", "value": "amount"}}, context
        )
    # 错误消息要给出正确写法，而不只是说"不认识"
    try:
        WorkflowRuntime._resolve_assignment({"$group_sum": {}}, context)
    except ValueError as error:
        assert "$formula" in str(error) and "sum_by" in str(error)

    with pytest.raises(ValueError, match="只能有一个操作符"):
        WorkflowRuntime._resolve_assignment(
            {"$sum": [1, 2], "$formula": "1+1"}, context
        )


def test_bare_operator_or_function_key_is_rejected_not_silently_stored() -> None:
    """漏 $ 或拿公式函数名当键：此前**完全不报错**，字段里静默存下一份没求值的
    表达式——形状合法的垃圾，一路走到数字断言才炸，报错还与真因无关。

    实测 4B（构建 b44d3594）写出 {"sum_by": ["sales","store","amount"]}。
    """

    import pytest
    from agent_platform.workflow_runtime import WorkflowRuntime

    context = {"nodes": {}, "inputs": {}}

    # 公式函数名当键：必须拒绝，并给出 $formula 的正确写法
    with pytest.raises(ValueError) as excinfo:
        WorkflowRuntime._resolve_assignment(
            {"sum_by": ["sales", "store", "amount"]}, context
        )
    message = str(excinfo.value)
    assert "公式函数" in message and "$formula" in message

    # 漏了 $ 前缀：必须点名该写成什么
    with pytest.raises(ValueError, match=r"要带 \$ 前缀"):
        WorkflowRuntime._resolve_assignment({"sum": [1, 2]}, context)

    # 普通字典照常放行（这条门只针对"看起来像操作符/函数"的单键）
    assert WorkflowRuntime._resolve_assignment({"store": "A店"}, context) == {"store": "A店"}


def test_failed_reference_lists_available_paths() -> None:
    """同一个坑（variable_assigner 的产出包在 output 下）把 32B 与人类作者
    双双绊倒——解析失败必须告诉调用者该节点真实可用的路径。"""

    import pytest
    from agent_platform.workflow_runtime import (
        WorkflowRuntime, WorkflowReferenceResolutionError,
    )

    context = {
        "nodes": {"calc": {"output": {"by_store": {"A店": 2000}, "total": 5000}}},
        "inputs": {},
    }
    with pytest.raises(WorkflowReferenceResolutionError) as excinfo:
        WorkflowRuntime._resolve(
            {"$ref": {"node_id": "calc", "path": ["by_store"]}}, context
        )
    message = str(excinfo.value)
    assert "该节点可用路径" in message
    # 提示必须用**真实输入语法**（分段数组）。此前用点号显示 output.by_store，
    # 模型照抄成 path: ["output.by_store"]，永远解析不了——平台自己教错了语法
    # （真机构建 881d90a6 因此耗尽 4 轮修复）。
    assert '["output", "by_store"]' in message
    assert "output.by_store" not in message

    # 该修哪一端要由平台判出来，不能把两条路并列丢给模型：
    #   近亲路径 → 多半是引用写错了
    with pytest.raises(WorkflowReferenceResolutionError) as near_error:
        WorkflowRuntime._resolve(
            {"$ref": {"node_id": "calc", "path": ["output", "by_stores"]}}, context
        )
    assert "多半是这里的引用写错了" in str(near_error.value)

    #   八竿子打不着 → 被引用节点确实没产出这个字段
    with pytest.raises(WorkflowReferenceResolutionError) as far_error:
        WorkflowRuntime._resolve(
            {"$ref": {"node_id": "calc", "path": ["completely_unrelated"]}}, context
        )
    assert "根本没产出这个字段" in str(far_error.value)


def test_dotted_path_written_as_one_segment_still_resolves() -> None:
    """["output.by_store"] 写成一个段：意图无歧义，解析时先按拆分重试。

    这个错法是**平台自己教的**——错误提示里的"可用路径"曾用点号显示，
    模型照抄成一个段。提示已改成分段语法，容忍也留着：老草稿照样能跑。
    """
    from agent_platform.workflow_runtime import WorkflowRuntime

    context = {
        "nodes": {"calc": {"output": {"by_store": {"A店": 2000}, "total": 5000}}},
        "inputs": {},
    }
    assert WorkflowRuntime._resolve(
        {"$ref": {"node_id": "calc", "path": ["output.by_store"]}}, context
    ) == {"A店": 2000}
    assert WorkflowRuntime._resolve(
        {"$ref": {"node_id": "calc", "path": ["output", "total"]}}, context
    ) == 5000


def test_node_not_found_lists_existing_ids() -> None:
    """拒绝即教学：node not found 必须说出现有 id 与最接近的候选。

    实测（日报基准 classic 单）：协调者反复对不存在的 'start' 做 remove/connect，
    每次只被告知 "node not found"，于是删了又加空转到 200 次工具预算耗尽。
    """

    import pytest
    from agent_platform.applications import ApplicationService
    from agent_platform.workflow_models import ApplicationSnapshot, WorkflowSpec, NodeSpec

    snapshot = ApplicationSnapshot(
        name="t", description="t", requirement="t",
        workflow=WorkflowSpec(nodes=[
            NodeSpec(id="start_node", type="start", title="输入", config={"inputs": []}),
            NodeSpec(id="workflow_end", type="end", title="输出", config={"outputs": {}}),
        ], edges=[]),
    )
    with pytest.raises(KeyError) as excinfo:
        ApplicationService._node(snapshot, "start")
    message = str(excinfo.value)
    assert "现有节点 id" in message and "start_node" in message
    assert "最接近的是" in message  # start → start_node 的模糊匹配


def test_self_reference_and_mixed_operator_are_rejected() -> None:
    """两个静默失败的洞（第四轮日报基准实测暴露）：

    ① 节点引用自身产出：draft_validate 全绿、运行期才炸；
    ② 操作符与普通键混写 {"$formula": {...}, "output_type": "object"}：
       长度不为 1 就当普通字典解析，公式根本不执行、字段里塞着未求值的公式
       ——形状合法的垃圾。
    """

    import pytest
    from agent_platform.workflow_runtime import WorkflowRuntime

    with pytest.raises(ValueError, match="不能和其它键混在同一个对象里"):
        WorkflowRuntime._resolve_assignment(
            {"$formula": "1+1", "output_type": "number"}, {"nodes": {}, "inputs": {}}
        )


def test_self_reference_rejected_by_validation(tmp_path: Path) -> None:
    from agent_platform.applications import ApplicationService

    snapshot_config = {"assignments": {
        "echo": {"$ref": {"node_id": "calc", "path": ["by_store"]}},
    }}
    from agent_platform.workflow_models import ApplicationSnapshot, WorkflowSpec, NodeSpec
    snapshot = ApplicationSnapshot(
        name="t", description="t", requirement="t",
        workflow=WorkflowSpec(nodes=[
            NodeSpec(id="calc", type="variable_assigner", title="计算", config=snapshot_config),
        ], edges=[]),
    )
    # 直接调用内部校验逻辑的等价物：找出自引用
    errors = []
    for node in snapshot.workflow.nodes:
        payload = json.dumps(node.config, ensure_ascii=False)
        if f'"node_id": "{node.id}"' in payload:
            errors.append(node.id)
    assert errors == ["calc"], "自引用必须可被检出"


def test_json_string_instead_of_array_is_named_as_such() -> None:
    """把数组写成带引号的 JSON 字面量时，报错要点破，别只说"需要一个对象数组"。

    真机构建 13284038：工作流完全正确，是**验收测试的 inputs** 把 sales 写成了
    字符串（"sales": "[{...}]"），报出来只有"需要一个对象数组"，4 轮修复全花在
    修一个没坏的工作流上。症状要指向真因。
    """
    import pytest
    from agent_platform.formula import evaluate_formula

    with pytest.raises(Exception) as error:
        evaluate_formula(
            'sum_by(sales, "store", "amount")',
            {"sales": '[{"store":"A店","amount":1200}]'},
        )
    message = str(error.value)
    assert "JSON 字符串" in message
    assert "去掉外层引号" in message

    # 普通字符串不该被误报成 JSON
    with pytest.raises(Exception) as plain:
        evaluate_formula('sum_by(sales, "store", "amount")', {"sales": "A店"})
    assert "JSON 字符串" not in str(plain.value)
