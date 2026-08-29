"""同因失败合并成一条——否则重复会把别的工作流的问题挤出屏幕。

回归背景（2026-08-28 真机）：recent_failures 8 条里 5 条同因，
客户端只显示前 5 条，另一个工作流的两个不同问题一条都没露面。
"""
import unittest

from agent_platform.overview import _dedupe_failures


def _fail(run_id, name, at, error):
    return {"id": run_id, "name": name, "at": at, "error": error}


class DedupeFailuresTest(unittest.TestCase):
    def test_same_cause_collapses_and_counts(self):
        rows = _dedupe_failures([
            _fail("aaaaaaaa11", "统计", "2026-08-28T06:00", "node start failed: missing required input: text"),
            _fail("bbbbbbbb22", "统计", "2026-08-28T05:00", "node start failed: missing required input: text"),
            _fail("cccccccc33", "统计", "2026-08-28T04:00", "node start failed: missing required input: text"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 3)
        # 留最近的那次：上游按时间倒序给，取第一条
        self.assertEqual(rows[0]["run_id"], "aaaaaaaa")
        self.assertEqual(rows[0]["at"], "2026-08-28T06:00")

    def test_distinct_problems_are_not_hidden(self):
        # 真机那一幕：5 条同因 + 另一个工作流的两个不同问题
        rows = _dedupe_failures(
            [_fail(f"x{i}0000000", "统计", f"2026-08-28T0{i}:00",
                   "node start failed: missing required input: text") for i in range(5)]
            + [_fail("y100000000", "日报", "2026-08-27T10:00",
                     "node start failed: missing required input: sales"),
               _fail("y200000000", "日报", "2026-08-27T09:00",
                     "node n failed: record_collection_normalize value must resolve")]
        )
        self.assertEqual(len(rows), 3)
        # 前 5 条的窗口里，两个工作流的问题都还在
        self.assertEqual({r["workflow"] for r in rows[:5]}, {"统计", "日报"})

    def test_same_workflow_different_causes_stay_apart(self):
        rows = _dedupe_failures([
            _fail("aaaaaaaa11", "日报", "2026-08-28T06:00",
                  "node start failed: missing required input: sales"),
            _fail("bbbbbbbb22", "日报", "2026-08-28T05:00",
                  "node agg failed: collection expression requires an array"),
        ])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["count"] == 1 for r in rows))

    def test_single_failure_still_carries_count(self):
        rows = _dedupe_failures([_fail("aaaaaaaa11", "统计", "2026-08-28T06:00", "boom")])
        self.assertEqual(rows[0]["count"], 1)

    def test_empty_input(self):
        self.assertEqual(_dedupe_failures([]), [])


class CountIsNotCappedBySqlLimitTest(unittest.TestCase):
    """×N 的次数不能被 SQL 的取数上限封顶。

    回归背景（2026-08-29 独立复查）：SQL 原本 `LIMIT 8`，合并却在 Python 里做，
    于是次数封顶在 8——线上真值 13 显示成 ×8，少报 38%，
    而同一个产品的 health-report 端点算得出 13，两个面板互相打脸。
    昨天那次「合并同因」只修了一半：显示合并了，取数没跟上。
    """

    def test_dedupe_counts_everything_it_is_given(self):
        rows = _dedupe_failures([
            _fail(f"r{i:04d}", "统计", f"2026-08-28T{i % 24:02d}:00",
                  "node start failed: missing required input: text")
            for i in range(13)])
        self.assertEqual(rows[0]["count"], 13)

    def test_a_rare_cause_is_not_pushed_out_by_repeats(self):
        # 20 条同因 + 1 条别的：合并后两条都要在
        rows = _dedupe_failures(
            [_fail(f"a{i:03d}", "统计", "2026-08-28T06:00",
                   "node start failed: missing required input: text")
             for i in range(20)]
            + [_fail("z1", "日报", "2026-08-27T06:00",
                     "node agg failed: collection expression requires an array")])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["workflow"] for r in rows}, {"统计", "日报"})
        self.assertEqual(rows[0]["count"], 20)


class CountsAreNotCappedTest(unittest.TestCase):
    """次数不能被"取回来多少条"限死——这个 bug 修过一次没修透。

    最早是 SQL LIMIT 8、合并在 Python 里做，于是次数封顶在 8
    （线上真值 13 显示成 ×8，少报 38%）。那次改成 LIMIT 500：
    数量级换了，病还是同一个——失败记录一超过 500 条，次数又开始少报，
    而且是**静默**的，面板上还是个数，只是变小了。

    现在聚合在 SQL 里做，每行自带 n；这里的合并必须**累加 n**。
    写成 +1 的话，一句人话下面藏着的几十次会被压成"几种"。
    """

    def test_the_upstream_count_is_added_not_counted_as_one(self):
        rows = _dedupe_failures([
            {**_fail("aaaaaaaa11", "统计", "2026-08-28T06:00", "boom"), "n": 900},
        ])
        self.assertEqual(rows[0]["count"], 900)

    def test_two_wordings_of_one_cause_add_up(self):
        """翻译会把不同原文归到同一句人话——那时两行的 n 要相加。"""
        rows = _dedupe_failures([
            {**_fail("aaaaaaaa11", "统计", "2026-08-28T06:00", "boom"), "n": 7},
            {**_fail("bbbbbbbb22", "统计", "2026-08-28T05:00", "boom"), "n": 5},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 12)

    def test_the_newest_occurrence_wins_whatever_the_order(self):
        """留的是最近那一次的时刻和编号，不能因为上游顺序变了就串。"""
        rows = _dedupe_failures([
            {**_fail("oldoldold1", "统计", "2026-08-20T01:00", "boom"), "n": 1},
            {**_fail("newnewnew2", "统计", "2026-08-28T09:00", "boom"), "n": 1},
        ])
        self.assertEqual(rows[0]["at"], "2026-08-28T09:00")
        self.assertEqual(rows[0]["run_id"], "newnewne")

    def test_rows_without_a_count_still_mean_one(self):
        """老调用方（和老测试）不带 n——不能因此算成 0。"""
        rows = _dedupe_failures([
            _fail("aaaaaaaa11", "统计", "2026-08-28T06:00", "boom"),
            _fail("bbbbbbbb22", "统计", "2026-08-28T05:00", "boom"),
        ])
        self.assertEqual(rows[0]["count"], 2)
