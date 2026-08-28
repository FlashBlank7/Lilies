"""运行失败的原因给业主看之前要换成人话，但关键名字得留着。

回归背景（2026-08-28 真机 today 面板）：
  ✕ 文本行数与净字数统计  missing required input: text
  ✕ 日报                  collection expression requires an array
  ✕ 日报                  'g'      ← 裸 KeyError，业主完全看不懂
"""
import re
import unittest

from agent_platform.overview import _human_error

ENGLISH_RUN = re.compile(r"[a-z]{3,}\s+[a-z]{3,}")


class RunErrorWordsTest(unittest.TestCase):
    def test_specifics_survive_the_translation(self):
        # 缺哪个输入、引用了哪个节点——业主正是拿这个名字去改东西的
        for error, keep in (
            ("node start failed: missing required input: text", "text"),
            ("node start failed: missing required input: sales", "sales"),
            ("node end failed: workflow reference could not resolve node='aggregator'",
             "aggregator"),
            ("node render_report failed: 'g'", "g"),
        ):
            human = _human_error(error)
            self.assertIn(keep, human, error)
            self.assertIsNone(ENGLISH_RUN.search(human.lower()), human)

    def test_common_english_causes_are_translated(self):
        for error in (
            "node aggregator failed: collection expression requires an array",
            "node normalizer failed: record_collection_normalize value must resolve to records",
            "node fetch failed: Connection refused",
        ):
            human = _human_error(error)
            self.assertIsNone(ENGLISH_RUN.search(human.lower()), human)
            self.assertTrue(human.strip())

    def test_chinese_node_messages_pass_through_untouched(self):
        # 节点自己抛的中文说明比任何模板都具体，别拿模板盖掉
        for error in (
            "node sum_by_store failed: 公式包含不支持的字符 '$'（位置 7）",
            'node aggregator failed: 操作符 $formula 不能和其它键混在同一个对象里',
            'node sum_by_store failed: sum_by(记录数组, "分组字段", "数值字段") 需要一个对象数组',
        ):
            # 原文的实质内容必须原样还在
            tail = error.split("failed: ", 1)[1]
            self.assertEqual(_human_error(error), tail[:110])

    def test_empty_stays_empty(self):
        self.assertEqual(_human_error(""), "")
        self.assertEqual(_human_error("   "), "")
