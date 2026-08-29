"""公式语法要在发布时查，不能等第一次真跑才炸。

回归背景（2026-08-29 真机报错清单）：

    node assigner failed: 公式包含不支持的字符 '.'（位置 8）

公式写在节点配置里，**静态可查**。可 validate_workflow 从头到尾不碰它，
于是写错的公式顺利发布，业主眼里是"东西发布了、跑起来却坏了"——
而这个错在发布那一刻就看得见。

只解析不求值：变量的值要到运行时才有，求值必然失败，那不是语法问题。
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
