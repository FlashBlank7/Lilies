"""「今天」是 UTC 的今天，不一定是业主的今天——差一天时要说清楚。

面板所有按天的数都用 UTC 日期切（date_utc / week / week_failures），
而这台服务器在 UTC+9。每天 00:00–09:00 本地时段里，
「今天跑了几次」答的其实是昨天；而管家会一句
「今天（8月29日）跑了 1 次」说得斩钉截铁，业主的今天却是 30 号。

切换口径要把面板上每一个按天的数字都挪一遍——那是产品决定，不顺手改。
能立刻做的是**别再说得那么肯定**：两个日期都给它，差一天时自己说清楚。

判定函数收一个可注入的 local_now：不然这段逻辑一天里只有某几个小时
能被测到，另外那几个小时的分支永远是靠运气。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_platform.assistant_agent import _day_scope_note
from helpers_overview import _seed, services  # noqa: F401


def _at(offset_hours: int, utc_moment: str = "2026-08-30T01:30:00+00:00"):
    return datetime.fromisoformat(utc_moment).astimezone(
        timezone(timedelta(hours=offset_hours)))


class TestTheNoteIsAlwaysThere:
    def test_it_names_the_calendar(self):
        note = _day_scope_note("2026-08-30", _at(9))
        assert "UTC" in note["按天的数字是按哪个日期切的"]
        assert "2026-08-30" in note["按天的数字是按哪个日期切的"]

    def test_it_reports_the_server_local_time(self):
        note = _day_scope_note("2026-08-30", _at(9))
        assert note["服务器本地现在是"].startswith("2026-08-30 10:30")


class TestTheWarningOnlyWhenItMatters:
    def test_same_day_says_nothing_extra(self):
        """UTC 01:30 在 +09 是同一天的 10:30——没必要啰嗦。

        多余的提醒会让真有事的时候没人看。
        """
        assert "注意" not in _day_scope_note("2026-08-30", _at(9))

    def test_a_local_day_behind_utc_is_flagged(self):
        """UTC 01:30 在 -08 还是 8月29日——业主的今天比面板的今天早一天。"""
        note = _day_scope_note("2026-08-30", _at(-8))
        assert "注意" in note
        assert "2026-08-29" in note["注意"] and "2026-08-30" in note["注意"]

    def test_a_local_day_ahead_of_utc_is_flagged(self):
        """UTC 23:30 在 +09 已经是第二天了——这正是 UTC+9 每天遇到的那一段。"""
        note = _day_scope_note("2026-08-29",
                               _at(9, "2026-08-29T23:30:00+00:00"))
        assert "注意" in note
        assert "2026-08-30" in note["注意"]

    def test_the_warning_says_what_to_do_about_it(self):
        note = _day_scope_note("2026-08-30", _at(-8))
        assert "说「今天」时" in note["注意"]


class TestTheDefaultPathIsRealToo:
    """不注入时走的那条路也得测。

    上面每条都塞了 local_now，于是 `datetime.now().astimezone()` 这一句
    从来没被跑过——把 .astimezone() 删掉（本地时间当成 UTC 用），
    七条测试照样全绿。变异验证当场发现的空档。
    """

    def test_without_injection_the_time_is_timezone_aware(self):
        note = _day_scope_note("2026-08-30")
        # 朴素时间的 %Z 是空串；带时区的会给出 JST / UTC / CST 之类
        tail = note["服务器本地现在是"].split()
        assert len(tail) == 3, f"没有时区名，说明用的是朴素时间：{note}"
        assert tail[2].strip(), note

    def test_without_injection_it_matches_the_real_local_clock(self):
        from datetime import datetime as _dt

        note = _day_scope_note("2026-08-30")
        assert note["服务器本地现在是"][:10] == _dt.now().strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_the_model_actually_receives_it(services):
    """算出来没端上去等于没算。"""
    from agent_platform.assistant_agent import WorkflowConcierge

    _seed(services, real_runs=[])
    result = await WorkflowConcierge(services, settings=None)._exec(
        "platform_overview", {}, user={})
    assert "日期口径" in result
    assert "UTC" in result["日期口径"]["按天的数字是按哪个日期切的"]
