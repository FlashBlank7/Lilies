"""表格进料——普通用户的数据入口：Excel/CSV/粘贴 → 记录数组。

"永远不写 JSON"的后端支点：使用页把文件或粘贴文本发过来，这里解析成
{columns, rows}，前端摆成可编辑的格子。零第三方依赖：xlsx 走 zipfile+XML
（与电梯净室流水线同源的解析路数），csv/tsv 走标准库。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any

MAX_ROWS = 5_000
MAX_COLUMNS = 64


class TableIntakeError(ValueError):
    """解析失败（消息面向普通用户）。"""


def _coerce(value: str) -> Any:
    text = value.strip()
    if text == "":
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer() and "." not in text and "e" not in text.lower():
        return int(number)
    return number


def _column_letters(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def _parse_xlsx(data: bytes) -> list[list[Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise TableIntakeError("这不是有效的 Excel 文件（.xlsx）") from error
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        blob = archive.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        shared = [
            re.sub(r"<[^>]+>", "", item)
            for item in re.findall(r"<si>(.*?)</si>", blob, re.S)
        ]
    sheet_names = sorted(
        name for name in archive.namelist()
        if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
    )
    if not sheet_names:
        raise TableIntakeError("Excel 里找不到工作表")
    sheet = archive.read(sheet_names[0]).decode("utf-8", "ignore")
    rows: list[list[Any]] = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells = re.findall(
            r'<c r="([A-Z]+\d+)"(?:[^>]*t="(\w+)")?[^>]*>(?:<v>([^<]*)</v>)?',
            row_xml,
        )
        if not cells:
            continue
        width = max(_column_index(_column_letters(ref)) for ref, _, _ in cells) + 1
        row: list[Any] = [""] * min(width, MAX_COLUMNS)
        for ref, kind, raw in cells:
            index = _column_index(_column_letters(ref))
            if index >= MAX_COLUMNS:
                continue
            if kind == "s":
                try:
                    row[index] = shared[int(raw)] if raw else ""
                except (ValueError, IndexError):
                    row[index] = ""
            else:
                row[index] = _coerce(raw or "")
        rows.append(row)
        if len(rows) > MAX_ROWS + 1:
            raise TableIntakeError(f"表格太大（超过 {MAX_ROWS} 行），请拆分后再上传")
    return rows


def _parse_delimited(text: str) -> list[list[Any]]:
    sample = text[:4_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        class _Tab(csv.Dialect):
            delimiter = "\t"
            quotechar = '"'
            doublequote = True
            skipinitialspace = False
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL
        dialect = _Tab
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [[_coerce(cell) for cell in row] for row in reader if any(str(c).strip() for c in row)]
    if len(rows) > MAX_ROWS + 1:
        raise TableIntakeError(f"表格太大（超过 {MAX_ROWS} 行），请拆分后再粘贴")
    return rows


def _records_from_grid(grid: list[list[Any]]) -> dict[str, Any]:
    if not grid:
        raise TableIntakeError("没解析到任何内容——第一行需要是表头")
    headers = [str(cell).strip() for cell in grid[0]]
    if not any(headers):
        raise TableIntakeError("第一行是空的——第一行需要是每一列的名字（表头）")
    columns = [header or f"列{index + 1}" for index, header in enumerate(headers)]
    if len(columns) > MAX_COLUMNS:
        raise TableIntakeError(f"列太多（超过 {MAX_COLUMNS} 列）")
    rows: list[dict[str, Any]] = []
    for raw in grid[1:]:
        record: dict[str, Any] = {}
        for index, column in enumerate(columns):
            record[column] = raw[index] if index < len(raw) else ""
        if any(str(v).strip() for v in record.values()):
            rows.append(record)
    return {"columns": columns, "rows": rows}


def parse_table(filename: str, data: bytes | None = None, text: str | None = None) -> dict[str, Any]:
    """文件（xlsx/csv）或粘贴文本 → {columns, rows}。第一行必须是表头。"""

    lowered = (filename or "").lower()
    if data is not None and lowered.endswith(".xlsx"):
        return _records_from_grid(_parse_xlsx(data))
    if data is not None:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError as error:
                raise TableIntakeError(
                    "文件编码无法识别——请另存为 UTF-8 的 CSV，或直接上传 .xlsx"
                ) from error
    if not text or not text.strip():
        raise TableIntakeError("内容是空的")
    return _records_from_grid(_parse_delimited(text))
