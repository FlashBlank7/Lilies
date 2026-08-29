from __future__ import annotations

from typing import Any

from pathlib import Path

import pytest
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.config import Settings
from agent_platform.workflow_models import EdgeSpec, NodeSpec
from tests.test_runtime import ScriptedProvider


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer workflow-edge-test",
        "Content-Type": "application/json",
    }


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    op: str,
    data: dict[str, object],
):
    return client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=_headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )


def test_block_registry_validates_named_and_typed_incremental_ports() -> None:
    registry = build_block_registry()
    llm = NodeSpec(id="llm", type="llm", title="LLM", config={"prompt": "hello"})
    iteration = NodeSpec(
        id="iteration",
        type="iteration",
        title="Iteration",
        config={},
    )
    end = NodeSpec(id="end", type="end", title="End", config={"outputs": {}})

    missing = registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="bad-name",
            source="iteration",
            target="end",
            source_port="bogus",
            target_port="input",
        ),
    )
    # 拒绝必须带上可用端口清单：只报"不存在"等于让构建者去猜（真机构建
    # b44d3594 里 4B 把配置字段名当端口，连撞 4 次同一条错误被判停）。
    assert len(missing) == 1
    assert missing[0].startswith("bad-name: unknown source port iteration.bogus")
    assert "可用输出端口：items" in missing[0]

    named_target = registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="bad-target",
            source="iteration",
            target="end",
            source_port="items",
            target_port="variables",
        ),
    )
    assert len(named_target) == 1
    assert "可用输入端口：" in named_target[0]
    # 端口不是配置字段名——这正是 4B 撞死的那个误解，拒绝里要直接点破
    assert "端口不是配置字段名" in named_target[0]

    # The default port name resolves to the block's primary output port.
    defaulted = registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="default-name",
            source="iteration",
            target="end",
            source_port="output",
            target_port="input",
        ),
    )
    assert defaulted == []

    incompatible = registry.validate_edge(
        llm,
        iteration,
        EdgeSpec(
            id="bad-type",
            source="llm",
            target="iteration",
            source_port="text",
            target_port="input",
        ),
    )
    assert incompatible == ["bad-type: incompatible ports string -> array"]

    assert registry.validate_edge(
        iteration,
        end,
        EdgeSpec(
            id="valid",
            source="iteration",
            target="end",
            source_port="items",
            target_port="input",
        ),
    ) == []


def test_add_edge_rejects_bad_port_before_persisting_it(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-edge-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=_headers(),
            json={"name": "Named ports", "requirement": "Check named workflow ports."},
        ).json()["id"]
        llm = _mutate(
            client,
            application_id,
            0,
            "add_node",
            {
                "node": {
                    "id": "llm",
                    "type": "llm",
                    "title": "LLM",
                    "config": {"prompt": "hello"},
                }
            },
        )
        assert llm.status_code == 200, llm.text
        end = _mutate(
            client,
            application_id,
            1,
            "add_node",
            {
                "node": {
                    "id": "end",
                    "type": "end",
                    "title": "End",
                    "config": {"outputs": {}},
                }
            },
        )
        assert end.status_code == 200, end.text

        invalid = _mutate(
            client,
            application_id,
            2,
            "add_edge",
            {
                "edge": {
                    "id": "llm-end",
                    "source": "llm",
                    "target": "end",
                    "source_port": "bogus",
                    "target_port": "input",
                }
            },
        )
        assert invalid.status_code == 422
        assert "unknown source port llm.bogus" in invalid.text
        unchanged = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=_headers(),
        ).json()
        assert unchanged["revision"] == 2
        assert unchanged["snapshot"]["workflow"]["edges"] == []

        valid = _mutate(
            client,
            application_id,
            2,
            "add_edge",
            {
                "edge": {
                    "id": "llm-end",
                    "source": "llm",
                    "target": "end",
                    "source_port": "text",
                    "target_port": "input",
                }
            },
        )
        assert valid.status_code == 200, valid.text
        assert valid.json()["revision"] == 3


