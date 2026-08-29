"""公式语法要在发布时查，不能等第一次真跑才炸。

回归背景（2026-08-29 真机报错清单）：

    node assigner failed: 公式包含不支持的字符 '.'（位置 8）

公式写在节点配置里，**静态可查**。可 validate_workflow 从头到尾不碰它，
于是写错的公式顺利发布，业主眼里是"东西发布了、跑起来却坏了"——
而这个错在发布那一刻就看得见。

只解析不求值：变量的值要到运行时才有，求值必然失败，那不是语法问题。

新规则拿真机 93 份快照（全部发布版 + 全部草稿）验过一遍：
  · 发布版被判不合法的：**0 份**——现有线上工作流一个都不受影响
  · 草稿被拦下的 8 份，每一份都是真坏的：
    用了不存在的函数 dict、把模板串「{by_store}」当公式喂进来、
    引用 template_transform / aggregate / sum 这些图里没有的节点。
    正是会在第一次真跑时炸的那些。
"""
import unittest

from agent_platform.blocks import build_block_registry
from agent_platform.formula import FormulaError, check_formula


class CheckFormulaTest(unittest.TestCase):
    def test_the_real_machine_failure_is_caught(self):
        with self.assertRaises(FormulaError) as caught:
            check_formula("by_store.A店 + by_store.B店")
        self.assertIn("不支持的字符", str(caught.exception))

    def test_a_truncated_expression_is_caught(self):
        with self.assertRaises(FormulaError):
            check_formula("a + ")

    def test_an_empty_expression_is_caught(self):
        for empty in ("", "   ", None):
            with self.assertRaises(FormulaError):
                check_formula(empty)

    def test_a_good_expression_passes(self):
        for good in ("1 + 2", "sum(a, b)", "记录 * 2", "max(a, b) - 1"):
            check_formula(good)

    def test_unknown_variables_are_not_a_syntax_error(self):
        """变量要到运行时才有值——只查语法，不许因为"变量没定义"就拦下。"""
        check_formula("从没见过的变量 + 1")


class ValidateWorkflowChecksFormulasTest(unittest.TestCase):
    """接线：函数写好了没接进发布校验，等于没写。"""

    @staticmethod
    def _workflow(expression):
        from agent_platform.workflow_models import WorkflowSpec

        return WorkflowSpec.model_validate({"nodes": [
            {"id": "start", "title": "start", "type": "start", "config": {"inputs": []}},
            {"id": "assigner", "title": "assigner", "type": "variable_assigner", "config": {
                "assignments": {"总额": {"$formula": {"expression": expression,
                                                      "vars": {}}}}}},
            {"id": "end", "title": "end", "type": "end", "config": {}},
        ], "edges": [{"source": "start", "target": "assigner"},
                     {"source": "assigner", "target": "end"}]})

    def test_a_broken_formula_fails_validation(self):
        errors = build_block_registry().validate_workflow(self._workflow("a.b + 1"))
        self.assertTrue([e for e in errors if "公式写不通" in e],
                        f"坏公式通过了发布校验：{errors}")

    def test_a_good_formula_does_not_add_errors(self):
        errors = build_block_registry().validate_workflow(self._workflow("1 + 2"))
        self.assertEqual([e for e in errors if "公式写不通" in e], [])

    def test_a_bare_string_formula_is_checked_too(self):
        """$formula 也可以直接给字符串，别只查 {expression} 那一种写法。"""
        from agent_platform.workflow_models import WorkflowSpec

        workflow = WorkflowSpec.model_validate({"nodes": [
            {"id": "start", "title": "start", "type": "start", "config": {"inputs": []}},
            {"id": "assigner", "title": "assigner", "type": "variable_assigner", "config": {
                "assignments": {"x": {"$formula": "a.b + 1"}}}},
            {"id": "end", "title": "end", "type": "end", "config": {}},
        ], "edges": [{"source": "start", "target": "assigner"},
                     {"source": "assigner", "target": "end"}]})
        errors = build_block_registry().validate_workflow(workflow)
        self.assertTrue([e for e in errors if "公式写不通" in e], errors)

    def test_the_error_names_the_node(self):
        errors = build_block_registry().validate_workflow(self._workflow("a.b + 1"))
        self.assertTrue([e for e in errors if e.startswith("assigner:")],
                        f"没说是哪个节点：{errors}")

    def test_a_workflow_without_formulas_is_unaffected(self):
        from agent_platform.workflow_models import WorkflowSpec

        workflow = WorkflowSpec.model_validate({"nodes": [
            {"id": "start", "title": "start", "type": "start", "config": {"inputs": []}},
            {"id": "end", "title": "end", "type": "end", "config": {}},
        ], "edges": [{"source": "start", "target": "end"}]})
        self.assertEqual(
            [e for e in build_block_registry().validate_workflow(workflow) if "公式" in e], [])


