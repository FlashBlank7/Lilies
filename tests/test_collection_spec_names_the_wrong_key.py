"""集合参数写错键名时，报出来的那句话反而指着写对了的地方。

真机 2026-08-23 最大的一族失败（66 次，占当天全部失败的多数）配的是：

    {"$sum": {"collection": {"$ref": {"node_id": "start", "path": ["sales"]}},
              "path": "amount"}}

`_assignment_collection_spec` 只认 items/path/where。`collection` 不在其中，
于是**整个字典被当成"一个值"**去解析，解析出来必然不是数组，
报的是 `collection expression requires an array`。

这句话里偏偏有个 collection——读起来像"你那个 collection 不是数组"，
而那个 collection 确确实实指着一个数组。写的人（和修工作流的模型）
只会盯着对的地方发呆。**真因是那个键根本没被读。**

这和本文件 4941 行修过的是同一个毛病、低一层：那次是操作符名字写错
（模型发明了 $sum_by），这次是参数键名写错。同一个判据没铺满所有出口。

反向那一批不能省：$ref 这类以 $ 开头的合法操作数必须照常放行，
否则整个赋值块都不能用了。
"""

from __future__ import annotations

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime


def _spec(operand, context=None):
    return WorkflowRuntime._assignment_collection_spec(operand, context or {})


class TestTheRealShapeFromProduction:
    def test_the_66_times_config_now_says_which_key_is_wrong(self):
        operand = {"collection": {"$ref": {"node_id": "start", "path": ["sales"]}},
                   "path": "amount"}
        with pytest.raises(ValueError, match="collection"):
            _spec(operand)

    def test_it_no_longer_blames_the_array(self):
        """旧话术里那句 requires an array 会把人引到对的地方去找错。"""
        operand = {"collection": [1, 2, 3]}
        with pytest.raises(ValueError) as caught:
            _spec(operand)
        assert "requires an array" not in str(caught.value)

    def test_it_says_what_the_right_key_is(self):
        """只说"不认识"没法改——得说该写什么。"""
        with pytest.raises(ValueError, match="items"):
            _spec({"collection": [1, 2, 3]})

    def test_every_unknown_key_is_listed(self):
        with pytest.raises(ValueError) as caught:
            _spec({"collection": [], "key": "store", "value": "amount"})
        for key in ("collection", "key", "value"):
            assert key in str(caught.value), key


class TestTheAcceptedShapesStillWork:
    """反向那一批：少了这些，"看到字典就拒"也能让上面全绿。"""

    def test_a_bare_list_is_the_collection(self):
        items, path, where = _spec([1, 2, 3])
        assert items == [1, 2, 3] and path == [] and where is None

    def test_items_with_a_path(self):
        items, path, where = _spec({"items": [{"n": 1}], "path": ["n"]})
        assert items == [{"n": 1}] and path == ["n"]

    def test_items_alone(self):
        items, _, _ = _spec({"items": [1]})
        assert items == [1]

    def test_items_with_a_where(self):
        _, _, where = _spec({"items": [{"a": 1}],
                             "where": {"path": ["a"], "equals": 1}})
        assert where == {"path": ["a"], "equals": 1}

    def test_a_ref_operand_is_not_mistaken_for_a_wrong_key(self):
        """`{"$ref": …}` 的键也不在 items/path/where 里，但它是**对的**写法。
        判据必须放过带 $ 的键，不然合法的引用会被当成写错。"""
        context = {"nodes": {"start": {"output": {"sales": [{"amount": 1}]}}}}
        items, _, _ = _spec(
            {"$ref": {"node_id": "start", "path": ["output", "sales"]}}, context)
        assert items == [{"amount": 1}]

    def test_items_holding_a_ref_also_works(self):
        """正规写法：items 里放引用。这条要是坏了，写对的人也用不了。"""
        context = {"nodes": {"start": {"output": {"sales": [{"amount": 2}]}}}}
        items, path, _ = _spec(
            {"items": {"$ref": {"node_id": "start", "path": ["output", "sales"]}},
             "path": ["amount"]}, context)
        assert items == [{"amount": 2}] and path == ["amount"]


class TestValuesThatAreSimplyNotArrays:
    """不是字典的那些，照旧走原来那句话——**不改它**：
    面板的译文和失败聚类都在正则匹配这句英文。"""

    @pytest.mark.parametrize("operand", ["文本", 42, None, True])
    def test_a_non_array_is_still_refused_the_old_way(self, operand):
        with pytest.raises(TypeError, match="requires an array"):
            _spec(operand)
