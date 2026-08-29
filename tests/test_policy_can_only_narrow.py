"""发布时定下的执行权限，之后只能越收越紧，不能被调用方放宽。

execution_policy 318 行，被 workflow_storage 和 workflow_runtime 用着，
在此之前**一条测试都没有**。它管的是"这次运行准干什么"：
能调哪些工具、能连哪些主机、能写几次外部系统、工作目录到哪为止。

先量后写：把 `constrained_by` 里唯一那句收窄
（`stored_values.intersection(caller_values)`）改成 `.union(...)`，
也就是"调用方报什么就准什么"——全仓 1302 条测试**全绿**。
一个能凭空给自己发权限的改动，没有任何东西拦得住。这个文件补的就是这个洞。

用词说明：这里说的"收窄"对每种字段长得不一样，别只测集合那一种——
  · 名单类（工具/主机/嵌套应用/连接器操作）：取交集，只会变少
  · 开关类 model_access：与，True 只能变 False
  · 开关类 governed_host_actions：或，False 只能变 True（治理是越多越紧）
  · 数量类（写次数、载荷字节）：取小
  · 需审批的操作：并集（要审批的越多越紧），再与"可写"取交
反着写任何一种都是放权，所以每一种都单独有一条。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.execution_policy import (
    ExecutionPolicyExpansionDenied,
    ExecutionPolicySnapshot,
)

ASSIGNMENT = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"


def published(tmp_path, **overrides) -> ExecutionPolicySnapshot:
    """一份发布时定下的策略：两个工具、两台主机、能写 10 次。"""
    fields = {
        "workspace_boundary": str(tmp_path / "ws"),
        "assignment_id": ASSIGNMENT,
        "session_id": SESSION,
        "allowed_nested_application_ids": ["app-a", "app-b"],
        "allowed_runtime_tools": ["read_file", "write_file"],
        "allowed_network_hosts": ["api.example.com", "cdn.example.com"],
        "model_access": True,
        "allowed_connector_operations": ["crm.read", "crm.write"],
        "writable_connector_operations": ["crm.write"],
        "permission_required_connector_operations": [],
        "compensation_connector_operations": ["crm.write"],
        "max_connector_write_count": 10,
        "max_connector_payload_bytes": 1024,
        "governed_host_actions": True,
    }
    fields.update(overrides)
    return ExecutionPolicySnapshot.build(**fields)


def asked(policy: ExecutionPolicySnapshot, **wanted) -> ExecutionPolicySnapshot:
    """调用方提出的要求；没提到的字段一律传 None（＝不动）。"""
    request = {
        "workspace_boundary": None,
        "assignment_id": None,
        "session_id": None,
        "allowed_nested_application_ids": None,
        "allowed_runtime_tools": None,
        "allowed_network_hosts": None,
        "model_access": None,
        "allowed_connector_operations": None,
        "writable_connector_operations": None,
        "permission_required_connector_operations": None,
        "compensation_connector_operations": None,
        "max_connector_write_count": None,
        "max_connector_payload_bytes": None,
        "governed_host_actions": False,
    }
    request.update(wanted)
    return policy.constrained_by(**request)


class TestNamesCanOnlyGetFewer:
    """名单类：调用方多报的一律不算数。"""

    @pytest.mark.parametrize(
        "field, stored, sneaked",
        [
            ("allowed_runtime_tools", "read_file", "run_shell"),
            ("allowed_nested_application_ids", "app-a", "app-evil"),
            ("allowed_network_hosts", "api.example.com", "evil.test"),
            ("allowed_connector_operations", "crm.read", "payments.transfer"),
        ],
    )
    def test_a_name_that_was_not_published_stays_out(
        self, tmp_path, field, stored, sneaked
    ):
        """夹带一个没发布过的名字，进不来；本来就有的那个还在。"""
        policy = published(tmp_path)
        narrowed = asked(policy, **{field: [stored, sneaked]})
        assert sneaked not in getattr(narrowed, field)
        assert stored in getattr(narrowed, field)

    def test_asking_for_less_really_gives_less(self, tmp_path):
        """真收窄要生效——否则"放宽不行"可以靠"什么都不改"作弊过关。"""
        policy = published(tmp_path)
        narrowed = asked(policy, allowed_runtime_tools=["read_file"])
        assert narrowed.allowed_runtime_tools == ("read_file",)

    def test_saying_nothing_keeps_what_was_published(self, tmp_path):
        """None 是"不动"，不是"清空"，也不是"随便"。"""
        policy = published(tmp_path)
        assert asked(policy).allowed_runtime_tools == policy.allowed_runtime_tools

    def test_a_host_only_differing_in_case_is_still_the_same_host(self, tmp_path):
        """主机名大小写和末尾的点不算区别，不然合法主机会被无声丢掉。"""
        policy = published(tmp_path)
        narrowed = asked(policy, allowed_network_hosts=["API.Example.COM."])
        assert narrowed.allowed_network_hosts == ("api.example.com",)


class TestSwitchesCanOnlyGetStricter:
    """两个开关的紧方向是相反的，所以分开写。"""

    def test_model_access_can_be_dropped_but_not_granted(self, tmp_path):
        without = published(tmp_path, model_access=False)
        assert asked(without, model_access=True).model_access is False
        assert asked(published(tmp_path), model_access=False).model_access is False

    def test_host_governance_can_be_added_but_not_removed(self, tmp_path):
        """治理开着就摘不掉——这一项紧的方向是 True。"""
        governed = published(tmp_path, governed_host_actions=True)
        assert asked(governed, governed_host_actions=False).governed_host_actions is True
        ungoverned = published(tmp_path, governed_host_actions=False)
        assert asked(ungoverned, governed_host_actions=True).governed_host_actions is True


class TestBudgetsCanOnlyGetSmaller:
    """数量类：取小。"""

    @pytest.mark.parametrize(
        "field, published_value, greedy, modest",
        [
            ("max_connector_write_count", 10, 10_000, 3),
            ("max_connector_payload_bytes", 1024, 10 * 1024 * 1024, 256),
        ],
    )
    def test_asking_for_more_gets_the_published_number(
        self, tmp_path, field, published_value, greedy, modest
    ):
        policy = published(tmp_path)
        assert getattr(asked(policy, **{field: greedy}), field) == published_value
        assert getattr(asked(policy, **{field: modest}), field) == modest


class TestApprovalCanOnlyBeAdded:
    """需审批的操作是并集：调用方能给自己加审批，不能替自己免审批。"""

    def test_a_caller_cannot_drop_an_approval_requirement(self, tmp_path):
        policy = published(
            tmp_path, permission_required_connector_operations=["crm.write"]
        )
        narrowed = asked(policy, permission_required_connector_operations=[])
        assert "crm.write" in narrowed.permission_required_connector_operations

    def test_a_caller_can_add_one(self, tmp_path):
        policy = published(tmp_path)
        narrowed = asked(
            policy, permission_required_connector_operations=["crm.write"]
        )
        assert "crm.write" in narrowed.permission_required_connector_operations

    def test_approval_never_covers_something_that_cannot_be_written(self, tmp_path):
        """只读的操作挂不上"要审批"——审批闸本来就只拦写。"""
        policy = published(tmp_path)
        narrowed = asked(
            policy, permission_required_connector_operations=["crm.read"]
        )
        assert "crm.read" not in narrowed.permission_required_connector_operations


class TestTheAuthorityCannotBeRebound:
    """换个身份、换个工作目录来跑，都要当场拒。"""

    def test_another_assignment_is_refused(self, tmp_path):
        with pytest.raises(ExecutionPolicyExpansionDenied):
            asked(published(tmp_path), assignment_id=OTHER_UUID)

    def test_another_session_is_refused(self, tmp_path):
        with pytest.raises(ExecutionPolicyExpansionDenied):
            asked(published(tmp_path), session_id=OTHER_UUID)

    def test_a_workspace_outside_the_published_one_is_refused(self, tmp_path):
        with pytest.raises(ExecutionPolicyExpansionDenied):
            asked(published(tmp_path), workspace_boundary=str(tmp_path / "elsewhere"))

    def test_the_parent_directory_is_refused(self, tmp_path):
        """往上一级是放宽，不是收窄。"""
        with pytest.raises(ExecutionPolicyExpansionDenied):
            asked(published(tmp_path), workspace_boundary=str(tmp_path))

    def test_a_sibling_with_the_same_prefix_is_refused(self, tmp_path):
        """`ws-evil` 的字符串前缀是 `ws`，但它不在 `ws` 里面。

        按字符串前缀比路径的实现会放它进来，按路径分段比的不会。
        """
        with pytest.raises(ExecutionPolicyExpansionDenied):
            asked(published(tmp_path), workspace_boundary=str(tmp_path / "ws-evil"))

    def test_a_subdirectory_is_allowed_and_takes_effect(self, tmp_path):
        """往里缩是收窄，准，而且要真缩进去。"""
        inner = tmp_path / "ws" / "step-1"
        narrowed = asked(published(tmp_path), workspace_boundary=str(inner))
        assert narrowed.workspace_boundary == str(inner)


class TestAPublishedPolicyCannotBeEditedInPlace:
    """策略是连着摘要一起存的，改一个字就验不过——包括改摘要本身。"""

    def test_adding_a_tool_by_hand_is_rejected(self, tmp_path):
        raw = published(tmp_path).model_dump(mode="json")
        raw["allowed_runtime_tools"] = [*raw["allowed_runtime_tools"], "run_shell"]
        with pytest.raises(ValidationError, match="digest mismatch"):
            ExecutionPolicySnapshot.model_validate(raw)

    def test_raising_a_budget_by_hand_is_rejected(self, tmp_path):
        raw = published(tmp_path).model_dump(mode="json")
        raw["max_connector_write_count"] = 10_000
        with pytest.raises(ValidationError, match="digest mismatch"):
            ExecutionPolicySnapshot.model_validate(raw)

    def test_moving_the_workspace_by_hand_is_rejected(self, tmp_path):
        raw = published(tmp_path).model_dump(mode="json")
        raw["workspace_boundary"] = str(tmp_path)
        with pytest.raises(ValidationError, match="digest mismatch"):
            ExecutionPolicySnapshot.model_validate(raw)

    def test_an_untouched_policy_still_loads(self, tmp_path):
        """反向那一条：原样存下再读回来必须过，否则"改了就拒"是靠全拒实现的。"""
        policy = published(tmp_path)
        again = ExecutionPolicySnapshot.model_validate(policy.model_dump(mode="json"))
        assert again == policy


class TestTheHostPathNeverLeaks:
    def test_the_projection_has_no_filesystem_path(self, tmp_path):
        """给外面看的那份不带真实目录，只带一个摘要。"""
        policy = published(tmp_path)
        projection = policy.public_projection()
        assert "workspace_boundary" not in projection
        assert str(tmp_path) not in str(projection)
        assert projection["workspace_scope"]["digest"] == policy.workspace_scope_digest

    def test_the_digest_still_tells_two_workspaces_apart(self, tmp_path):
        """路径藏了，但"是不是同一个工作目录"还得答得上来。

        不然把摘要写成常量也能过上面那条：路径确实没漏，可它也不指向任何东西了。
        """
        here = published(tmp_path)
        same = published(tmp_path)
        other = published(tmp_path / "another")
        assert here.public_projection()["workspace_scope"]["digest"] == (
            same.public_projection()["workspace_scope"]["digest"]
        )
        assert here.public_projection()["workspace_scope"]["digest"] != (
            other.public_projection()["workspace_scope"]["digest"]
        )
