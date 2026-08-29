"""生成 Excel 的那 759 行，一条测试都没有。

2026-08-29 扫数值上限时发现的：`-k workbook` 一条都选不中，
全仓没有任何测试 import 过 typed_workbook。而它是生产代码——
blocks 注册了 `typed_workbook` 积木、workflow_runtime 会写产物、
record_pipeline 也在用。真机上暂时没有工作流用到它（发布版 0、草稿 0），
所以坏了也不会有人发现，直到第一个客户拿到一个打不开的表。

这里不求覆盖 759 行，只钉住三件**坏了就很难受**的事：
产物是不是合法 xlsx、公式注入进不进得去、规模上限有没有保护力。
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from agent_platform.typed_workbook import render_typed_workbook

SPEC = {"sheets": [{"name": "明细",
                    "columns": [{"key": "name", "header": "名称", "type": "string"},
                                {"key": "count", "header": "数量", "type": "integer"}],
                    "rows": [{"name": "甲", "count": 3},
                             {"name": "乙", "count": 5}]}]}


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


class TestItProducesAWorkbookThatOpens:
    """最基本的一件事：Excel 打得开。"""

    def test_the_bytes_are_a_sound_zip(self):
        archive = _zip(render_typed_workbook(SPEC))
        assert archive.testzip() is None

    def test_the_required_parts_are_there(self):
        """少一个部件，Excel 会说"文件已损坏"，而且不告诉你少了哪个。"""
        names = set(_zip(render_typed_workbook(SPEC)).namelist())
        for required in ("[Content_Types].xml", "_rels/.rels",
                         "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                         "xl/worksheets/sheet1.xml"):
            assert required in names, f"缺部件：{required}"

    def test_the_data_is_actually_in_there(self):
        """光"能打开"不够——里面得有数据。"""
        sheet = _zip(render_typed_workbook(SPEC)).read(
            "xl/worksheets/sheet1.xml").decode("utf-8")
        assert "名称" in sheet and "甲" in sheet
        assert ">3<" in sheet and ">5<" in sheet

    def test_two_renders_of_the_same_spec_are_identical(self):
        """实现自称 deterministic。不确定的话，同一份数据每次产出不同字节，
        比对、缓存、复现都无从谈起。"""
        assert render_typed_workbook(SPEC) == render_typed_workbook(SPEC)


class TestFormulasCannotGetIn:
    """表格公式注入：客户打开表就执行——这是这个模块最要紧的一条。"""

    LOOKS_LIKE_FORMULA = ["=1+1", "=cmd|'/c calc'!A1", "@SUM(1)", "+1", "-1"]

    def _spec(self, value):
        return {"sheets": [{"name": "s",
                            "columns": [{"key": "v", "header": "值",
                                         "type": "string"}],
                            "rows": [{"v": value}]}]}

    @pytest.mark.parametrize("value", LOOKS_LIKE_FORMULA)
    def test_by_default_they_are_refused(self, value):
        with pytest.raises(Exception) as caught:
            render_typed_workbook(self._spec(value))
        assert "formula" in str(caught.value).lower()

    @pytest.mark.parametrize("value", LOOKS_LIKE_FORMULA)
    def test_the_literal_policy_writes_them_as_text_not_formula(self, value):
        """`literal` 是明确选择"照原样存"，但也**只能存成文本**。

        判据是产物里不能出现 `<f>`：那才是 Excel 会去求值的东西。
        写成 `<is><t>` 的内容，Excel 当字符串看。
        """
        data = render_typed_workbook(self._spec(value), formula_policy="literal")
        sheet = _zip(data).read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<f>" not in sheet, f"{value} 变成了真公式"
        texts = re.findall(r"<t[^>]*>(.*?)</t>", sheet)
        assert any(value in t for t in texts), f"{value} 连文本都没存进去"

    def test_an_ordinary_value_is_not_mistaken_for_a_formula(self):
        """别把闸关死：正常内容不能因为像公式就被拒。"""
        for ordinary in ("甲乙丙", "1+1", "a=b", "3", "2026-08-29"):
            render_typed_workbook(self._spec(ordinary))


class TestTheSizeCapsHaveTeeth:
    """七个上限一条测试都没有。把它们全部拉大 100 倍，全仓无一变红。

    和公式引擎那次一样：只验"机制在"是不够的（用例从常量现算输入，
    常量一变输入也跟着变），得对**数值本身**设个上界。
    """

    def test_the_defaults_are_bounded(self):
        from agent_platform import typed_workbook as tw

        assert 0 < tw.MAX_WORKBOOK_SHEETS <= 256
        assert 0 < tw.MAX_WORKBOOK_COLUMNS <= 1_024
        assert 0 < tw.MAX_WORKBOOK_ROWS_PER_SHEET <= 200_000
        assert 0 < tw.MAX_WORKBOOK_CELLS <= 2_000_000
        assert 0 < tw.MAX_WORKBOOK_STRING_CHARS <= 100_000
        assert 0 < tw.MAX_WORKBOOK_TEXT_BYTES <= 50_000_000
        assert 0 < tw.MAX_WORKBOOK_BYTES <= 50_000_000

    def test_too_many_cells_is_refused(self):
        from agent_platform import typed_workbook as tw

        columns = [{"key": f"c{i}", "header": f"列{i}", "type": "integer"}
                   for i in range(50)]
        rows = [{f"c{i}": 1 for i in range(50)}
                for _ in range(tw.MAX_WORKBOOK_CELLS // 50 + 10)]
        with pytest.raises(Exception) as caught:
            render_typed_workbook({"sheets": [{"name": "s", "columns": columns,
                                               "rows": rows}]})
        assert "cell" in str(caught.value).lower() or "row" in str(caught.value).lower()

    def test_duplicate_sheet_names_are_refused(self):
        """Excel 里两个同名表打不开——大小写不同也算同名。"""
        one = {"name": "明细", "columns": [{"key": "a", "header": "A"}], "rows": []}
        with pytest.raises(Exception) as caught:
            render_typed_workbook({"sheets": [one, dict(one)]})
        assert "duplicate" in str(caught.value).lower()
