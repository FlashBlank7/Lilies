"""工作流套工作流：不许成环、不许套太深、不许套没授权的那个。

三道闸都在 create_run 的最前面，而 `NestedWorkflowCycleDenied` /
`NestedWorkflowDepthExceeded` / `NestedWorkflowScopeDenied` 这三个异常名
在整个 tests/ 里一次都没出现过（2026-08-29 用 ast 扫的）。

为什么这三道值得单独钉：环和深度拦的是**无限递归**，
而每一层嵌套都要跑模型、都要花钱。闸坏了不是"结果不对"，
是一次调用把预算烧穿。

调用链是从父运行传下来的（state.application_call_chain），
所以这里直接把链喂进去——闸就在函数头几行，不碰后面任何东西。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_platform.workflow_models import WorkflowRunRequest
from agent_platform.workflow_runtime import (
    MAX_NESTED_WORKFLOW_DEPTH,
    NestedWorkflowCycleDenied,
    NestedWorkflowDepthExceeded,
    NestedWorkflowScopeDenied,
    WorkflowRuntime,
)


class _Boom(RuntimeError):
    """闸都过了才会走到这儿——用它证明"确实走过头了"。"""


@pytest.fixture
def runtime(tmp_path: Path) -> WorkflowRuntime:
    async def get_draft(*_, **__):
        raise _Boom("闸没拦住，已经开始取草稿了")

    async def get_application(*_, **__):
        raise _Boom("闸没拦住，已经开始取应用了")

    store = SimpleNamespace(get_draft=get_draft, get_application=get_application)
    # 构造函数的必填项全给桩：这三道闸在 create_run 的头几行，
    # 谁都用不到。用真依赖反而会让"闸没拦住"变成别的报错，
    # 遮住真正要看的那一条。
    return WorkflowRuntime(
        storage=SimpleNamespace(), workflow_store=store,
        harness=SimpleNamespace(worker_id="w1"), applications=SimpleNamespace(),
        blocks=SimpleNamespace(), provider=SimpleNamespace(),
        agent_runtime=SimpleNamespace(), tools=SimpleNamespace(),
        sandboxes=SimpleNamespace(), runtime_model="m",
    )


def _run(runtime, *, app_id: str, chain: list[str]):
    return asyncio.run(runtime.create_run(
        app_id, WorkflowRunRequest(inputs={}, use_draft=True),
        application_call_chain=chain))


class TestACycleIsRefused:
    def test_a_workflow_cannot_call_itself(self, runtime):
        with pytest.raises(NestedWorkflowCycleDenied):
            _run(runtime, app_id="a", chain=["a"])

    def test_a_longer_loop_is_caught_too(self, runtime):
        """a → b → c → a：不是直接自环，照样是环。"""
        with pytest.raises(NestedWorkflowCycleDenied):
            _run(runtime, app_id="a", chain=["a", "b", "c"])

    def test_a_chain_without_a_cycle_gets_past_this_gate(self, runtime):
        """反向那一条：没成环就不该拦。

        走到 _Boom 说明它过了闸、开始干正事了——
        少了这条，"一律拒绝"也能让上面两条全绿。
        """
        with pytest.raises(_Boom):
            _run(runtime, app_id="d", chain=["a", "b", "c"])


class TestDepthHasAFloorAndACeiling:
    def test_the_limit_is_a_sane_number(self):
        """绝对值也钉一下：只写"到了 MAX 就拒"这种从常量推出来的断言，
        常量被人乘以 100 时照样绿（今天在 formula / table_intake 上踩过）。"""
        assert 4 <= MAX_NESTED_WORKFLOW_DEPTH <= 64

    def test_one_layer_below_the_limit_still_runs(self, runtime):
        chain = [f"app-{i}" for i in range(MAX_NESTED_WORKFLOW_DEPTH - 1)]
        with pytest.raises(_Boom):
            _run(runtime, app_id="fresh", chain=chain)

    def test_at_the_limit_it_is_refused(self, runtime):
        chain = [f"app-{i}" for i in range(MAX_NESTED_WORKFLOW_DEPTH)]
        with pytest.raises(NestedWorkflowDepthExceeded):
            _run(runtime, app_id="fresh", chain=chain)

    def test_way_past_the_limit_is_refused(self, runtime):
        chain = [f"app-{i}" for i in range(MAX_NESTED_WORKFLOW_DEPTH * 3)]
        with pytest.raises(NestedWorkflowDepthExceeded):
            _run(runtime, app_id="fresh", chain=chain)


class TestOnlyAllowedApplicationsCanBeNested:
    """这一道是授权，不是防递归——名单来自发布时定下的执行策略。"""

    @staticmethod
    def _check(tool: str, allowed):
        # 方法名从源码核过（是 _validate_nested_workflow_target，
        # 不是我顺手写的那个名字）——今天已经因为猜名字红过两次了。
        WorkflowRuntime._validate_nested_workflow_target(tool, allowed)

    def test_an_application_outside_the_list_is_refused(self):
        with pytest.raises(NestedWorkflowScopeDenied):
            self._check("workflow:evil", frozenset({"good"}))

    def test_an_application_in_the_list_passes(self):
        self._check("workflow:good", frozenset({"good"}))

    def test_an_empty_list_lets_nothing_through(self):
        """空名单是"一个都不许"，不是"随便"——这是 fail-closed 的方向。"""
        with pytest.raises(NestedWorkflowScopeDenied):
            self._check("workflow:anything", frozenset())

    def test_no_list_at_all_means_the_policy_does_not_apply(self):
        """None 和空集合是两件事：None 表示这条运行没有嵌套白名单这项约束。"""
        self._check("workflow:anything", None)

    def test_a_plain_tool_is_not_touched(self):
        """非 workflow: 前缀的工具跟这道闸无关，别误伤。"""
        self._check("Read", frozenset({"good"}))