if __name__ == "__main__":
    unittest.main()


class DanglingReferenceTest(unittest.TestCase):
    """$ref 指向的节点在不在图里——静态可查，而发布校验一直没查。

    真机上最常见的失败族就是引用解析不了。其中"路径对不对"要看运行时的
    产出形状，静态查不了；但"这个节点存不存在"是纯静态的。
    引用一个根本不存在的节点，此前也能顺利发布。
    """

    @staticmethod
    def _workflow(reference):
        from agent_platform.workflow_models import WorkflowSpec

        return WorkflowSpec.model_validate({"nodes": [
            {"id": "start", "title": "开始", "type": "start", "config": {"inputs": []}},
            {"id": "assigner", "title": "赋值", "type": "variable_assigner",
             "config": {"assignments": {"x": {"$ref": reference}}}},
            {"id": "end", "title": "结束", "type": "end", "config": {}},
        ], "edges": [{"source": "start", "target": "assigner"},
                     {"source": "assigner", "target": "end"}]})

    def _errors(self, reference):
        return [e for e in build_block_registry().validate_workflow(self._workflow(reference))
                if "不存在的节点" in e]

    def test_a_dangling_reference_is_caught(self):
        self.assertTrue(self._errors({"node_id": "没有这个节点", "path": ["out"]}))

    def test_the_error_names_both_nodes(self):
        error = self._errors({"node_id": "没有这个节点", "path": ["out"]})[0]
        self.assertIn("assigner", error, "没说是哪个节点写错了")
        self.assertIn("没有这个节点", error, "没说它指向了谁")

    def test_a_real_node_passes(self):
        self.assertEqual(self._errors({"node_id": "start", "path": ["out"]}), [])

    def test_the_runtime_builtins_pass(self):
        """$inputs / $run 是运行时内置的名字，不是图里的节点。"""
        for builtin in ("$inputs", "$run"):
            self.assertEqual(self._errors({"node_id": builtin, "path": ["x"]}), [],
                             builtin)

    def test_an_optional_reference_may_dangle(self):
        """标了 optional 的引用允许指不到——运行时会回 None，是设计好的。"""
        self.assertEqual(
            self._errors({"node_id": "没有这个", "path": ["out"], "optional": True}), [])

    def test_a_node_may_reference_its_own_nested_workflow(self):
        """loop 的 break_value 取的就是它自己子图里输出节点的值——合法。

        第一版没考虑这个，把 7 条正常测试判红了。
        """
        from agent_platform.workflow_models import WorkflowSpec

        nested = {"nodes": [
            {"id": "loop-start", "title": "进", "type": "start",
             "config": {"inputs": [{"name": "i", "type": "number"}]}},
            {"id": "loop-end", "title": "出", "type": "end", "config": {"outputs": {
                "current": {"$ref": {"node_id": "loop-start", "path": ["i"]}}}}},
        ], "edges": [{"source": "loop-start", "target": "loop-end"}]}
        workflow = WorkflowSpec.model_validate({"nodes": [
            {"id": "start", "title": "开始", "type": "start", "config": {"inputs": []}},
            {"id": "loop", "title": "循环", "type": "loop", "config": {
                "workflow": nested, "variables": {},
                "break_condition": {"value": 0, "operator": "gte", "expected": 2},
                "break_value": {"$ref": {"node_id": "loop-end", "path": ["current"]}},
                "max_iterations": 5, "output_node_id": "loop-end"}},
            {"id": "end", "title": "结束", "type": "end", "config": {}},
        ], "edges": [{"source": "start", "target": "loop"},
                     {"source": "loop", "target": "end"}]})
        errors = [e for e in build_block_registry().validate_workflow(workflow)
                  if "不存在的节点" in e]
        self.assertEqual(errors, [], f"把合法的子图引用判成错了：{errors}")
