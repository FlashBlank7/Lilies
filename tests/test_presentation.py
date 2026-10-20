"""Editable report production without training or provider access."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import pytest

from agent_platform import presentation_code as code
from agent_platform.presentation_workflow import example_files
from tests.test_projects import configured, start  # noqa: F401


def test_paginated_content_keeps_every_table_value_and_source_line():
    headers = ['编号', '甲', '乙', '丙', '丁', '戊']
    rows = [[f'r{r}c{c}' for c in range(6)] for r in range(25)]
    text = '# 对比\n正文含0.77与12.6秒。\n' + '|'.join(headers) + '\n' + '|'.join(['---'] * 6) + '\n'
    text += '\n'.join('|'.join(row) for row in rows)
    pages = code.paginate(code.parse(text))
    tables = [p for p in pages if p['kind'] == 'table']
    assert len(tables) > 2
    actual = [cell.replace('\n', '') for p in tables for row in p['rows'] for cell in row]
    for row in rows:
        assert actual.count(row[0]) == 2  # first column accompanies both column groups
        assert all(actual.count(value) == 1 for value in row[1:])
    assert all(p['start'] >= 3 and p['end'] <= 29 for p in tables)
    assert all(sum(p['row_heights']) <= 4.8 for p in tables)
    assert '0.77' in pages[0]['items'][0]['text']


def test_changed_input_keeps_previous_content_and_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / 'requirement-package'; folder.mkdir()
    original = folder / 'report.md'; original.write_text('# 首稿\n结果为0.77，尚需复核。')
    first = code.prepare({'source_path':'requirement-package/report.md'})
    before = Path(first['manifest_path']).read_bytes()
    original.write_text('# 修改稿\n尚未训练。')
    second = code.prepare({'source_path':'requirement-package/report.md'})
    assert first['manifest_path'] != second['manifest_path']
    assert Path(first['manifest_path']).read_bytes() == before
    data = json.loads(before)
    assert '0.77' in Path(data['source_snapshot']).read_text()
    assert data['source_sha256'] == hashlib.sha256(Path(data['source_snapshot']).read_bytes()).hexdigest()
    Path(data['source_snapshot']).write_text('changed')
    with pytest.raises(ValueError, match='快照已改变'):
        code.render({'prepared':first})


@pytest.mark.parametrize(('inputs', 'error'), [
    ({}, '二者只填一项'),
    ({'source_path':'requirement-package/a.md','outline':'x'}, '二者只填一项'),
    ({'source_path':'../other/report.md'}, '当前项目'),
    ({'outline':'# 只有标题'}, '只有标题'),
    ({'outline':'a|b\n---|---\n1|2|3'}, '第3行表格列数'),
    ({'outline':'```python\nmissing'}, '没有结束'),
    ({'outline':'!['+'图注'*30+'](requirement-package/a.png)'}, '图片说明过长'),
])
def test_actionable_input_errors(tmp_path, monkeypatch, inputs, error):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=error): code.prepare(inputs)


def test_images_are_frozen_and_render_only_uses_matching_snapshots(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / 'requirement-package'; folder.mkdir()
    Image.new('RGB', (40, 20), 'blue').save(folder/'chart.png')
    (folder/'report.md').write_text('# 原图\n![图](chart.png)')
    prepared = code.prepare({'source_path':'requirement-package/report.md'})
    path = Path(prepared['manifest_path']); data = json.loads(path.read_text())
    snapshot = Path(data['images'][0]['snapshot'])
    original = snapshot.read_bytes()
    (folder/'chart.png').write_bytes(b'changed')
    assert snapshot.read_bytes() == original
    data['pages'][0]['path'] = 'https://example.invalid/chart.png'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='图片与本次汇报快照不一致'):
        code.render({'prepared':prepared})


def test_missing_environment_keeps_prepared_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prepared = code.prepare({'outline':'# 汇报\n原始结论，不调用模型改写。'})
    monkeypatch.setattr(code.shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='文档环境缺少'):
        code.render({'prepared':prepared})
    assert Path(prepared['manifest_path']).is_file()


def test_skill_discovery_loads_code_on_demand_without_starting_task(configured):
    client, _, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    listing = client.get(base+'/skills').json()
    assert any(s['id']=='presentation' for s in listing)
    assert all('content' not in s and 'references' not in s for s in listing)
    result = client.post(base+'/agent-tools', json={'name':'project_skills','arguments':{
        'action':'read','skill_id':'presentation','reference':'render.py'}})
    assert result.status_code == 200, result.text
    scope = {}; exec(result.json()['content'], scope)
    assert scope['parse']('# 稿件\n正文')[1]['text'] == '正文'
    assert client.get(base+'/tasks').json() == []


def test_real_platform_renders_editable_pptx_pdf_and_downloads(configured):
    client, _, project, settings = configured
    if not shutil.which('docker'):
        pytest.skip('Docker is required for actual document rendering')
    check = subprocess.run(['docker','run','--rm','--network','none','--entrypoint','node',
                            settings.sandbox_image,'-e',"require('pptxgenjs')"], capture_output=True)
    if check.returncode:
        pytest.skip('Set SANDBOX_IMAGE to a documents image with PptxGenJS')
    base = '/api/v1/projects/' + project['id']
    installed = client.post(base+'/space/official-workflows/presentation')
    assert installed.status_code == 201, installed.text
    task = start(client, base, 'presentation', workflow_id=installed.json()['workflow_id'],
                 inputs={'outline':example_files()['内部试用汇报.md']})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        task = client.get(base+'/tasks/'+task['id']).json()
        if task['status'] not in {'queued','running'}: break
        time.sleep(.1)
    assert task['status']=='succeeded', task.get('error')
    result = task['outputs']['result']
    assert result['slides'] == 5
    files = {}
    for artifact in result['artifacts']:
        download = client.get('/api/v1/applications/'+project['id']+'/workspace/files/'+artifact['file_path'])
        assert download.status_code == 200, download.text[:200]
        files[Path(artifact['file_path']).name] = download.content
    with zipfile.ZipFile(io.BytesIO(files['presentation.pptx'])) as pptx:
        slides = [n for n in pptx.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')]
        assert len(slides) == 5
        assert any(b'<a:tbl>' in pptx.read(n) for n in slides)
        assert any('0.77' in pptx.read(n).decode() for n in slides)
        assert any(n.startswith('ppt/notesSlides/notesSlide') for n in pptx.namelist())
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(files['presentation.pdf']))
    assert len(pdf.pages)==5 and '方法' in pdf.pages[2].extract_text()
    assert len([n for n in files if n.endswith('.png')])==5
