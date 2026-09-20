"""Saved project tests must run the same files in isolated parent/child workspaces."""
import asyncio
import json
import shlex
import sys

import pytest

from agent_platform.sandbox import CommandResult, SandboxSession
from agent_platform.tools.base import Tool, ToolResult
from agent_platform.tools.core import BashInput
from tests.test_projects import configured as configured  # noqa: F401
from tests.test_projects import edge, graph, node, ref, settled, start


def test_saved_cases_isolate_records_but_share_them_with_members(configured):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    live = client.put(base + '/records/resources/same', json={
        'expected_revision': 0, 'value': {'owner': 'live'}})
    assert live.status_code == 200, live.text
    member = client.post(base + '/members', json={'name': 'Complete test allocation'}).json()['id']
    graph(client, member, [node('start', 'start'),
        node('read', 'project_record', action='get', collection='resources', key='same'),
        node('write', 'project_record', action='put', collection='resources', key='same',
             expected_revision=ref('read', 'revision'), value={'owner': 'child'}),
        node('stale', 'project_record', action='put', collection='resources', key='same',
             expected_revision=1, value={'owner': 'wrong'}),
        node('end', 'end', outputs={'previous': ref('read', 'value', 'owner'),
             'conflict': ref('stale', 'conflict'), 'revision': ref('stale', 'revision')})],
        [edge('start', 'read'), edge('read', 'write'), edge('write', 'stale'), edge('stale', 'end')])
    graph(client, pid, [node('start', 'start'),
        node('before', 'project_record', action='get', collection='resources', key='same'),
        node('seed', 'project_record', action='put', collection='resources', key='same',
             expected_revision=0, value={'owner': 'parent'}),
        node('child', 'tool', tool_name='workflow:' + member, input={}),
        node('list', 'project_record', action='list', collection='resources'),
        node('end', 'end', outputs={'fresh': ref('before', 'found'),
             'written': ref('seed', 'written'), 'child': ref('child', 'output'),
             'records': ref('list', 'records')})],
        [edge('start', 'before'), edge('before', 'seed'), edge('seed', 'child'),
         edge('child', 'list'), edge('list', 'end')])
    for case in ('first', 'second'):
        path = f'/api/v1/applications/{pid}/draft'
        draft = client.get(path).json()
        response = client.post(path, json={'expected_revision': draft['revision'],
            'idempotency_key': case, 'op': 'add_test', 'data': {'test': {
                'id': case, 'name': case, 'requirement': 'Isolated shared state',
                'assertions': [
                    {'path': ['fresh'], 'operator': 'equals', 'expected': False},
                    {'path': ['written'], 'operator': 'equals', 'expected': True},
                    {'path': ['child', 'previous'], 'operator': 'equals', 'expected': 'parent'},
                    {'path': ['child', 'conflict'], 'operator': 'equals', 'expected': True},
                    {'path': ['child', 'revision'], 'operator': 'equals', 'expected': 2}]}}})
        assert response.status_code == 200, response.text
    # Same cases and business keys, twice: no key rotation or record cleanup.
    for _ in range(2):
        response = client.post(base + '/members/' + pid + '/tests/run')
        assert response.status_code == 200, response.text
        report = response.json()
        assert report['passed'], report
        for test in report['tests']:
            records = test['outputs']['records']
            assert len(records) == 1
            assert records[0]['key'] == 'same'
            assert records[0]['value'] == {'owner': 'child'}
            assert records[0]['revision'] == 2
        assert client.get(base + '/records/resources/same').json()['value'] == {'owner': 'live'}
        assert len(client.get(base + '/records').json()) == 1


