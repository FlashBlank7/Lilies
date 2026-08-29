"""给搭建模型看的配置报错，不该是 pydantic 的英文原文。

回归背景（2026-08-29，统计 76 份真机搭建 transcript）：
被拒的工具调用里，pydantic 英文报错出现 8 种、共 140 次：

    ×76  Input should be a valid list
    ×53  Extra inputs are not permitted
    ×4   Input should be a valid dictionary or instance of NodeSpec
    …

「Extra inputs are not permitted」尤其误导：模型看不出是自己把
outputs 写在了节点顶层而不是 config 里。搭建失败的头号原因是
"反复提同一个被拒的方案"（40%），而它拿到的就是这类看不懂的话。

只翻真机上真出现过的句式。没出现过的原样保留——
胡乱翻一堆没见过的，只会在真出现时翻错。
"""
import re
import unittest

from agent_platform.blocks import _zh_pydantic

ENGLISH = re.compile(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}")

# 真机上出现过的全部 8 种（按次数排序）
REAL = [
    "Input should be a valid list",
    "Extra inputs are not permitted",
    "Input should be a valid dictionary or instance of NodeSpec",
    "Input should be 'first_non_null', 'array' or 'merge'",
    "Value error, unbounded wildcard regex quantifiers are not supported",
    "Input should be a valid dictionary",
    "Value error, a regex whose first variable repeat has no start anchor",
    "Value error, record paths must contain paths of 1 to 16 segments",
]


class ZhPydanticTest(unittest.TestCase):
    def test_the_two_most_common_are_translated(self):
        self.assertEqual(_zh_pydantic("Input should be a valid list"), "这里要一个数组")
        self.assertIn("层级放错", _zh_pydantic("Extra inputs are not permitted"))

    def test_extra_inputs_says_what_it_actually_means(self):
        """模型看不出是自己把字段放错了层级——这句得说出来。"""
        text = _zh_pydantic("Extra inputs are not permitted")
        self.assertNotIn("permitted", text)
        self.assertIn("不属于这里", text)

    def test_an_enum_keeps_the_allowed_values(self):
        text = _zh_pydantic("Input should be 'first_non_null', 'array' or 'merge'")
        self.assertIn("first_non_null", text, "把可选值弄丢了，模型没法改")
        self.assertNotIn("：：", text, "冒号重复了")

    def test_a_value_error_prefix_is_dropped_not_the_content(self):
        text = _zh_pydantic("Value error, 每条路径要有 1 到 16 段")
        self.assertEqual(text, "每条路径要有 1 到 16 段")

    def test_an_unseen_message_is_left_alone(self):
        """没见过的句式原样保留，别乱翻。"""
        odd = "Some brand new pydantic message"
        self.assertEqual(_zh_pydantic(odd), odd)

    def test_no_translation_is_empty(self):
        for message in REAL:
            self.assertTrue(_zh_pydantic(message).strip(), message)

    def test_the_frequent_ones_have_no_english_prose_left(self):
        """前两种占了 140 次里的 129 次，它们必须干净。"""
        for message in REAL[:2]:
            self.assertIsNone(ENGLISH.search(_zh_pydantic(message)), message)


class RecordPathErrorsTest(unittest.TestCase):
    """记录路径的报错是**我们自己**写的英文，不是 pydantic 的。

    record_collection_normalize 的配置报错真机上出现 43 次，
    尾巴上挂着 "must contain paths of 1 to 16 segments"。
    """

    def test_a_too_deep_path_explains_the_shape(self):
        from agent_platform.record_pipeline import _validate_paths

        with self.assertRaises(ValueError) as caught:
            _validate_paths([[]], label="候选记录路径")
        message = str(caught.exception)
        self.assertIn("候选记录路径", message)
        self.assertIn("段", message)
        self.assertIsNone(ENGLISH.search(message), message)

    def test_a_bad_segment_type_says_what_is_allowed(self):
        from agent_platform.record_pipeline import _validate_paths

        with self.assertRaises(TypeError) as caught:
            _validate_paths([[1.5]], label="候选记录路径")
        message = str(caught.exception)
        self.assertIn("字段名", message)
        self.assertIsNone(ENGLISH.search(message), message)


if __name__ == "__main__":
    unittest.main()
