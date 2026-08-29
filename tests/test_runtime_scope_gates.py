"""运行时的几道范围闸：工具、连接器操作、写次数、载荷大小。

execution_policy 管的是"这次运行**准**干什么"（今天已经补了收窄不许放宽的测试）；
这个文件管的是另一半——**执行时真去比对那份名单**的地方。
两半缺一不可：名单再严，执行时不查也白搭。

来由：用 ast 扫"tests 里从没出现过的异常类"，
WorkflowRuntime*ScopeDenied / *LimitExceeded 一整族九个，
在整个 tests/ 里一次都没被提过。

这里挑的是纯函数式的那几道（不需要起运行时）：
  · _validate_runtime_tool_target      工具在不在名单里
  · _resolve_connector_operation       连接器操作在不在名单里、有没有歧义
每一条都配反向，否则"一律拒"也能全绿。
"""

from __future__ import annotations

import pytest

from agent_platform.workflow_runtime import (
    WorkflowRuntime,
    WorkflowRuntimeConnectorScopeDenied,
    WorkflowRuntimeToolScopeDenied,
)


class TestRuntimeToolsAreCheckedAgainstTheList:
    @staticmethod
    def _check(tool: str, allowed):
        WorkflowRuntime._validate_runtime_tool_target(tool, allowed)

    def test_a_tool_outside_the_list_is_refused(self):
        with pytest.raises(WorkflowRuntimeToolScopeDenied):
            self._check("Bash", frozenset({"Read"}))

    def test_a_tool_in_the_list_passes(self):
        self._check("Read", frozenset({"Read"}))

    def test_an_empty_list_lets_nothing_through(self):
        """空名单是"一个都不许"。写成 `not allowed` 判空的话就成了"随便"。"""
        with pytest.raises(WorkflowRuntimeToolScopeDenied):
            self._check("Read", frozenset())

    def test_no_list_means_this_gate_does_not_apply(self):
        """None 和空集合是两件事：None 表示这条运行没有工具白名单这项约束。"""
        self._check("Bash", None)

    def test_nested_workflow_targets_go_through_the_other_gate(self):
        """workflow: 前缀由 _validate_nested_workflow_target 管，这里别重复拦。

        少了这条，"顺手把 workflow: 也塞进工具名单里比一遍"看着也对，
        实际会把所有嵌套调用拦死。
        """
        self._check("workflow:some-app", frozenset({"Read"}))


class TestConnectorOperationsMustMatchExactlyOne:
    @staticmethod
    def _resolve(connector: str, operation: str, allowed):
        # 这个模型在 blocks 里，不在 workflow_models——名字和位置都从源码核过
        from agent_platform.blocks import ConnectorActionConfig

        # 必填项一个不少地给全（源码里 tenant_id / actor_id 这几个都是必填），
        # 这道闸只看 connector_id + operation_id，其余给占位值即可
        config = ConnectorActionConfig(
            connector_id=connector, operation_id=operation,
            tenant_id="t", actor_id="a", actor_roles=[], profile_id="p",
            payload={}, idempotency_key="k")
        return WorkflowRuntime._resolve_connector_operation(config, allowed)

    def test_the_canonical_name_is_accepted(self):
        assert self._resolve("crm", "create", frozenset({"crm.create"})) == "crm.create"

    def test_a_bare_operation_id_is_accepted(self):
        """名单里可能只写了操作名——三种写法都认，但只能命中一种。"""
        assert self._resolve("crm", "create", frozenset({"create"})) == "create"

    def test_an_operation_outside_the_list_is_refused(self):
        with pytest.raises(WorkflowRuntimeConnectorScopeDenied):
            self._resolve("crm", "delete", frozenset({"crm.create"}))

    def test_an_ambiguous_match_is_refused(self):
        """名单里同时写了两种写法——**含糊也要拒**，不能挑一个用。

        这是 fail-closed 的方向：两种写法可能对应不同的授权意图，
        猜错一次就是越权。
        """
        with pytest.raises(WorkflowRuntimeConnectorScopeDenied):
            self._resolve("crm", "create", frozenset({"create", "crm.create"}))

    def test_an_empty_list_lets_nothing_through(self):
        with pytest.raises(WorkflowRuntimeConnectorScopeDenied):
            self._resolve("crm", "create", frozenset())

    def test_no_list_means_this_gate_does_not_apply(self):
        assert self._resolve("crm", "create", None) == "crm.create"
