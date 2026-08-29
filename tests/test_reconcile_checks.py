"""对账脚本自己也得能变红。

一个只会打勾的检查比没有检查更糟——它让人以为查过了。
scripts/reconcile_endpoints.py 的比较逻辑抽在 _checks 里，这里直接喂
对不上的载荷，确认它真的报出来。

（脚本本身在真机上验过一次：把 published_workflows 改成把草稿也算上，
  三条当场变红、退出码 1。这里补的是不依赖真机、能进门禁的那一半。）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from reconcile_endpoints import _checks  # noqa: E402


def _payload(**overrides):
    overview = {
        "published_workflows": 3,
        "runs_today": {"total": 5, "succeeded": 4, "failed": 1, "running": 0},
        "week": [{"date": "2026-08-28", "ok": 10, "fail": 5, "other": 0}],
        "week_failures": [{"day": "2026-08-28", "workflow": "甲", "failed": 5}],
        "recent_failures": [{"workflow": "甲"}],
        "recent_failures_total": 1,
    }
    overview.update(overrides.pop("overview", {}))
    for key in overrides.pop("drop", ()):      # 真的删掉，而不是覆盖成别的值
        overview.pop(key, None)
    apps = overrides.pop("apps", [{"active_version": 1}] * 3)
    # items 里带上 ever_ran：夹具比真载荷瘦，测的就不是真载荷。
    # （2026-08-29 加"还没跑过"那条检查时，空 dict 的夹具让它误报了一次。）
    health = overrides.pop("health", {
        "items": [{"workflow": f"w{i}", "ever_ran": True} for i in range(3)],
        "never_ran": [],
    })
    db = overrides.pop("db", {"published": 3, "runs_today": 5})
    def one(sql: str):
        return db["published"] if "FROM applications" in sql else db["runs_today"]
    return _checks(overview, apps, health, one)


def _mismatches(rows):
    return [name for name, left, _, right, _ in rows if left != right]


class ReconcileChecksTest(unittest.TestCase):
    def test_a_consistent_payload_has_no_mismatch(self):
        self.assertEqual(_mismatches(_payload()), [])

    def test_it_notices_when_the_panel_and_the_list_disagree(self):
        rows = _payload(apps=[{"active_version": 1}])       # 列表只有 1 个
        self.assertIn("已发布工作流数", _mismatches(rows))

    def test_it_notices_when_the_panel_and_the_database_disagree(self):
        rows = _payload(db={"published": 9, "runs_today": 5})
        self.assertIn("已发布工作流数（对库）", _mismatches(rows))

    def test_it_notices_when_health_covers_a_different_set(self):
        rows = _payload(health={"items": [{}]})
        self.assertIn("体检覆盖的工作流数", _mismatches(rows))

    def test_it_notices_when_todays_parts_do_not_add_up(self):
        """成 + 败 + 在跑 ≠ 总数：说明有一类状态在面板上凭空消失了。"""
        rows = _payload(overview={"runs_today": {"total": 5, "succeeded": 2,
                                                 "failed": 1, "running": 0}})
        self.assertIn("今日成败之和 = 今日总数", _mismatches(rows))

    def test_it_notices_when_the_week_total_cannot_be_accounted_for(self):
        """真机上发生过的那次：数字在，出处查不到。"""
        rows = _payload(overview={"week_failures": [
            {"day": "2026-08-28", "workflow": "甲", "failed": 2}]})
        self.assertIn("近7日失败总数 = 拆到工作流之和", _mismatches(rows))

    def test_it_notices_when_the_truncated_list_does_not_match_the_total(self):
        rows = _payload(overview={"recent_failures": [{"workflow": "甲"}],
                                  "recent_failures_total": 20})
        self.assertIn("失败清单条数 = 种类总数截到 8", _mismatches(rows))

    def test_a_missing_total_falls_back_instead_of_crying_wolf(self):
        """老远端没有 recent_failures_total——别凭空报一个不存在的不一致。

        这条第一版是**空断言**：_payload 的 overview 参数是 update 合并，
        默认里已经有 recent_failures_total=1，"没有这个字段"从来没被构造出来。
        变异（把兜底从 listed_kinds 改成 0）当场逃掉。要真删掉那个键才算测。
        """
        rows = _payload(drop=["recent_failures_total"])
        self.assertNotIn("失败清单条数 = 种类总数截到 8", _mismatches(rows))


if __name__ == "__main__":
    unittest.main()


class NeverRanChecksTest(unittest.TestCase):
    """「还没跑过」那两条：既要抓得住不一致，也不能对老后端误报。"""

    def _health(self, **kwargs):
        """只想动体检那部分，但工作流个数得跟着一起对齐——
        否则「体检覆盖的工作流数」那条会先红，测的就不是我要测的东西了。"""
        count = len(kwargs.get("items", []))
        return _payload(health=kwargs,
                        apps=[{"active_version": 1}] * count,
                        overview={"published_workflows": count},
                        db={"published": count, "runs_today": 5})

    def test_it_notices_when_the_count_disagrees(self):
        rows = self._health(
            items=[{"workflow": "甲", "ever_ran": False},
                   {"workflow": "乙", "ever_ran": True}],
            never_ran=[])
        self.assertIn("还没跑过的个数 = items 里 ever_ran 为假的个数",
                      _mismatches(rows))

    def test_it_notices_a_name_that_is_not_in_the_list(self):
        rows = self._health(
            items=[{"workflow": "甲", "ever_ran": False}],
            never_ran=["甲", "查无此人"])
        self.assertIn("还没跑过的都在体检名单里", _mismatches(rows))

    def test_a_consistent_never_ran_payload_is_clean(self):
        rows = self._health(
            items=[{"workflow": "甲", "ever_ran": False},
                   {"workflow": "乙", "ever_ran": True}],
            never_ran=["甲"])
        self.assertEqual(_mismatches(rows), [])

    def test_an_old_backend_without_the_field_is_not_flagged(self):
        """老后端没有 ever_ran / never_ran——不能因此报一堆假警。

        写成 `not i.get("ever_ran")` 的话这条会红：每一项都被算成"没跑过"。
        检查自己误报，比不检查更消耗人。
        """
        rows = self._health(items=[{"workflow": "甲"}, {"workflow": "乙"}])
        self.assertEqual(_mismatches(rows), [])
