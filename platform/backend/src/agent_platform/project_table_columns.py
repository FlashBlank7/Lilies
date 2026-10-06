"""Read project table headers without loading their data rows or executing formulas."""
import csv
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .table_intake import TableIntakeError

MAX_HEADER_BYTES = 64 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_COLUMNS = 512
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


class _BoundedReader:
    def __init__(self, stream, limit=MAX_XML_BYTES):
        self.stream, self.remaining = stream, limit

    def read(self, size=-1):
        data = self.stream.read(min(size if size >= 0 else self.remaining + 1, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise TableIntakeError('表头信息较大，暂时无法列出字段；可以手动填写字段名')
        return data


def _xml(archive, name):
    with archive.open(name) as stream:
        return ET.fromstring(_BoundedReader(stream).read())


def _xlsx_columns(path, sheet):
    with ZipFile(path) as archive:
        workbook = _xml(archive, 'xl/workbook.xml')
        sheets = workbook.findall(NS + 'sheets/' + NS + 'sheet')
        if not sheets:
            raise TableIntakeError('Excel 里找不到工作表')
        view = workbook.find(NS + 'bookViews/' + NS + 'workbookView')
        active = int(view.get('activeTab', '0')) if view is not None else 0
        chosen = next((item for item in sheets if item.get('name') == sheet), None) if sheet else sheets[min(active, len(sheets)-1)]
        if chosen is None:
            raise TableIntakeError('工作表不存在；可用工作表：' + '、'.join(item.get('name', '') for item in sheets))
        relation_id = chosen.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        relations = _xml(archive, 'xl/_rels/workbook.xml.rels')
        target = next((item.get('Target') for item in relations if item.get('Id') == relation_id and item.get('TargetMode') != 'External'), '')
        if not target:
            raise TableIntakeError('无法读取此 Excel 的工作表')
        name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if not name.startswith('xl/'):
            raise TableIntakeError('无法读取此 Excel 的工作表')
        columns, shared = [], {}
        with archive.open(name) as stream:
            for _, row in ET.iterparse(_BoundedReader(stream), events=('end',)):
                if row.tag != NS + 'row':
                    continue
                if int(row.get('r', '1')) != 1:
                    break
                for cell in row.findall(NS + 'c'):
                    letters = ''.join(char for char in cell.get('r', '') if char.isalpha())
                    index = 0
                    for char in letters.upper():
                        index = index * 26 + ord(char) - ord('A') + 1
                    index = index - 1 if letters else len(columns)
                    if index < 0 or index >= MAX_COLUMNS:
                        raise TableIntakeError(f'表头超过 {MAX_COLUMNS} 列，可以手动填写字段名')
                    columns.extend([''] * (index + 1 - len(columns)))
                    value = cell.findtext(NS + 'v', '')
                    if cell.get('t') == 's':
                        shared[index] = int(value)
                    elif cell.get('t') == 'inlineStr':
                        columns[index] = ''.join(cell.itertext()).strip()
                    else:
                        columns[index] = value.strip()
                break
        if shared:
            wanted = set(shared.values())
            strings = {}
            with archive.open('xl/sharedStrings.xml') as stream:
                index = 0
                for _, item in ET.iterparse(_BoundedReader(stream), events=('end',)):
                    if item.tag == NS + 'si':
                        if index in wanted:
                            strings[index] = ''.join(node.text or '' for node in item.iter(NS + 't')).strip()
                        item.clear()
                        index += 1
                        if wanted <= strings.keys():
                            break
            for index, reference in shared.items():
                columns[index] = strings.get(reference, '')
        return columns, chosen.get('name', '')


def table_columns(root: Path, file_path: str, sheet: str = ''):
    relative = Path(file_path)
    if not file_path or relative.is_absolute() or '..' in relative.parts:
        raise FileNotFoundError
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise FileNotFoundError
    if target.suffix.lower() not in {'.csv', '.tsv', '.xlsx'}:
        raise TableIntakeError('请选择 CSV、TSV 或 XLSX 表格；其他资料可手动填写字段名')
    try:
        if target.suffix.lower() == '.xlsx':
            columns, sheet = _xlsx_columns(target, sheet)
        else:
            def lines(stream):
                remaining = MAX_HEADER_BYTES
                while line := stream.readline(remaining + 1):
                    remaining -= len(line)
                    if remaining < 0:
                        raise TableIntakeError('表头过长，可以手动填写字段名')
                    yield line.decode('utf-8-sig')
            with target.open('rb') as stream:
                columns = next(csv.reader(lines(stream), delimiter='\t' if target.suffix.lower() == '.tsv' else ',', strict=True), [])
            sheet = ''
    except (BadZipFile, KeyError, ET.ParseError, StopIteration, IndexError, ValueError, RuntimeError, csv.Error) as error:
        if isinstance(error, TableIntakeError):
            raise
        raise TableIntakeError('无法读取表头，请检查文件格式，或手动填写字段名') from error
    if not columns or any(not name.strip() for name in columns) or len(columns) != len(set(columns)):
        raise TableIntakeError('第一行需要非空、不重名的字段名，请检查表头')
    if len(columns) > MAX_COLUMNS:
        raise TableIntakeError(f'表头超过 {MAX_COLUMNS} 列，可以手动填写字段名')
    return {'columns': columns, 'sheet': sheet}
