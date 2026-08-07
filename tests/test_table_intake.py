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
