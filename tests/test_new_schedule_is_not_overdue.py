"""刚发布的定时工作流，不该立刻被判「有定时却没跑起来」。

回归背景（2026-08-29）：_overdue 里写着

    if not last_fired:
        return True, label          # 有定时、从没开过火

下午两点发布一个「每天 8:00」的工作流：last_fired 是空，
而"上一次该开火的时刻"算出来是今早 8 点——于是它刚设好就被判逾期。
用户前脚发布，后脚看见面板说它没跑起来。

本该开火的那一炮响的时候它还没上线，那一炮不算它的账。
"""
import unittest
from datetime import datetime, timedelta, timezone

from agent_platform.overview import _overdue

DAILY_8 = {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}
NOW = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)      # 北京时间 14:00
BEFORE_EXPECTED = "2026-08-01T00:00:00+00:00"
AFTER_EXPECTED = "2026-08-29T05:30:00+00:00"                # 北京 13:30 才上线


class NewScheduleTest(unittest.TestCase):
    def test_published_after_todays_fire_is_not_overdue(self):
        overdue, _ = _overdue(DAILY_8, None, NOW, published_at=AFTER_EXPECTED)
        self.assertFalse(overdue, "刚发布就被判逾期")

    def test_published_before_todays_fire_and_never_fired_is_overdue(self):
        """昨天就上线了却一次没开过火——那是真有问题。"""
        overdue, _ = _overdue(DAILY_8, None, NOW, published_at=BEFORE_EXPECTED)
        self.assertTrue(overdue)

    def test_a_missed_fire_is_still_caught(self):
        """修误报不能把真问题一起放过。"""
        overdue, _ = _overdue(DAILY_8, "2026-08-28T00:00:00+00:00", NOW,
                              published_at=BEFORE_EXPECTED)
        self.assertTrue(overdue)

    def test_a_normal_fire_today_is_not_overdue(self):
        overdue, _ = _overdue(DAILY_8, "2026-08-29T00:00:00+00:00", NOW,
                              published_at=BEFORE_EXPECTED)
        self.assertFalse(overdue)

    def test_without_a_publish_time_the_old_behaviour_stands(self):
        """取不到发布时刻就按老规矩判——宁可误报，也别把真逾期漏了。"""
        overdue, _ = _overdue(DAILY_8, None, NOW)
        self.assertTrue(overdue)

    def test_a_broken_publish_time_does_not_crash(self):
        for bad in ("不是时间", "", "2026-13-45T99:99:99"):
            overdue, _ = _overdue(DAILY_8, None, NOW, published_at=bad)
            self.assertTrue(overdue, bad)

    def test_a_naive_publish_time_is_treated_as_utc(self):
        """库里存的时间戳有的不带时区，不能因此判错。"""
        overdue, _ = _overdue(DAILY_8, None, NOW, published_at="2026-08-29T05:30:00")
        self.assertFalse(overdue)

    def test_the_label_still_says_when_it_should_have_fired(self):
        _, label = _overdue(DAILY_8, None, NOW, published_at=AFTER_EXPECTED)
        self.assertIn("2026-08-29 00:00", label)


# ── 接线：体检真跑一遍，看刚发布的定时会不会被判 stale ──
#
# 初稿这里写的是 inspect.getsource + 查有没有 "published_at" 这个串，
# 自己删了——那是断言源码长什么样，换个写法就骗过去了。

import json  # noqa: E402

import pytest  # noqa: E402

from agent_platform.overview import build_health  # noqa: E402

from helpers_overview import services  # noqa: E402,F401  (pytest fixture)

SCHEDULED = json.dumps({"name": "日报", "workflow": {"nodes": [
    {"id": "s", "type": "schedule_trigger",
     "config": {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}}]}})


def _publish(services, app_id, created_at):  # noqa: F811
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES(?,?,'','','workflow',1,datetime('now'),datetime('now'))",
            (app_id, app_id))
        conn.execute(
            "INSERT INTO application_versions(application_id,version,snapshot_json,"
            "content_hash,validation_report_json,created_at) "
            "VALUES(?,1,?,?,'{}',?)",
            (app_id, SCHEDULED, "h" * 64, created_at))


@pytest.mark.asyncio
async def test_a_workflow_published_moments_ago_is_not_stale(services):  # noqa: F811
    """刚发布的定时不该被判「有定时却没跑起来」。"""
    _publish(services, "brand-new", datetime.now(timezone.utc).isoformat())
    report = await build_health(services)
    item = next(i for i in report["items"] if i["workflow"] == "brand-new")
    assert not item["overdue"], "刚发布就被判逾期了"
    assert item["state"] != "stale"


@pytest.mark.asyncio
async def test_an_old_schedule_that_never_fired_is_still_stale(services):  # noqa: F811
    """修误报不能把真问题一起放过：上线一个月没开过火，那是真有问题。"""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _publish(services, "long-dead", old)
    report = await build_health(services)
    item = next(i for i in report["items"] if i["workflow"] == "long-dead")
    assert item["overdue"], "真逾期的被放过了"
