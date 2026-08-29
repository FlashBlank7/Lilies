"""表格进料：普通用户的 Excel/CSV/粘贴必须变成干净的记录数组。"""

from __future__ import annotations

import io
import zipfile

import pytest

from agent_platform.table_intake import TableIntakeError, parse_table


def _tiny_xlsx() -> bytes:
    shared = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" count="3" uniqueCount="3">'
        "<si><t>单号</t></si><si><t>金额</t></si><si><t>PO-001</t></si></sst>"
    )
    sheet = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1200.5</v></c></row>'
        '<row r="3"><c r="A3"><v>2026</v></c><c r="B3"><v>88</v></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_xlsx_first_sheet_headers_and_typed_cells() -> None:
    result = parse_table("流水.xlsx", data=_tiny_xlsx())
    assert result["columns"] == ["单号", "金额"]
    assert result["rows"] == [
        {"单号": "PO-001", "金额": 1200.5},
        {"单号": 2026, "金额": 88},
    ]


def test_pasted_tsv_and_csv() -> None:
    tsv = "sku\t库存\nA-100\t60\nB-200\t80\n"
    result = parse_table("paste.txt", text=tsv)
    assert result["columns"] == ["sku", "库存"]
    assert result["rows"][0] == {"sku": "A-100", "库存": 60}

    csv_text = "名称,数量\n轴承,3\n"
    assert parse_table("d.csv", data=csv_text.encode("utf-8"))["rows"] == [
        {"名称": "轴承", "数量": 3}
    ]


def test_human_errors() -> None:
    with pytest.raises(TableIntakeError) as empty:
        parse_table("x.csv", text="   ")
    assert "空" in str(empty.value)
    with pytest.raises(TableIntakeError) as bad:
        parse_table("x.xlsx", data=b"not a zip")
    assert "Excel" in str(bad.value)


# ── 规模上限：客户会粘一大坨 ──
#
# 变异验证（2026-08-29，禁写字节码后重验）：把 MAX_ROWS 和 MAX_COLUMNS
# 各自拉大 100 倍，**这套用例全绿**。
#
# 这条路是**客户面**的：使用页上粘一段文本、传一个 Excel 就走到这里。
# 上限坏了不会立刻出事，只是某天有人粘进来一个十万行的表，
# 整个请求把内存吃光——而报错会停在解析器深处，看不出是"太大了"。


def test_too_many_pasted_rows_says_so(monkeypatch):
    from agent_platform import table_intake

    monkeypatch.setattr(table_intake, "MAX_ROWS", 5)
    text = "甲,乙\n" + "".join(f"{i},{i}\n" for i in range(20))
    with pytest.raises(TableIntakeError) as caught:
        parse_table("x.csv", text=text)
    assert "太大" in str(caught.value)
    assert "5" in str(caught.value), "得说清上限是多少，不然用户不知道要拆到多小"


def test_a_table_at_the_limit_still_parses(monkeypatch):
    """别把闸关死：正好到上限的表要能进来。

    实现写的是 `len(rows) > MAX_ROWS + 1`——那个 +1 是表头。
    没有这一条的话，把它改成 `> MAX_ROWS` 也没人发现，
    而那会让一张正好 MAX_ROWS 行的表被拒。
    """
    from agent_platform import table_intake

    monkeypatch.setattr(table_intake, "MAX_ROWS", 5)
    text = "甲,乙\n" + "".join(f"{i},{i}\n" for i in range(5))
    assert len(parse_table("x.csv", text=text)["rows"]) == 5


def test_the_real_default_is_not_unlimited():
    """默认值才是线上真正生效的那个——它得是个"够用但有边"的数。"""
    from agent_platform.table_intake import MAX_COLUMNS, MAX_ROWS

    assert 0 < MAX_ROWS <= 100_000
    assert 0 < MAX_COLUMNS <= 1_000


def test_too_many_columns_says_so(monkeypatch):
    """列超限在**粘贴/CSV** 这条路上是报错，不是截断。

    （写这条时我先猜成"截掉"——xlsx 那条路确实是截的，
      CSV 这条是拒的。两条路对同一个上限做法不同，先量再写。）
    """
    from agent_platform import table_intake

    monkeypatch.setattr(table_intake, "MAX_COLUMNS", 3)
    with pytest.raises(TableIntakeError) as caught:
        parse_table("x.csv", text="a,b,c,d,e\n1,2,3,4,5\n")
    assert "列太多" in str(caught.value)
    assert "3" in str(caught.value)


def test_a_table_at_the_column_limit_still_parses(monkeypatch):
    """别把闸关死：正好到列上限的表要能进来。"""
    from agent_platform import table_intake

    monkeypatch.setattr(table_intake, "MAX_COLUMNS", 3)
    parsed = parse_table("x.csv", text="a,b,c\n1,2,3\n")
    assert parsed["columns"] == ["a", "b", "c"]
