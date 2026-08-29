"""业主拿到的搭建记录：只给白名单里那几样，别的一律不递。

原来是黑名单——只把 turn 的 text 换成空串，别的字段原样递出去。
拿真机 76 份 transcript、2653 条记录跑一遍当时的规则，递出去的有：

  · thinking 非空 **452 条**：搭建方的原始思考，正是那段注释里
    说"一句都不给"的东西。英文的 "Okay, let's see. The user wants…"、
    中文的 "end节点的greeting字段仍然是字面量'string'" 都在里面。
    **闸装在 text 上，没装在 thinking 上**，而两者是同一类东西。
  · model：deepseek-v4-pro、local/Qwen/Qwen3-4B-Instruct-2507 等四个
  · actor：architect、configurator、repairer 等十个内部角色名
  · tool_calls 里的 arguments / result：2547 条带机器痕迹

这个接口只要有业主码就能直接 curl，前端渲不渲染都不影响它已经发出去了。

黑名单的毛病不只是这次漏了三样：transcript 以后多一个字段，
它就自动出现在业主那边。白名单反过来，多的字段默认不给。

**仍然会递出去的**：tool_calls 里的工具名（draft_add_node 这种）。
前端靠它翻成「搭了一个环节」。要彻底不递就得改成服务端翻好再发，
那要前后端一起动，这次没做——写在这里，别当成已经解决了。
"""

from __future__ import annotations

import pytest

from agent_platform.api import _owner_safe_records

INTERNALS = ("thinking", "model", "actor", "prompt", "usage", "stop_reason",
             "draft_revision")


def _builder_turn(**extra) -> dict:
    """一条搭建方轮次，字段照真 transcript 的样子给全。"""
    record = {
        "recorded_at": "2026-08-30T00:00:00+00:00",
        "kind": "turn",
        "turn": 3,
        "actor": "coordinator",
        "model": "deepseek-v4-pro",
        "thinking": "Okay, let's see. The end node still has a literal 'string'.",
        "text": "我把 aggregator→assigner 的边删了，改接到 variable_assigner。",
        "prompt": "内部提示词",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1234},
        "draft_revision": 7,
        "tool_calls": [{
            "tool": "draft_add_node",
            "arguments": {"node": {"id": "n1", "type": "variable_assigner"}},
            "result": '{"revision": 8}',
            "truncated": False,
            "is_error": False,
        }],
    }
    record.update(extra)
    return record


class TestNothingInternalGetsThrough:
    @pytest.mark.parametrize("field", INTERNALS)
    def test_the_field_is_not_handed_over(self, field):
        [safe] = _owner_safe_records([_builder_turn()])
        assert field not in safe, f"{field} 递出去了：{safe.get(field)!r}"

    def test_the_builder_prose_is_gone_but_the_gap_is_marked(self):
        """正文不给，但要留个记号——前端靠它补时间线上的占位。"""
        [safe] = _owner_safe_records([_builder_turn()])
        assert safe["text"] == ""
        assert safe["text_withheld"] is True

    def test_tool_arguments_and_results_are_dropped(self):
        """动作行只需要工具名；参数和结果里是节点 id、公式、schema。"""
        [safe] = _owner_safe_records([_builder_turn()])
        assert safe["tool_calls"] == [{"tool": "draft_add_node"}]
        assert "variable_assigner" not in str(safe), safe

    def test_a_field_nobody_thought_of_is_not_handed_over(self):
        """白名单的意义就在这一条：以后新加的字段默认不给。

        黑名单写法下这条必红——它只挡它认识的那几个。
        """
        [safe] = _owner_safe_records([
            _builder_turn(secret_scratchpad="内部草稿", cost_usd=0.42)])
        assert "secret_scratchpad" not in safe
        assert "cost_usd" not in safe


class TestWhatTheOwnerShouldSeeStillArrives:
    """反向那一批。没有它们，"什么都不给"也能让上面全绿。"""

    def test_the_owners_own_message_comes_back_whole(self):
        [safe] = _owner_safe_records([{
            "kind": "owner", "turn": 2, "text": "我想让它只统计两次以上的词",
            "recorded_at": "2026-08-30T00:00:00+00:00"}])
        assert safe["text"] == "我想让它只统计两次以上的词"
        assert not safe.get("text_withheld")

    def test_a_milestone_event_comes_through(self):
        [safe] = _owner_safe_records([{
            # 事件类别用真值：白名单是 {"published", "needs_attention"}，
            # 我顺手写成了 "workflow.published"，那条根本不在名单里——
            # 断言红了才发现测的是我以为的名字。
            "kind": "event", "turn": 4, "event": "published",
            "text": "工作流已发布为正式版 v1，现在可以试运行了",
            "recorded_at": "2026-08-30T00:00:00+00:00"}])
        assert "已发布为正式版" in safe["text"]

    def test_the_action_line_still_has_something_to_render_from(self):
        """前端拿工具名翻成「搭了一个环节」——名字没了动作行就空了。"""
        [safe] = _owner_safe_records([_builder_turn()])
        assert safe["tool_calls"][0]["tool"] == "draft_add_node"

    def test_ordering_fields_survive(self):
        """turn / recorded_at 是排序和翻页要用的。"""
        [safe] = _owner_safe_records([_builder_turn()])
        assert safe["turn"] == 3
        assert safe["recorded_at"].startswith("2026-08-30")


class TestItHoldsOnRealShapedRecords:
    def test_a_record_with_no_tool_calls_has_no_empty_key(self):
        [safe] = _owner_safe_records([_builder_turn(tool_calls=[])])
        assert "tool_calls" not in safe

    def test_an_event_that_is_not_owner_facing_is_withheld(self):
        """不是给业主看的 event，正文照样要挡下来。"""
        [safe] = _owner_safe_records([{
            "kind": "event", "turn": 1, "event": "builder.internal.retry",
            "text": "coordinator 第 3 次重试 draft_connect",
            "recorded_at": "2026-08-30T00:00:00+00:00"}])
        assert safe["text"] == ""
        assert safe["text_withheld"] is True

    def test_every_record_is_still_returned(self):
        """挡的是内容，不是条目——少了条目时间线会断。"""
        records = [_builder_turn(), {"kind": "owner", "text": "好"},
                   {"kind": "event", "text": "已发布"}]
        assert len(_owner_safe_records(records)) == 3