def test_config_validation_reports_the_element_not_just_the_field() -> None:
    """元素级的错误不能报成字段级的，否则模型看不懂。

    真机构建 3e8158a3：record_paths 收到 ["store","amount"]（它要的是
    [["store"],["amount"]]），报出来却是"候选记录路径：Input should be a valid list"
    ——模型明明传了一个 list，却被告知"不是 list"，原样重试 7 次被判停。
    位置（第几项）和收到的值都要报出来。
    """
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_models import NodeSpec

    registry = build_block_registry()
    with pytest.raises(ValueError) as error:
        registry.validate_node(NodeSpec(
            id="n", type="record_collection_normalize", title="t",
            config={
                "value": {"$ref": {"node_id": "start", "path": ["sales"]}},
                "record_paths": ["store", "amount"],
            },
        ))
    message = str(error.value)
    assert "record_paths" in message
    assert "[0]" in message, message          # 第几项
    assert "'store'" in message, message      # 收到的值


def test_duplicate_edge_rejection_lists_what_is_wired_and_what_is_missing(tmp_path: Path) -> None:
    """连线类拒绝必须报出"现在接了什么、还差什么"。

    真机构建 310141fd：4B 对同一条已存在的边连提 9 次被反刍守卫判停——它每轮
    拿到的信息里根本没有边的清单，只能猜下一条该连哪。缺口是从图上机械算得出来的。
    """
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "Wiring", "requirement": "接线提示"}).json()["id"]

        def op(payload: dict) -> Any:
            draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers).json()
            return client.post(
                f"/api/v1/applications/{app_id}/draft",
                headers=headers,
                json={**payload, "expected_revision": draft["revision"],
                      "idempotency_key": uuid4().hex},
            )

        for node in (
            {"id": "start", "type": "start", "title": "In",
             "config": {"inputs": [{"name": "name", "type": "string"}]}},
            {"id": "mid", "type": "template_transform", "title": "Mid",
             "config": {"template": "hi {{ name }}", "variables": {
                 "name": {"$ref": {"node_id": "start", "path": ["name"]}}}}},
            {"id": "end", "type": "end", "title": "Out",
             "config": {"outputs": {"greeting": {"$ref": {
                 "node_id": "mid", "path": ["text"]}}}}},
        ):
            assert op({"op": "add_node", "data": {"node": node}}).status_code < 300

        assert op({"op": "add_edge", "data": {"edge": {
            "id": "e1", "source": "start", "target": "mid",
            "source_port": "output", "target_port": "input"}}}).status_code < 300

        duplicate = op({"op": "add_edge", "data": {"edge": {
            "id": "e1", "source": "start", "target": "mid",
            "source_port": "output", "target_port": "input"}}})
        detail = duplicate.json().get("detail", "")
        # 2026-08-29：拒绝主干改成中文（真机上模型对同一条边连提 9 次，
        # 拿到的却是一句英文）。断言跟着改成意思。
        assert "这条连线已经有了" in detail
        assert "start→mid" in detail                 # 已接的
        assert "mid" in detail and "end" in detail   # 还缺的
        assert "还缺" in detail or "还没有" in detail


