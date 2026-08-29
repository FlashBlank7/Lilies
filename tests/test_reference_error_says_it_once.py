"""引用解析失败的那条报错，把同一句话说了两遍。

真机上这是第二大的失败族（「workflow reference could not resolve …」）。
拼报错的那段里，"该修哪一端"那一块**连着写了两遍**——
八行注释加八行代码原样复制，于是每一条这类报错的结尾都是：

    …改引用方而不改产出方是修不好的。；要么让节点 'start' 真正产出
    output.sales（例如…），要么把这里的引用改成上面已有的路径之一——
    改引用方而不改产出方是修不好的。

读的人是业主和**修工作流的模型**。同一句指令说两遍，除了让人怀疑
自己看漏了什么，没有别的作用；对模型更糟，重复的指令会被当成强调。

这一族本来就有很用心的话术（近亲判断、分段数组的写法提醒、
该改哪一端），偏偏结尾复读——正说明**没有任何测试在读这条消息的全文**。
这个文件就是来读全文的。
"""

from __future__ import annotations

import pytest

from agent_platform.workflow_runtime import WorkflowRuntime


def _resolve_failure(path: list, context: dict) -> str:
    with pytest.raises(Exception) as caught:
        WorkflowRuntime._assignment_collection_spec(
            {"$ref": {"node_id": "start", "path": path}}, context)
    return str(caught.value)


CONTEXT = {"nodes": {"start": {"output": {"sales_records": [{"amount": 1}]}}}}


class TestItSaysEachThingOnce:
    def test_the_which_end_to_fix_sentence_appears_once(self):
        message = _resolve_failure(["output", "sales"], CONTEXT)
        assert message.count("要么让节点") == 1, message

    def test_no_sentence_in_the_message_is_repeated(self):
        """不只钉那一句——整条消息里任何一句都不该出现两遍。

        照句号切开数，比只盯一个关键词更难被下一次复制粘贴绕过去。
        """
        message = _resolve_failure(["output", "sales"], CONTEXT)
        sentences = [s.strip() for s in message.split("。") if len(s.strip()) > 12]
        duplicated = {s for s in sentences if sentences.count(s) > 1}
        assert not duplicated, duplicated

    def test_the_far_miss_branch_is_also_clean(self):
        """两个分支各自拼各自的话，都要读一遍——近亲那条上面测了，这是另一条。"""
        message = _resolve_failure(["output", "完全不沾边的字段"], CONTEXT)
        assert message.count("要么让节点") == 1, message


class TestItStillSaysTheUsefulThings:
    """反向那一批：去重不能把有用的内容一起去掉。"""

    def test_it_still_names_the_node_and_the_wanted_path(self):
        message = _resolve_failure(["output", "sales"], CONTEXT)
        assert "start" in message and "sales" in message

    def test_it_still_lists_the_paths_that_do_exist(self):
        """光说取不到没法改——得摆出这个节点真正有什么。"""
        message = _resolve_failure(["output", "sales"], CONTEXT)
        assert "sales_records" in message

    def test_the_hint_uses_the_real_input_syntax(self):
        """提示要用分段数组写法：模型会照抄提示，写成 ["output.字段"] 永远解析不了。"""
        message = _resolve_failure(["output", "sales"], CONTEXT)
        assert '["output", "sales_records"]' in message

    def test_a_near_miss_is_told_to_fix_the_reference_side(self):
        message = _resolve_failure(["output", "sales"], CONTEXT)
        assert "先改引用方" in message
