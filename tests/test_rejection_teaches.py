"""拒绝要教学：说清为什么被拒、下一步做什么。

回归背景（2026-08-29 拿 76 份真机搭建 transcript 统计）：
搭建过程中被拒的工具调用 1087 次、150 种，其中最大的一类是

    ValueError: draft operation would not change the workflow

一句英文，说了"没变化"，没说**为什么**没变化，也没说**该怎么办**。
模型能做的只有再试一遍——而"反复提同一个被拒的方案"正是搭建失败的
头号原因（57 次失败里 23 次，40%）。

数据要说清楚：这条拒绝在 8-23 占当天工具调用的 23%（332/1461），
最近三天是 0 次——但那三天工具调用总共才 272 次，样本太小，
不足以说它已经消失。这个改动不依赖那个判断：信息严格变多，没有风险。

本项目在别处写过「拒绝即教学」（见 _node_id_hint：node not found 会列出
现有 id）。这一条一直没照做。
"""
import unittest

from agent_platform.applications import ApplicationService
from agent_platform.workflow_models import ApplicationSnapshot

SNAPSHOT = ApplicationSnapshot.model_validate({
    "name": "日报", "description": "", "requirement": "",
    "workflow": {"nodes": [
        {"id": "start", "title": "开始", "type": "start", "config": {"inputs": []}},
        {"id": "end", "title": "结束", "type": "end", "config": {}}],
        "edges": [{"source": "start", "target": "end"}]}})


def _why(operation, data):
    return ApplicationService._why_no_change(operation, data, SNAPSHOT)


class RejectionTeachesTest(unittest.TestCase):
    def test_adding_an_existing_node_says_what_to_do_instead(self):
        text = _why("add_node", {"node": {"id": "start"}})
        self.assertIn("start", text)
        self.assertIn("draft_update_node", text, "没告诉它改用哪个工具")

    def test_updating_with_identical_config_says_to_look_first(self):
        text = _why("update_node", {"node_id": "start"})
        self.assertIn("draft_inspect", text, "没告诉它先去看当前状态")

    def test_removing_a_missing_node_lists_the_real_ones(self):
        text = _why("remove_node", {"node_id": "没有这个"})
        self.assertIn("start", text)
        self.assertIn("end", text)

    def test_a_duplicate_edge_names_both_ends(self):
        text = _why("add_edge", {"edge": {"source": "start", "target": "end"}})
        self.assertIn("start", text)
        self.assertIn("end", text)
        self.assertIn("draft_remove_edge", text)

    def test_removing_a_missing_edge_points_at_inspect(self):
        text = _why("remove_edge", {"edge": {"source": "a", "target": "b"}})
        self.assertIn("draft_inspect", text)

    def test_an_unknown_operation_still_says_something_useful(self):
        text = _why("set_metadata", {})
        self.assertIn("draft_inspect", text)
        self.assertTrue(text.strip())

    def test_no_message_is_english(self):
        import re

        for operation, data in (("add_node", {"node": {"id": "x"}}),
                                ("update_node", {"node_id": "x"}),
                                ("remove_node", {"node_id": "x"}),
                                ("add_edge", {"edge": {"source": "a", "target": "b"}}),
                                ("remove_edge", {"edge": {"source": "a", "target": "b"}}),
                                ("add_test", {}), ("set_metadata", {})):
            text = _why(operation, data)
            # 工具名（draft_inspect 之类）是给模型看的，不算"英文散文"
            prose = re.sub(r"\b(draft|test)_[a-z_]+\b", "", text)
            self.assertIsNone(re.search(r"[a-z]{4,}\s+[a-z]{4,}", prose),
                              f"{operation}: {text}")

    def test_every_message_says_what_to_do_next(self):
        """只说"不行"不算教学——每一条都得给下一步。"""
        for operation, data in (("add_node", {"node": {"id": "x"}}),
                                ("update_node", {"node_id": "x"}),
                                ("remove_node", {"node_id": "x"}),
                                ("add_edge", {"edge": {"source": "a", "target": "b"}}),
                                ("remove_edge", {"edge": {"source": "a", "target": "b"}}),
                                ("add_test", {}), ("set_metadata", {})):
            text = _why(operation, data)
            self.assertTrue(
                any(word in text for word in ("draft_", "test_list", "换", "先")),
                f"{operation} 只说了不行，没说该怎么办：{text}")


class ItIsWiredIntoApplyOperationTest(unittest.TestCase):
    def test_apply_operation_uses_the_teaching_message(self):
        import inspect

        source = inspect.getsource(ApplicationService.apply_operation)
        self.assertIn("_why_no_change", source)
        self.assertNotIn("would not change the workflow", source)


if __name__ == "__main__":
    unittest.main()
