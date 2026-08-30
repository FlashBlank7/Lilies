"""客户表单上不该出现平台自己的接线字段。

变异验证（2026-08-30，全量 2671 条）：把 `_public_input` 里
`name in _INTERNAL_CONNECTOR_INPUTS` 那半个判据去掉——
于是 actor_id / tenant_id / connector_authorization_id / write_mode
这些**平台内部接线用的输入**会一个不落地出现在业主客户看到的表单上——
一条测试都没红。

同一个函数里另外半个判据（名字为空就不要）也一起钉上：
两个条件写在一个 if 里，去掉一个照样能让"另一个还在"的测试全绿。

漏出去的后果分两层：
· 看得见：客户看到一堆看不懂的字段，像是这个服务做坏了
· 更要紧：表单字段是**要填的**——把 tenant_id / actor_id 摆到客户面前，
  等于邀请他往平台的身份字段里写东西

这一族的其余八个判据（私有键、嵌套、列表、凭据文本、授权头、
内部报错标记、脱敏改动即隐藏、递归深度）都有人盯着，就这个没有。
"""

from __future__ import annotations

import pytest

from agent_platform.customer_runtime_projection import (
    _INTERNAL_CONNECTOR_INPUTS,
    _public_trigger_config,
)


def _names(config: dict) -> list[str]:
    return [item.get("name") for item
            in _public_trigger_config(config)["settings"]["inputs"]]


def _config(*inputs) -> dict:
    return {"settings": {"inputs": list(inputs)}}


class TestInternalWiringStaysOut:
    @pytest.mark.parametrize("name", sorted(_INTERNAL_CONNECTOR_INPUTS))
    def test_each_internal_input_is_dropped(self, name):
        assert _names(_config({"name": name, "label": "x"})) == []

    def test_they_are_dropped_even_next_to_real_ones(self):
        """混在一起时更要拦——真字段还在，会让人以为整份都过了闸。"""
        names = _names(_config(
            {"name": "月份", "label": "月份"},
            {"name": "tenant_id", "label": "租户"},
            {"name": "门店", "label": "门店"},
            {"name": "actor_id", "label": "操作人"},
        ))
        assert names == ["月份", "门店"]

    def test_the_list_is_not_empty(self):
        """防空跑：名单空了的话上面那批 parametrize 会一条都不跑。"""
        assert len(_INTERNAL_CONNECTOR_INPUTS) >= 5


class TestAnUnnamedInputIsAlsoDropped:
    """同一个 if 里的另外半个判据。两个条件写在一起时，
    去掉一个照样能让"另一个还在"的测试全绿——所以两个都要单独钉。"""

    @pytest.mark.parametrize("item", [
        {"label": "没有名字"},
        {"name": "", "label": "空名字"},
        {"name": "   ", "label": "只有空格"},
    ])
    def test_it_is_dropped(self, item):
        assert _names(_config(item)) == []


class TestOrdinaryInputsStillGetThrough:
    """反向那一批：少了它们，"一律丢掉"也能让上面全绿——
    而那会让客户页上一个可填的字段都没有。"""

    def test_a_plain_input_survives(self):
        assert _names(_config({"name": "月份", "label": "月份"})) == ["月份"]

    def test_its_display_fields_survive(self):
        item = _public_trigger_config(
            _config({"name": "月份", "label": "统计月份",
                     "type": "string", "required": True}))["settings"]["inputs"][0]
        assert item["label"] == "统计月份"
        assert item["type"] == "string"
        assert item["required"] is True

    def test_a_name_that_merely_contains_an_internal_word_is_kept(self):
        """判据是**整名相等**，不是包含——「租户名称」不该被误杀。"""
        assert _names(_config({"name": "tenant_id_display", "label": "x"})) \
            == ["tenant_id_display"]


class TestDeepNestingIsCutOff:
    """递归深度上限也没人盯着——去掉它，构造一份深嵌套就能把进程拖垮。

    输入是**业主的客户**给的（客户使用页的表单、上传的表），
    也就是说这个深度是外部可控的。
    """

    @staticmethod
    def _nest(levels: int):
        root: dict = {}
        current = root
        for _ in range(levels):
            current["k"] = {}
            current = current["k"]
        return root

    def _depth_of(self, value) -> int:
        depth = 0
        while isinstance(value, dict) and "k" in value and value["k"] is not None:
            depth += 1
            value = value["k"]
        return depth

    def test_a_deep_shape_is_cut_not_carried(self):
        from agent_platform.customer_runtime_projection import project_public_value

        projected = project_public_value(self._nest(200))
        assert self._depth_of(projected) < 200

    def test_an_ordinary_shape_survives_intact(self):
        """反向：不能宽到"稍微嵌套就砍"——业务数据本来就有几层。"""
        from agent_platform.customer_runtime_projection import project_public_value

        projected = project_public_value(self._nest(5))
        assert self._depth_of(projected) == 5

    def test_it_does_not_blow_the_stack(self):
        """真正的赌注：不是"截得准不准"，是**别把进程拖垮**。"""
        from agent_platform.customer_runtime_projection import project_public_value

        project_public_value(self._nest(5_000))
