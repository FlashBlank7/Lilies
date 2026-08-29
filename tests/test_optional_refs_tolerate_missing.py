"""`optional` 的引用取不到值时要给 None，而不是把整条运行判失败。

变异验证（2026-08-29）：把两处 `if reference.get("optional"): return None`
都改成不生效，**全套 1191 条测试全绿**——这个行为一直没人测。

它管的正是这台机器上最大的一族失败：
`could not resolve node=... path=[...]`（真机 72 条）。
optional 存在的意义就是让"这一项本来就可能没有"的引用不必炸掉整条运行；
它坏了，那些工作流会从"少一个字段"变成"整条跑不完"，
而报错长得跟真的缺字段一模一样，排查时根本分不出来。

两处 optional 是**两个不同的情况**，各要一条：
· 整个节点都不在（上游被跳过 / 分支没走到）
· 节点在，但里面没有这一段路径
"""

from __future__ import annotations

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime


def _ref(node_id: str, path: list, optional: bool = False) -> dict:
    value = {"$ref": {"node_id": node_id, "path": path}}
    if optional:
        value["optional"] = True
    return value


CONTEXT = {
    "inputs": {"text": "一二三"},
    "run": {"run_id": "r-1"},
    "nodes": {"calc": {"output": {"line_count": 3}}},
}


def test_a_present_reference_resolves():
    """先确认这套上下文本身是通的——否则下面全是空断言。"""
    assert WorkflowRuntime._resolve(_ref("calc", ["output", "line_count"]),
                                    CONTEXT) == 3


def test_an_optional_reference_to_a_missing_node_gives_none():
    """整个节点都不在：上游被跳过、或分支没走到。

    实现里这一支是**快路径**，不是独立行为：把它注释掉，
    `context["nodes"][node_id]` 抛的 KeyError 会被外层 except 接住，
    再走一次 optional 判断，结果一样。
    变异验证时这条"逃"掉过——那是等价变异，不是测试的空档；
    记在这里免得下次再追一遍。
    """
    assert WorkflowRuntime._resolve(
        _ref("从来没跑过的节点", ["output"], optional=True), CONTEXT) is None


def test_an_optional_reference_to_a_missing_path_gives_none():
    """节点在，但里面没有这一段——和上一条是两个不同的分支。"""
    assert WorkflowRuntime._resolve(
        _ref("calc", ["output", "根本没有这一项"], optional=True), CONTEXT) is None


def test_an_optional_reference_that_does_resolve_still_returns_the_value():
    """optional 不是"永远给 None"——有值的时候要把值给出来。

    少了这一条的话，把 optional 分支改成"无条件 return None"也能全绿。
    """
    assert WorkflowRuntime._resolve(
        _ref("calc", ["output", "line_count"], optional=True), CONTEXT) == 3


def test_a_required_reference_to_a_missing_node_still_fails():
    """别把闸关死：没标 optional 的引用取不到值，就该炸。

    没有这一条的话，"一律返回 None"也能让上面几条全绿——
    而那意味着所有拼错的引用都会静悄悄变成 None，
    工作流照跑，结果是错的。
    """
    with pytest.raises(Exception) as caught:
        WorkflowRuntime._resolve(_ref("从来没跑过的节点", ["output"]), CONTEXT)
    assert "could not resolve" in str(caught.value)


def test_a_required_reference_to_a_missing_path_still_fails():
    with pytest.raises(Exception) as caught:
        WorkflowRuntime._resolve(_ref("calc", ["output", "没这一项"]), CONTEXT)
    assert "could not resolve" in str(caught.value)


def test_the_failure_still_teaches_which_paths_exist():
    """报错要摆出该节点真实可用的路径——只说"解析不了"等于让人猜。

    而且提示要用**分段数组**写法：此前用点号显示（output.line_count），
    模型照抄成一个段，永远解析不了（真机构建 881d90a6 为此耗掉 4 轮修复）。
    """
    with pytest.raises(Exception) as caught:
        WorkflowRuntime._resolve(_ref("calc", ["output", "没这一项"]), CONTEXT)
    message = str(caught.value)
    assert "可用路径" in message
    assert '["output", "line_count"]' in message, message
