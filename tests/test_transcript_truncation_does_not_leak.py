"""搭建 transcript：截断和脱敏的**顺序**，以及那条 20 万字符的线。

来由（2026-08-29 实测）：`tool_call_record` 原来是先截到 20 万字符、
再脱敏。秘密正好跨在那条线上时，尾巴被切掉，剩下的头部短于
`sk-` + 16 位那个正则的要求，于是匹配不上、**原样留在 transcript 里**。
漏出窗口是"幸存 1–18 个字符"——最多 sk- 加 15 位明文落盘。
顺序反过来（先脱敏再截断）就没有这一段。

MAX_TOOL_RESULT_CHARS 本身此前也没有测试：把它乘以 100，
全量跑下来一条不红。它是防"某个工具吐出一坨天文数字大小的结果"的阀门。

真机量过（2656 条工具结果记录）：最大 14,172 字符、中位 280、
95 分位 4,607，**一条都没被截断过**。所以这个阀门至今没响过——
这里测的是它响的时候对不对，不是它常响。
"""

from __future__ import annotations

import pytest

from agent_platform.build_transcript import (
    MAX_TOOL_RESULT_CHARS,
    redact,
    tool_call_record,
)

SECRET = "sk-" + "A" * 40


def _record(result: str) -> dict:
    return tool_call_record(name="t", arguments={}, result=result, is_error=False)


class TestASecretOnTheCutLineIsStillHidden:
    def test_a_whole_secret_is_redacted_at_all(self):
        """前提。这一条不成立的话，下面每条"没漏"都可能是脱敏器整个坏了。"""
        assert "[REDACTED]" in redact(f"前面 {SECRET}")

    @pytest.mark.parametrize("survives", [1, 3, 5, 10, 18, 19, 25, 43, 60])
    def test_no_fragment_survives_wherever_the_cut_falls(self, survives):
        """把秘密的开头挪到距上限 N 个字符处，逐个位置看。

        1–18 那几个位置正是原实现漏的：幸存部分太短，正则够不着。
        用空格填充，不是 'x'——'x' 会让 `\\b` 匹配不上，
        那样"没漏"是填充造成的假象（第一版就这么骗过自己一次）。
        """
        padding = " " * (MAX_TOOL_RESULT_CHARS - survives)
        record = _record(padding + SECRET + "y" * 1_000)
        assert "sk-A" not in record["result"], record["result"].rstrip()[-40:]

    def test_a_secret_well_inside_the_limit_is_redacted(self):
        """反向那一条：不在边界上的秘密照样要被换掉。"""
        record = _record(f"用的是 {SECRET} 这个")
        assert "[REDACTED]" in record["result"]
        assert "sk-A" not in record["result"]


class TestTheCapHasTeeth:
    def test_the_number_itself_is_sane(self):
        """绝对值也钉一下。

        只写"截到 MAX 个字符"这种从常量推出来的断言，
        常量被人乘以 100 时它照样绿——测的是机制，不是这个数。
        （同一个坑今天在 formula 和 table_intake 上都踩过。）
        """
        assert 10_000 <= MAX_TOOL_RESULT_CHARS <= 1_000_000

    def test_a_huge_result_is_cut_and_says_so(self):
        record = _record("z" * (MAX_TOOL_RESULT_CHARS + 5_000))
        assert len(record["result"]) == MAX_TOOL_RESULT_CHARS
        assert record["truncated"] is True

    def test_a_normal_result_is_untouched(self):
        """真机上 2656 条记录里最大才 14,172 字符——常态是这一条。"""
        body = "正常的工具结果" * 100
        record = _record(body)
        assert record["result"] == body
        assert record["truncated"] is False

    def test_a_result_exactly_at_the_limit_is_not_called_truncated(self):
        """正好等于上限：一个字都没丢，别说成截断了。"""
        record = _record("z" * MAX_TOOL_RESULT_CHARS)
        assert record["truncated"] is False
        assert len(record["result"]) == MAX_TOOL_RESULT_CHARS

    def test_the_flag_describes_what_was_actually_cut(self):
        """脱敏会让串变短（一长串秘密变成 [REDACTED]）。

        "截没截断"说的是**存下来的这份**丢没丢东西，
        所以要按脱敏之后的长度判，不是按原始长度。
        原始长度过线、脱敏后没过线，就不该说成截断了。
        """
        many = (SECRET + " ") * 6_000          # 原始远超上限
        record = _record(many)
        assert len(redact(many)) < MAX_TOOL_RESULT_CHARS, "前提：脱敏后缩到了限内"
        assert record["truncated"] is False
        assert "sk-A" not in record["result"]
