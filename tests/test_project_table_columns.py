"""Employee column choices read bounded headers from authorized project files."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from openpyxl import Workbook

from agent_platform.project_table_columns import table_columns, MAX_HEADER_BYTES, MAX_XML_BYTES
from agent_platform.table_intake import TableIntakeError
from agent_platform import example_processing
from test_users import platform, signup, project  # noqa: F401


@pytest.mark.parametrize('extension,header,columns', [
    ('csv', '\ufeff设备,"产量,数量"\r\n', ['设备', '产量,数量']),
    ('tsv', '设备\t产量\n', ['设备', '产量']),
])
def test_delimited_headers_do_not_decode_or_scan_data(tmp_path, extension, header, columns):
    path = tmp_path / ('source.' + extension)
    path.write_bytes(header.encode() + b'\xff' * (MAX_HEADER_BYTES * 2))
    assert table_columns(tmp_path, path.name) == {'columns': columns, 'sheet': ''}


def test_xlsx_uses_active_or_explicit_sheet_and_cached_header_values(tmp_path):
    book = Workbook()
    book.active.title = '旧数据'
    book.active.append(['旧字段'])
    sheet = book.create_sheet('本次数据')
    sheet.append(['设备', '产量'])
    sheet.append(['A', 12])
    book.active = 1
    book.save(tmp_path / 'source.xlsx')
    assert table_columns(tmp_path, 'source.xlsx') == {'columns': ['设备', '产量'], 'sheet': '本次数据'}
    assert table_columns(tmp_path, 'source.xlsx', '旧数据')['columns'] == ['旧字段']
    with pytest.raises(TableIntakeError, match='旧数据、本次数据'):
        table_columns(tmp_path, 'source.xlsx', '不存在')


def test_xlsx_shared_strings_and_bounded_sheet_read(tmp_path):
    path = tmp_path / 'shared.xlsx'
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="数据" r:id="r1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/sharedStrings.xml', f'<sst xmlns="{ns}"><si><t>设备</t></si><si><r><t>产</t></r><r><t>量</t></r></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>' + '<row r="2"><c r="A2"><v>1</v></c></row>' * 100000 + '</sheetData></worksheet>')
    assert table_columns(tmp_path, path.name)['columns'] == ['设备', '产量']
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('xl/workbook.xml', b' ' * (MAX_XML_BYTES + 1))
    with pytest.raises(TableIntakeError, match='表头信息较大'):
        table_columns(tmp_path, path.name)


@pytest.mark.parametrize('content', ['', 'a,a\n', 'a,\n', 'a' * (MAX_HEADER_BYTES + 1)])
def test_invalid_and_overlong_headers_leave_manual_input_available(tmp_path, content):
    (tmp_path / 'source.csv').write_text(content)
    with pytest.raises(TableIntakeError):
        table_columns(tmp_path, 'source.csv')


def test_table_headers_use_project_authorization_and_workspace_boundary(platform, tmp_path):
    client, app = platform
    _, owner = signup(client, '字段负责人')
    _, other = signup(client, '其他员工')
    pid = project(client, owner)
    root = app.state.services.settings.workspace_root / pid
    root.mkdir(parents=True, exist_ok=True)
    (root / 'source.csv').write_text('设备,产量\nA,12\n')
    (root / 'notes.md').write_text('# 说明')
    secret = tmp_path / 'private.csv'
    secret.write_text('secret\nvalue\n')
    (root / 'outside.csv').symlink_to(secret)
    (root / 'invalid.xlsx').write_bytes(b'not a workbook')
    with ZipFile(root / 'empty.xlsx', 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets/></workbook>')
    url = f'/api/v1/applications/{pid}/workspace/table-columns'
    assert client.get(url, params={'path': 'source.csv'}, headers=owner).json()['columns'] == ['设备', '产量']
    assert client.get(url, params={'path': 'source.csv'}, headers=other).status_code == 404
    assert client.get(url, params={'path': 'source.csv'}).status_code == 401
    for path in ['../private.csv', str(secret), 'outside.csv', 'missing.csv']:
        assert client.get(url, params={'path': path}, headers=owner).status_code == 404
    assert client.get(url, params={'path': 'notes.md'}, headers=owner).status_code == 422
    assert client.get(url, params={'path': 'invalid.xlsx'}, headers=owner).status_code == 422
    empty = client.get(url, params={'path': 'empty.xlsx'}, headers=owner)
    assert empty.status_code == 422
    assert '找不到工作表' in empty.json()['detail']
    assert client.get(f'/api/v1/projects/{pid}/tasks', headers=owner).json() == []
    assert app.state.services.settings.model_egress_enabled is False


def test_summary_missing_column_lists_available_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Path('requirement-package/data.csv')
    source.parent.mkdir()
    source.write_text('设备,产量\nA,12\n')
    with pytest.raises(ValueError, match='可用字段：设备、产量'):
        example_processing.main({'operation': 'summary', 'source_path': str(source), 'group': '旧分组', 'value': '产量'})
