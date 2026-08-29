"""数据处理积木的那批上限：21 个常量，测试里一个都没提过。

record_pipeline 里的 MAX_JSON_DEPTH / MAX_JSON_NODES / MAX_CONTAINER_ITEMS /
MAX_STRING_CHARS / MAX_REGEX_TEXT_CHARS …… 一共 21 个，
`grep` 全部 tests 一次都没出现（2026-08-29）。

这些不是风格约束，是**挡住"一份输入把进程拖垮"**的闸：
工作流处理的是业主给的数据，深嵌套 JSON、几十万项的数组、
超长文本喂给正则，都是现实里会发生的形状。

实测过：闸本身是好的（拦得住 37 层嵌套、5010 项数组、26 万字符的串）。
补的是"它坏了会有人知道"。

每类都配：超一点点要拦、限内要放行、常量本身有个绝对值范围
（只写"超过 MAX 就拦"这种从常量推出来的断言，常量被乘以 100 时照样绿——
今天在 formula、table_intake、build_transcript 上都踩过）。
"""

from __future__ import annotations

import pytest

from agent_platform.record_pipeline import (
    MAX_CONTAINER_ITEMS,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_REGEX_TEXT_CHARS,
    MAX_STRING_CHARS,
    RegexExtractField,
    _validate_json_value,
    extract_regex_fields,
)


def _nested(levels: int) -> dict:
    root: dict = {}
    current = root
    for _ in range(levels):
        current["k"] = {}
        current = current["k"]
    return root


class TestTheNumbersThemselvesAreSane:
    """绝对值也钉一下——常量被人放大时上面那些"超限要拦"照样绿。"""

    @pytest.mark.parametrize("name, value, low, high", [
        ("MAX_JSON_DEPTH", MAX_JSON_DEPTH, 8, 128),
        ("MAX_JSON_NODES", MAX_JSON_NODES, 1_000, 500_000),
        ("MAX_CONTAINER_ITEMS", MAX_CONTAINER_ITEMS, 100, 100_000),
        ("MAX_STRING_CHARS", MAX_STRING_CHARS, 4_096, 4_000_000),
        ("MAX_REGEX_TEXT_CHARS", MAX_REGEX_TEXT_CHARS, 1_024, 1_000_000),
    ])
    def test_it_is_in_a_reasonable_range(self, name, value, low, high):
        assert low <= value <= high, f"{name}={value}"


class TestJsonShapesThatCouldWedgeTheProcess:
    def test_too_deep_is_refused(self):
        with pytest.raises(ValueError, match="nested JSON levels"):
            _validate_json_value(_nested(MAX_JSON_DEPTH + 5), label="x")

    def test_just_inside_the_depth_is_allowed(self):
        """反向那一条。少了它，"一律拒绝"也能让上面全绿——
        而那会让所有嵌套数据都处理不了。"""
        _validate_json_value(_nested(MAX_JSON_DEPTH - 2), label="x")

    def test_too_many_items_in_one_container_is_refused(self):
        with pytest.raises(ValueError, match="exceeds"):
            _validate_json_value({"a": list(range(MAX_CONTAINER_ITEMS + 10))},
                                 label="x")

    def test_a_normal_sized_container_is_allowed(self):
        _validate_json_value({"a": list(range(50))}, label="x")

    def test_a_huge_string_is_refused(self):
        with pytest.raises(ValueError, match="characters"):
            _validate_json_value("x" * (MAX_STRING_CHARS + 10), label="x")

    def test_an_ordinary_string_is_allowed(self):
        _validate_json_value("hello", label="x")

    def test_the_label_is_carried_into_the_message(self):
        """报错要说清是哪一份数据出的事，不然业主不知道去改哪儿。"""
        with pytest.raises(ValueError, match="业主上传的表"):
            _validate_json_value(_nested(MAX_JSON_DEPTH + 5), label="业主上传的表")

    def test_the_path_is_pointed_at(self):
        """深处的问题要指出位置——只说"太大了"没法定位。"""
        with pytest.raises(ValueError, match=r"\$\.a\.b"):
            _validate_json_value({"a": {"b": list(range(MAX_CONTAINER_ITEMS + 10))}},
                                 label="x")


class TestRegexInputIsBounded:
    # 模式必须带起始锚或字面前缀——见下面那个类，那是另一道闸
    FIELDS = [RegexExtractField(name="n", pattern=r"id=(\d+)", required=False)]

    def test_text_over_the_limit_is_refused(self):
        with pytest.raises(ValueError, match="character limit"):
            extract_regex_fields("x" * (MAX_REGEX_TEXT_CHARS + 1), self.FIELDS)

    def test_text_inside_the_limit_works(self):
        got = extract_regex_fields("abc id=42 def", self.FIELDS)
        assert got["fields"]["n"] == "42", got

    def test_a_non_string_is_a_type_error_not_a_crash(self):
        """业主的数据可能是任何形状——报清楚，别让它变成别处的怪异错。"""
        with pytest.raises(TypeError, match="must resolve to a string"):
            extract_regex_fields({"not": "text"}, self.FIELDS)

    def test_multibyte_text_is_measured_in_bytes_too(self):
        """字符数没超、字节数可能超（一个汉字三字节）——两把尺子都要有。"""
        text = "中" * (MAX_REGEX_TEXT_CHARS - 1)
        assert len(text) <= MAX_REGEX_TEXT_CHARS
        assert len(text.encode("utf-8")) <= MAX_REGEX_TEXT_CHARS * 4
        extract_regex_fields(text, self.FIELDS)      # 这个规模仍然是允许的


class TestCatastrophicPatternsAreRefusedUpFront:
    """正则本身也是攻击面：业主写的模式会跑在平台进程里。

    两道闸（实测出来的，不是从注释读的）：
    · 开头就是变长重复且无锚点、无字面前缀 → 拒（`(\\d+)` 这种）
    · 嵌套量词 → 拒（`(a+)+` 这种，经典的灾难性回溯）
    """

    @pytest.mark.parametrize("pattern", [r"(\d+)", r"(\w*)"])
    def test_an_unanchored_leading_repeat_is_refused(self, pattern):
        with pytest.raises(Exception, match="anchor|prefix"):
            RegexExtractField(name="n", pattern=pattern, required=False)

    @pytest.mark.parametrize("pattern", [r"(a+)+b", r"^(a+)+$"])
    def test_a_quantified_group_is_refused(self, pattern):
        """(a+)+ 是灾难性回溯的教科书例子。"""
        with pytest.raises(Exception, match="quantified"):
            RegexExtractField(name="n", pattern=pattern, required=False)

    @pytest.mark.parametrize("pattern", [r"^(\d+)", r"id=(\w+)", r"金额[:：]\s*(\d+)"])
    def test_an_ordinary_anchored_pattern_is_allowed(self, pattern):
        """反向那一批：不然"全都拒"会让这个积木没法用。"""
        assert RegexExtractField(name="n", pattern=pattern, required=False)
