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


class MisplacedConfigKeysTest(unittest.TestCase):
    """本该写进 config 的字段放在了节点顶层——意图无歧义，就别judge。

    真机测量（2026-08-29）：「Extra inputs are not permitted」被拒 53 次，
    绝大多数是模型把 outputs / inputs 写在了节点顶层。它从那句 pydantic
    英文里看不出层级放错了，于是反复重提——而"反复提同一个被拒的方案"
    正是搭建失败的头号原因（57 次失败里 23 次）。

    同一个文件里 update_node 早就这么干了：merge_config 被嵌进 changes 时
    直接抬出来，注释写着 "the intent is unambiguous, so hoist it"。
    这里是同一条道理，方向相反——该往里放的就往里放。
    """

    @staticmethod
    def _sink(node):
        return ApplicationService._sink_config_keys(node)

    def test_a_top_level_outputs_moves_into_config(self):
        node = self._sink({"id": "end", "title": "结束", "type": "end",
                           "outputs": {"r": 1}})
        self.assertEqual(node["config"]["outputs"], {"r": 1})
        self.assertNotIn("outputs", node)

    def test_it_merges_with_an_existing_config(self):
        node = self._sink({"id": "n", "type": "x", "config": {"a": 1}, "b": 2})
        self.assertEqual(node["config"], {"a": 1, "b": 2})

    def test_a_conflicting_key_is_left_alone(self):
        """两处都写了，谁覆盖谁没有唯一答案——交给校验去报，别猜。"""
        node = self._sink({"id": "n", "type": "x",
                           "config": {"outputs": {"a": 1}}, "outputs": {"b": 2}})
        self.assertEqual(node["config"]["outputs"], {"a": 1})
        self.assertEqual(node["outputs"], {"b": 2})

    def test_a_well_formed_node_is_untouched(self):
        node = {"id": "s", "title": "开始", "type": "start", "config": {"inputs": []}}
        self.assertEqual(self._sink(node), node)

    def test_known_node_fields_stay_at_the_top(self):
        """title / description / retry 这些是 NodeSpec 自己的字段，不能被搬走。"""
        node = self._sink({"id": "n", "title": "标题", "description": "说明",
                           "type": "x", "retry": {"max_attempts": 2}})
        for key in ("title", "description", "retry"):
            self.assertIn(key, node, key)

    def test_a_non_dict_is_returned_as_is(self):
        for odd in (None, "字符串", 42, ["列表"]):
            self.assertEqual(self._sink(odd), odd)

    def test_add_node_really_accepts_a_misplaced_outputs(self):
        """接线：真走一遍 _apply_to_snapshot，看节点有没有落对地方。

        初稿写的是 inspect.getsource + 查有没有 "_sink_config_keys" 这个串，
        自己删了——那是断言源码长什么样。
        """
        from types import SimpleNamespace

        service = ApplicationService.__new__(ApplicationService)
        service.blocks = SimpleNamespace(validate_node=lambda node: None)
        snapshot = SNAPSHOT.model_copy(deep=True)
        service._apply_to_snapshot(snapshot, "add_node", {"node": {
            "id": "汇总", "title": "汇总", "type": "variable_assigner",
            # 本该写在 config 里，模型放到了顶层
            "assignments": {"总额": 1},
        }})
        node = next(n for n in snapshot.workflow.nodes if n.id == "汇总")
        self.assertEqual(node.config.get("assignments"), {"总额": 1},
                         "顶层字段没被收进 config")


class FlattenedTestPayloadTest(unittest.TestCase):
    """test_add 被摊平写时也认——不认就只回一句 KeyError: 'test'。

    真机测量（2026-08-29）：test_add 被拒 27 次，全是这一句裸报错。
    模型把 id / name / requirement / assertions 平铺在了顶层
    （有几次还自己发明了一个 frame 包着）。schema 写的是嵌在 test 下，
    但摊平的意图无歧义，而 KeyError 什么也没教，它只能重试。
    """

    TEST = {"id": "t1", "name": "门店合计", "requirement": "各门店求和",
            "assertions": [], "mandatory": True}

    def test_the_documented_shape_works(self):
        self.assertEqual(
            ApplicationService._test_payload({"test": self.TEST})["name"], "门店合计")

    def test_a_flattened_payload_works(self):
        self.assertEqual(
            ApplicationService._test_payload(dict(self.TEST))["name"], "门店合计")

    def test_a_frame_wrapper_works(self):
        """模型爱用 frame 这个名字，schema 里根本没有。"""
        self.assertEqual(
            ApplicationService._test_payload({"frame": self.TEST})["name"], "门店合计")

    def test_the_explicit_test_key_wins(self):
        payload = ApplicationService._test_payload(
            {"test": self.TEST, "name": "另一个", "requirement": "x"})
        self.assertEqual(payload["name"], "门店合计")

    def test_an_unrecognisable_payload_teaches_instead_of_keyerror(self):
        with self.assertRaises(ValueError) as caught:
            ApplicationService._test_payload({"随便": 1})
        message = str(caught.exception)
        self.assertIn("test", message)
        self.assertIn("requirement", message, "没给出要填什么")
        self.assertNotIn("KeyError", message)

    def test_flattening_only_takes_known_fields(self):
        """摊平时别把无关的键一起塞进测试用例。"""
        payload = ApplicationService._test_payload(
            {**self.TEST, "expected_revision": 7, "idempotency_key": "abc"})
        self.assertNotIn("expected_revision", payload)
        self.assertNotIn("idempotency_key", payload)
