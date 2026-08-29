"""整张图的环路检查——这是第二道防线，而它一直没有测试。

变异验证（2026-08-29）：把 validate_workflow 里那句
「拓扑排序走不完 → 图里有环」关掉，**全套 1186 条测试全绿**。

第一道防线是 add_edge 时逐条查（今天刚补上测试）。
但整图校验是**另一条路**上唯一的那道闸：
· replace_workflow 一次换掉整张图，不走 add_edge；
· 发布前校验草稿；
· 导入快照。
这几条路上没有第二个人查环。

成环的后果不是"图不好看"：执行照着边走，会绕回去。
平台为此专门提供了「循环」积木（有次数上限、有退出条件）。
"""

from __future__ import annotations

from agent_platform.blocks import build_block_registry
from agent_platform.workflow_models import EdgeSpec, NodeSpec, WorkflowSpec


def _assigner(node_id: str) -> NodeSpec:
    """两侧都有端口的积木——这样环边不会被端口校验提前拒掉，
    测到的才是环路检查本身。"""
    return NodeSpec(id=node_id, type="variable_assigner", title=node_id, config={})


def _edge(source: str, target: str) -> EdgeSpec:
    return EdgeSpec(id=f"{source}-{target}", source=source, target=target,
                    source_port="output", target_port="input")


def _graph(edges: list[tuple[str, str]], extra_nodes: list[str] = ()) -> WorkflowSpec:
    nodes = [NodeSpec(id="start", type="start", title="开始",
                      config={"inputs": [{"name": "text", "type": "string",
                                          "required": False}]}),
             NodeSpec(id="end", type="end", title="结束", config={"outputs": {}})]
    nodes += [_assigner(name) for name in extra_nodes]
    return WorkflowSpec(nodes=nodes, edges=[_edge(s, t) for s, t in edges])


def _errors(workflow: WorkflowSpec) -> str:
    return " ".join(build_block_registry().validate_workflow(workflow))


def test_a_two_node_cycle_is_reported():
    """a → b → a：拓扑排序走不完，必须报出来。"""
    workflow = _graph([("start", "a"), ("a", "b"), ("b", "a"), ("b", "end")],
                      extra_nodes=["a", "b"])
    assert "cycle" in _errors(workflow)


def test_a_three_node_cycle_is_reported():
    """绕两步的环——只查"直接互指"的实现会放它过去。"""
    workflow = _graph([("start", "a"), ("a", "b"), ("b", "c"), ("c", "a"),
                       ("c", "end")],
                      extra_nodes=["a", "b", "c"])
    assert "cycle" in _errors(workflow)


def test_a_straight_line_is_not_reported():
    """别把闸关死：直链要判合法。

    没有这一条的话，「一律报环」也能让上面两条全绿。
    """
    workflow = _graph([("start", "a"), ("a", "b"), ("b", "end")],
                      extra_nodes=["a", "b"])
    assert "cycle" not in _errors(workflow)


def test_a_diamond_is_not_a_cycle():
    """分叉再合流不是环——最容易被写错的一种误报。"""
    workflow = _graph([("start", "a"), ("a", "b"), ("a", "c"),
                       ("b", "end"), ("c", "end")],
                      extra_nodes=["a", "b", "c"])
    assert "cycle" not in _errors(workflow)


def test_the_message_points_at_the_loop_brick():
    """报错要说清"那该怎么办"——平台是有循环积木的。"""
    workflow = _graph([("start", "a"), ("a", "b"), ("b", "a"), ("b", "end")],
                      extra_nodes=["a", "b"])
    assert "loop block" in _errors(workflow)