def test_string_shaped_reference_and_formula_are_rejected_with_the_real_syntax() -> None:
    """写成字符串的引用/公式必须当场拒，不能静默存成字面量。

    真机构建 93430e0b 里 4B 写出 {"value": "$ref{start, sales}"} 与
    {"assignments": {"by_store": "group_by_sum(...)"}}——两条都结构合法，被原样
    存下，工作流照跑，输出就是那串字面量。这正是本项目要消灭的"形状合法的垃圾"：
    模型的意图毫无歧义，平台没有理由装看不见。
    """
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_models import NodeSpec

    registry = build_block_registry()

    with pytest.raises(ValueError) as ref_error:
        registry.validate_node(NodeSpec(
            id="n", type="template_transform", title="t",
            config={"template": "x", "variables": {"v": "$ref{start, sales}"}},
        ))
    assert "引用不是字符串语法" in str(ref_error.value)
    assert '"$ref"' in str(ref_error.value)      # 报出正确写法

    # 模型发明语法的花样不止一种，逐个补正则永远慢一步：三轮真机各写出一种
    for shape in ('$ref:{"node_id":"start","path":["sales"]}', "$ref = start.sales"):
        with pytest.raises(ValueError, match="引用不是字符串语法"):
            registry.validate_node(NodeSpec(
                id="n", type="template_transform", title="t",
                config={"template": "x", "variables": {"v": shape}},
            ))
    # $操作符 当字符串前缀写（1334c391：$formula.sum_by(...) 被静默存成字面量）
    for shape in ('$formula.sum_by(sales, "store", "amount")', "$formula: sum(x)"):
        with pytest.raises(ValueError, match="是对象里的"):
            registry.validate_node(NodeSpec(
                id="n", type="variable_assigner", title="t",
                config={"assignments": {"x": shape}},
            ))
    # 含 $ 的普通文本不受牵连
    registry.validate_node(NodeSpec(
        id="ok0", type="variable_assigner", title="t",
        config={"assignments": {"note": "单价 $100", "store": "A店"}},
    ))

    with pytest.raises(ValueError) as formula_error:
        registry.validate_node(NodeSpec(
            id="n", type="variable_assigner", title="t",
            config={"assignments": {"by_store": 'group_by_sum(x, "store", "amount")'}},
        ))
    assert "公式不是字符串语法" in str(formula_error.value)
    assert "$formula" in str(formula_error.value)

    # 值槽里填**类型名**同样是静默垃圾：工作流会原样输出字符串 "object"
    # （真机构建 e28708d3 的 end 节点就是这么写的）
    with pytest.raises(ValueError) as type_error:
        registry.validate_node(NodeSpec(
            id="n", type="end", title="t",
            config={"outputs": {"by_store": "object", "total": "number"}},
        ))
    assert "填的是类型名" in str(type_error.value)

    # 正常写法一律放行：模板文本、真正的 $ref、字面量常数
    registry.validate_node(NodeSpec(
        id="ok", type="template_transform", title="t",
        config={"template": "各门店合计：{{ by_store }}（单位：元）", "variables": {}},
    ))
    registry.validate_node(NodeSpec(
        id="ok2", type="end", title="t",
        config={"outputs": {"g": {"$ref": {"node_id": "a", "path": ["x"]}}}},
    ))
    registry.validate_node(NodeSpec(
        id="ok3", type="variable_assigner", title="t",
        config={"assignments": {"rate": 0.08, "store": "A店"}},
    ))


def test_single_brace_template_placeholder_is_rejected() -> None:
    """模板里用单花括号写占位符——变量永远不会被替换。

    真机构建 a6284ec0 是形态 B 第一次 published：数字全算对了
    （by_store={辛店:210,己店:230}、total=440），report 却是模板原文
    "各门店销售额合计：{by_store}，总销售额：{total}"。模板渲染认的是
    {{ 变量名 }}，单花括号原样输出——数字对、报表废，正是"形状合法的垃圾"。
    """
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_models import NodeSpec

    registry = build_block_registry()

    with pytest.raises(ValueError) as error:
        registry.validate_node(NodeSpec(
            id="report", type="template_transform", title="日报",
            config={
                "template": "各门店销售额合计：{by_store}，总销售额：{total}",
                "variables": {"by_store": {}, "total": {}},
            },
        ))
    message = str(error.value)
    assert "单花括号" in message
    assert "{{ by_store }}" in message

    # 双花括号正常放行
    registry.validate_node(NodeSpec(
        id="ok", type="template_transform", title="日报",
        config={"template": "合计：{{ by_store }}", "variables": {"by_store": {}}},
    ))
    # 模板正文里本来就有的花括号（没声明成变量）不受牵连
    registry.validate_node(NodeSpec(
        id="ok2", type="template_transform", title="说明",
        config={"template": 'JSON 形如 {"a": 1}', "variables": {"x": {}}},
    ))
