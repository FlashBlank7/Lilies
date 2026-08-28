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


class ReferenceErrorIsNotReversedTest(unittest.TestCase):
    """线上最大一族失败（72 条）原本被翻**反**了。

    原文 could not resolve node='X' path=['f'] container_type=NoneType
    的意思是「取不到 X 的 f 这一项」——节点就在图上。
    原译文说「引用了不存在的节点「X」」，会把人引去找一个其实存在的节点，
    修的方向正好相反，而且把原文里唯一可执行的信息（哪个字段、为什么取不到）全丢了。
    """

    NONE_TYPE = ("node end failed: workflow reference could not resolve "
                 "node='template_transform' path=['text']; "
                 "failed_segment='text'; container_type=NoneType")
    DICT_TYPE = ("node end failed: workflow reference could not resolve "
                 "node='aggregator' path=['by_store']; "
                 "failed_segment='by_store'; container_type=dict")

    def test_it_no_longer_says_the_node_is_missing(self):
        for error in (self.NONE_TYPE, self.DICT_TYPE):
            self.assertNotIn("不存在的节点", _human_error(error), error[:60])

    def test_it_names_both_the_node_and_the_field(self):
        out = _human_error(self.NONE_TYPE)
        self.assertIn("template_transform", out)
        self.assertIn("text", out)

    def test_container_type_becomes_the_real_cause(self):
        self.assertIn("没有产出", _human_error(self.NONE_TYPE))
        self.assertIn("没有这一项", _human_error(self.DICT_TYPE))

    def test_container_type_survives_truncation(self):
        """判据要在未截断的原文上跑。

        _brief_error 砍到 110 字，而 container_type 恰好在那之后——
        先截再匹配就永远读不到真因（第一版就是这么错的）。
        """
        self.assertGreater(len(self.NONE_TYPE), 110)
        self.assertIn("没有产出", _human_error(self.NONE_TYPE))

    def test_without_container_type_it_still_reads_well(self):
        out = _human_error("node x failed: workflow reference could not "
                           "resolve node='n' path=['p']")
        self.assertIn("取不到", out)
        self.assertIn("n", out)
