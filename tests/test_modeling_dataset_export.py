"""Uploaded inputs reach existing file workflows without accessing internal paths."""
import asyncio
import hashlib
import shlex
import sys

import pytest

from agent_platform.sandbox import CommandResult, SandboxSession
from agent_platform.tools.base import Tool, ToolResult
from agent_platform.tools.core import BashInput
from tests.test_projects import configured, edge, graph, node, ref  # noqa: F401
from tests.test_project_tool_help import prepare


def upload(configured):
    client, manager, pid, base = prepare(configured)
    content = b'id,x\n001,2\n002,4\n'
    response = client.post(base + '/datasets/upload', files={'file': ('fresh.csv', content, 'text/csv')})
    assert response.status_code == 201, response.text
    return client, manager, pid, base, response.json(), content


def export(client, base, dataset_id):
    return client.post(base + '/agent-tools', json={'name': 'project_modeling',
        'arguments': {'action': 'export_dataset', 'dataset_id': dataset_id}})


def test_uploaded_file_runs_in_existing_workflow_and_repeat_preserves_original(configured, monkeypatch):
    client, manager, pid, base, dataset, content = upload(configured)
    response = export(client, base, dataset['id'])
    assert response.status_code == 200, response.text
    exported = response.json()
    assert exported['files']['source']['sha256'] == hashlib.sha256(content).hexdigest()
    assert exported['labels_path'] == ''
    assert export(client, base, dataset['id']).json() == exported
    help = client.post(base + '/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'project_modeling'}}).json()
    example = next(e for e in help['examples'] if e['action'] == 'export_dataset')
    assert 'dataset_id' in example
    _, app, _, settings = configured
    workspace = settings.workspace_root / pid
    (workspace / 'solution').mkdir(exist_ok=True)
    (workspace / 'solution/check.py').write_text('''
import csv,json,sys
with open(sys.argv[1]) as f:
    rows=list(csv.DictReader(f))
print(json.dumps({'ids':[r['id'] for r in rows], 'sum':sum(float(r['x']) for r in rows)}))
''')

    async def fake_docker(self, argv, **kwargs):
        return CommandResult('container', '', 0)

    class LocalPython(Tool):
        name = 'Bash'
        description = 'Real Python, with only the container transport substituted.'
        input_model = BashInput

        async def execute(self, data, context):
            argv = shlex.split(data['command'])
            assert argv[0] == 'python3'
            process = await asyncio.create_subprocess_exec(sys.executable, *argv[1:],
                cwd=context.sandbox.workspace, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await process.communicate()
            return ToolResult((err if process.returncode else out).decode(), is_error=bool(process.returncode))

    monkeypatch.setattr(SandboxSession, '_host_command', fake_docker)
    monkeypatch.setitem(app.state.services.tools._tools, 'Bash', LocalPython())
    graph(client, pid, [node('start', 'start'),
        node('read', 'tool', tool_name='Bash', input={'command': 'python3 solution/check.py ' + exported['source_path']}),
        node('end', 'end', outputs={'result': ref('read', 'output')})], [edge('start', 'read'), edge('read', 'end')])
    run = client.post(base + '/agent-tools', json={'name': 'workflow_run', 'arguments': {'action': 'start'}})
    assert run.status_code == 200, run.text
    assert run.json()['status'] == 'succeeded', run.json()
    assert run.json()['outputs']['result'] == {'ids': ['001', '002'], 'sum': 6}
    copy = workspace / exported['source_path']
    original = app.state.services.modeling.path(pid, dataset['id']) / 'source.csv'
    assert copy.stat().st_ino != original.stat().st_ino
    copy.write_text('edited')
    assert original.read_bytes() == content
    repeated = export(client, base, dataset['id'])
    assert repeated.status_code == 409 and '不完整' in repeated.text
    assert copy.read_text() == 'edited'


def test_export_is_project_scoped_and_requires_build_phase(configured):
    client, manager, pid, base, dataset, _ = upload(configured)
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    prepare((client, configured[1], {'id': other}, configured[3]))
    assert export(client, '/api/v1/projects/' + other, dataset['id']).status_code == 404
    state = manager.load(pid); state.update(phase='operate'); manager.save(pid, state)
    assert export(client, base, dataset['id']).status_code == 422
    assert not (configured[3].workspace_root / pid / 'results/datasets').exists()


@pytest.mark.parametrize('where', ['results', 'dataset', 'file', 'source'])
def test_export_rejects_symlinks_without_writing_outside_project(configured, where):
    client, _, pid, base, dataset, content = upload(configured)
    _, app, _, settings = configured
    outside = settings.workspace_root / 'outside'; outside.mkdir()
    (outside / 'source.csv').write_bytes(content)
    root = settings.workspace_root / pid
    if where == 'results':
        target = root / 'results'; link = outside
    elif where == 'source':
        target = app.state.services.modeling.path(pid, dataset['id']) / 'source.csv'
        target.unlink(); link = outside / 'source.csv'
    else:
        target = root / 'results/datasets' / dataset['id']
        link = outside
        if where == 'file':
            target = target / 'source.csv'; link = outside / 'source.csv'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(link)
    response = export(client, base, dataset['id'])
    assert response.status_code in {409, 422}, response.text
    assert list(outside.iterdir()) == [outside / 'source.csv']
    assert (outside / 'source.csv').read_bytes() == content


def test_incomplete_export_is_cleaned_and_retry_succeeds(configured, monkeypatch):
    client, _, pid, base, dataset, _ = upload(configured)
    from agent_platform import modeling
    copy = modeling.copy_workspace_file

    def broken(source, destination):
        destination.write_text('partial')

    monkeypatch.setattr(modeling, 'copy_workspace_file', broken)
    response = export(client, base, dataset['id'])
    assert response.status_code == 422 and '校验失败' in response.text
    parent = configured[3].workspace_root / pid / 'results/datasets'
    assert list(parent.iterdir()) == []
    monkeypatch.setattr(modeling, 'copy_workspace_file', copy)
    assert export(client, base, dataset['id']).status_code == 200


def test_export_includes_labels_and_checks_registered_hash(configured):
    client, _, pid, base = prepare(configured)
    workspace = configured[3].workspace_root / pid
    (workspace / 'results').mkdir()
    (workspace / 'results/source.csv').write_text('id,x\n1,3\n')
    (workspace / 'results/labels.csv').write_text('id,y\n1,4\n')
    dataset = client.post(base + '/datasets', json={'source_path': 'results/source.csv', 'labels_path': 'results/labels.csv'}).json()
    original = configured[1].state.services.modeling.path(pid, dataset['id']) / 'source.csv'
    content = original.read_bytes(); original.write_bytes(b'changed')
    assert export(client, base, dataset['id']).status_code == 422
    original.write_bytes(content)
    response = export(client, base, dataset['id'])
    assert response.status_code == 200, response.text
    assert (workspace / response.json()['labels_path']).read_text() == 'id,y\n1,4\n'