@pytest.mark.parametrize('nested', [False, True])
def test_saved_project_files_are_available_isolated_and_readonly(configured, monkeypatch, nested):
    client, app, project, settings = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    workspace = settings.workspace_root / pid
    for directory in ('requirement-package', 'requirements', 'solution', 'results'):
        (workspace / directory).mkdir()
    (workspace / 'requirement-package' / 'values.json').write_text('[2, 3]')
    (workspace / 'requirements' / 'requirements.md').write_text('Add the supplied values')
    (workspace / 'results' / 'cached.json').write_text('{"seed": 5}')
    (workspace / 'private.txt').write_text('Not a project input or artifact')
    (workspace / 'solution' / 'probe.py').write_text('''
import json, sys
from pathlib import Path
mode, case = sys.argv[1:]
assert Path('requirements/requirements.md').read_text() == 'Add the supplied values'
value = sum(json.loads(Path('requirement-package/values.json').read_text()))
assert value == json.loads(Path('results/cached.json').read_text())['seed']
marker = Path('results/marker.txt')
if mode == 'seed':
    assert not marker.exists(), 'another test already wrote here'
    marker.write_text(case)
    Path('solution/local.txt').write_text(case)
else:
    assert marker.read_text() == case, 'member used another workspace'
print(json.dumps({'value': value, 'case': marker.read_text()}))
''')
    mounts = []
    observed = []

    async def fake_docker(self, argv, **kwargs):
        if argv[:2] == ['docker', 'run']:
            mounts.append(argv)
        return CommandResult('container', '', 0)

    class LocalPython(Tool):
        name = 'Bash'
        description = 'Run the fixture script locally; only Docker transport is replaced.'
        input_model = BashInput

        async def execute(self, data, context):
            observed.append(context.sandbox.workspace)
            argv = shlex.split(data['command'])
            assert argv[0] == 'python3'
            process = await asyncio.create_subprocess_exec(
                sys.executable, *argv[1:], cwd=context.sandbox.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await process.communicate()
            return ToolResult((err if process.returncode else out).decode(), is_error=bool(process.returncode))

    monkeypatch.setattr(SandboxSession, '_host_command', fake_docker)
    monkeypatch.setitem(app.state.services.tools._tools, 'Bash', LocalPython())
    nodes = [node('start', 'start'),
             node('seed', 'tool', tool_name='Bash', input={'command': ref('$inputs', 'seed')})]
    edges = [edge('start', 'seed')]
    result_node = 'seed'
    if nested:
        member = client.post(base + '/members', json={'name': 'Read parent result'}).json()['id']
        graph(client, member, [node('start', 'start'),
            node('read', 'tool', tool_name='Bash', input={'command': ref('$inputs', 'command')}),
            node('end', 'end', outputs={'value': ref('read', 'output', 'value')})],
            [edge('start', 'read'), edge('read', 'end')])
        nodes.append(node('child', 'tool', tool_name='workflow:' + member,
                          input={'command': ref('$inputs', 'verify')}))
        edges.append(edge('seed', 'child'))
        result_node = 'child'
    nodes.append(node('end', 'end', outputs={'value': ref(result_node, 'output', 'value')}))
    edges.append(edge(result_node, 'end'))
    graph(client, pid, nodes, edges)

    def inputs(case):
        return {'seed': f'python3 solution/probe.py seed {case}',
                'verify': f'python3 solution/probe.py verify {case}'}

    for case in ('a', 'b'):
        path = f'/api/v1/applications/{pid}/draft'
        draft = client.get(path).json()
        r = client.post(path, json={'expected_revision': draft['revision'], 'idempotency_key': case,
            'op': 'add_test', 'data': {'test': {'id': case, 'name': case, 'requirement': 'Read real files',
                'inputs': inputs(case), 'assertions': [{'path': ['value'], 'operator': 'equals', 'expected': 5}]}}})
        assert r.status_code == 200, r.text
    response = client.post(base + '/members/' + pid + '/tests/run')
    assert response.status_code == 200, response.text
    assert response.json()['passed'], response.json()
    case_workspaces = set(observed)
    assert len(case_workspaces) == 2
    assert all(p != workspace and workspace in p.parents for p in case_workspaces)
    assert sorted((p / 'results/marker.txt').read_text() for p in case_workspaces) == ['a', 'b']
    assert all(not (p / 'private.txt').exists() for p in case_workspaces)
    assert not (workspace / 'results/marker.txt').exists()
    assert not (workspace / 'solution/local.txt').exists()
    assert all(p.stat().st_ino != (workspace / 'solution/probe.py').stat().st_ino
               for p in (w / 'solution/probe.py' for w in case_workspaces))
    for argv in mounts:
        assert any(value.endswith(':/workspace/requirement-package:ro') for value in argv)
        assert any(value.endswith(':/workspace/requirements:ro') for value in argv)
    # The same graph and script work through the ordinary project run entry too.
    result = settled(client, base, start(client, base, 'ordinary', inputs=inputs('ordinary')))
    assert result['status'] == 'succeeded' and result['outputs']['value'] == 5, result
    assert json.loads((workspace / 'requirement-package/values.json').read_text()) == [2, 3]


def test_automatic_project_file_copy_rejects_symlinks(configured):
    client, _, project, settings = configured
    pid = project['id']
    workspace = settings.workspace_root / pid
    (workspace / 'solution').mkdir()
    other = settings.workspace_root / 'outside.txt'
    other.write_text('another project')
    (workspace / 'solution/escape.txt').symlink_to(other)
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'ok': True})], [edge('start', 'end')])
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path).json()
    client.post(path, json={'expected_revision': draft['revision'], 'idempotency_key': 'symlink-test',
        'op': 'add_test', 'data': {'test': {'id': 'check', 'name': 'check', 'requirement': 'stay scoped',
            'assertions': [{'path': ['ok'], 'operator': 'equals', 'expected': True}]}}})
    result = client.post(f'/api/v1/projects/{pid}/members/{pid}/tests/run')
    assert result.status_code == 422
    assert 'symlink' in result.json()['detail']
    assert other.read_text() == 'another project'
