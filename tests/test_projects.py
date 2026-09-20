"""Project cooperation runs through the real workflow engine (no industrial data)."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_requirement_package import package


@pytest.fixture
def configured(tmp_path):
    settings = Settings(api_token='test', data_dir=tmp_path/'data', workspace_root=tmp_path/'ws',
                        model_egress_enabled=False, scheduler_poll_seconds=3600)
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer test'
        project = client.post('/api/v1/projects', json={'name': '平台协作测试'}).json()
        yield client, app, project, settings


def ref(node, *path):
    return {'$ref': {'node_id': node, 'path': list(path)}}


def node(id, type, **config):
    return {'id': id, 'type': type, 'title': id, 'config': config}


def edge(source, target, branch=None):
    return {'id': source+'-'+target, 'source': source, 'target': target, 'branch': branch}


def graph(client, id, nodes, edges):
    base = f'/api/v1/applications/{id}/draft'
    draft = client.get(base).json()
    result = client.post(base, json={'expected_revision': draft['revision'], 'idempotency_key': f'graph-{draft["revision"]}',
        'op': 'replace_workflow', 'data': {'workflow': {'nodes': nodes, 'edges': edges}}})
    assert result.status_code == 200, result.text


def settled(client, base, task):
    for _ in range(300):
        result = client.get(base+'/tasks/'+task['id']).json()
        if result['status'] not in {'running', 'queued'}:
            return result
        time.sleep(.01)
    raise AssertionError(result)


def start(client, base, key, **kwargs):
    result = client.post(base+'/tasks', json={'request_key': key, **kwargs})
    assert result.status_code == 202, result.text
    return result.json()


def fixture_graphs(client, project):
    pid = project['id']
    base = '/api/v1/projects/'+pid
    validate = client.post(base+'/members', json={'name': '申请校验'}).json()['id']
    allocate = client.post(base+'/members', json={'name': '资源分配'}).json()['id']
    fields = [{'name': 'request_id', 'type': 'string', 'required': True}]
    graph(client, validate, [node('start', 'start', inputs=fields),
        node('save', 'project_record', action='put', collection='requests', key=ref('$inputs', 'request_id'),
             value={'status': 'validated'}, expected_revision=0),
        node('end', 'end', outputs={'request_id': ref('$inputs', 'request_id')})],
        [edge('start', 'save'), edge('save', 'end')])
    graph(client, allocate, [node('start', 'start', inputs=fields),
        node('read', 'project_record', action='get', collection='resources', key='only'),
        node('available', 'if_else', cases=[{'id': 'yes', 'conditions': [
            {'value': ref('read', 'value', 'owner'), 'operator': 'equals', 'expected': ''}]}]),
        node('claim', 'project_record', action='put', collection='resources', key='only',
             value={'owner': ref('$inputs', 'request_id')}, expected_revision=ref('read', 'revision')),
        node('written', 'if_else', cases=[{'id': 'yes', 'conditions': [
            {'value': ref('claim', 'written'), 'expected': True}]}]),
        node('done', 'end', outputs={'allocated': True, 'owner': ref('$inputs', 'request_id')}),
        node('wait', 'end', outputs={'allocated': False, 'task_status': 'waiting_input', 'message': '请释放资源后继续'})],
        [edge('start', 'read'), edge('read', 'available'), edge('available', 'claim', 'yes'),
         edge('available', 'wait', 'else'), edge('claim', 'written'), edge('written', 'done', 'yes'), edge('written', 'wait', 'else')])
    graph(client, pid, [node('start', 'start', inputs=fields),
        node('check', 'tool', tool_name='workflow:'+validate, input={'request_id': ref('$inputs', 'request_id')}),
        node('allocate', 'tool', tool_name='workflow:'+allocate, input={'request_id': ref('check', 'output', 'request_id')}),
        node('end', 'end', outputs={'result': ref('allocate', 'output')})],
        [edge('start', 'check'), edge('check', 'allocate'), edge('allocate', 'end')])
    return base, validate, allocate


def test_import_stays_blank_and_member_ownership(configured):
    client, app, project, _ = configured
    imported = client.post('/api/v1/projects/requirement-packages/import', files={'file': ('input.zip', package())})
    assert imported.status_code == 201, imported.text
    pid = imported.json()['project']['id']
    assert client.get(f'/api/v1/applications/{pid}/draft').json()['snapshot']['workflow']['nodes'] == []
    assert client.get(f'/api/v1/projects/{pid}/tasks').json() == []
    assert client.get(f'/api/v1/projects/{pid}/agent-session').json()['provider'] is None
    assert not app.state.services.local_agents.tasks
    assert client.get(f'/api/v1/applications/{pid}/project').json()['project_id'] == pid


def test_records_cas_and_cross_project_isolation(configured):
    client, _, project, _ = configured
    base = '/api/v1/projects/'+project['id']
    def write(owner):
        return client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': owner}})
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ['a', 'b']))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert client.get(base+'/records/resources/only').json()['revision'] == 1
    other = client.post('/api/v1/projects', json={'name': '其他项目'}).json()['id']
    assert client.get(f'/api/v1/projects/{other}/records/resources/only').json()['found'] is False
    assert client.put(base+'/records/resources/only', json={'expected_revision': True, 'value': {}}).status_code == 422


def test_two_flows_compete_duplicate_wait_release_resume(configured):
    client, _, project, _ = configured
    base, validate, allocate = fixture_graphs(client, project)
    assert client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': ''}}).status_code == 200
    a = start(client, base, 'a', inputs={'request_id': 'a'})
    b = start(client, base, 'b', inputs={'request_id': 'b'})
    results = [settled(client, base, t) for t in [a, b]]
    assert sorted(r['status'] for r in results) == ['succeeded', 'waiting_input'], results
    assert all(len(r['runs']) == 3 for r in results)
    assert all(run['draft_revision'] == 1 for result in results for run in result['runs'])
    repeated = start(client, base, 'a', inputs={'request_id': 'a'})
    assert repeated['id'] == a['id'] and len(repeated['runs']) == 3
    assert client.post(base+'/tasks', json={'request_key': 'a', 'inputs': {'request_id': 'different'}}).status_code == 409
    assert client.delete(base+'/members/'+validate).status_code == 409
    pending = next(r for r in results if r['status'] == 'waiting_input')
    resource = client.get(base+'/records/resources/only').json()
    assert resource['revision'] == 2
    assert client.put(base+'/records/resources/only', json={'expected_revision': 2, 'value': {'owner': ''}}).status_code == 200
    assert client.post(base+'/tasks/'+pending['id']+'/resume', json={}).status_code == 202
    done = settled(client, base, pending)
    assert done['status'] == 'succeeded', done
    assert len([r for r in done['runs'] if r['application_id'] == validate]) == 1
    assert client.get(base+'/records/resources/only').json()['value']['owner'] == pending['request_key']


def test_illegal_member_and_missing_input_fail_clearly(configured):
    client, _, project, _ = configured
    base, validate, _ = fixture_graphs(client, project)
    missing = settled(client, base, start(client, base, 'missing', workflow_id=validate))
    assert missing['status'] == 'failed' and 'request_id' in missing['error']
    other = client.post('/api/v1/projects', json={'name': '其他项目'}).json()['id']
    graph(client, project['id'], [node('start', 'start'), node('call', 'tool', tool_name='workflow:'+other),
        node('end', 'end', outputs={})], [edge('start', 'call'), edge('call', 'end')])
    denied = settled(client, base, start(client, base, 'denied'))
    assert denied['status'] == 'failed', denied
    assert 'policy' in denied['error'] or 'scope' in denied['error'] or '范围' in denied['error']


def test_snapshot_and_stop_children(configured, monkeypatch):
    client, app, project, _ = configured
    base, _, allocate = fixture_graphs(client, project)
    client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': ''}})
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    original = _NODE_EXECUTORS['project_record']
    async def slow(self, run):
        if run.state.application_id == allocate:
            await asyncio.sleep(.35)
        return await original(self, run)
    # Executors dispatch by method name; delay actual record processing, not graph execution.
    monkeypatch.setitem(_NODE_EXECUTORS, 'project_record', slow)
    task = start(client, base, 'frozen', inputs={'request_id': 'frozen'})
    graph(client, allocate, [node('start', 'start'), node('end', 'end', outputs={'edited': True})], [edge('start', 'end')])
    result = settled(client, base, task)
    assert result['status'] == 'succeeded', result
    assert client.get(base+'/records/resources/only').json()['value']['owner'] == 'frozen'
    # Restore the cooperating graph to test stopping while a child is active.
    base, _, allocate = fixture_graphs(client, project)
    task = start(client, base, 'stop', inputs={'request_id': 'stop'})
    for _ in range(100):
        current = client.get(base+'/tasks/'+task['id']).json()
        if any(r['application_id'] == allocate and r['status'] == 'running' for r in current['runs']):
            break
        time.sleep(.01)
    stopped = client.post(base+'/tasks/'+task['id']+'/stop').json()
    assert stopped['status'] == 'interrupted', stopped
    assert all(r['status'] not in {'running', 'queued'} for r in stopped['runs'])
    assert client.get(base+'/records/requests/stop').json()['found']
    assert client.post(base+'/tasks/'+task['id']+'/resume', json={}).status_code == 202
    resumed = settled(client, base, task)
    assert resumed['status'] == 'waiting_input', resumed
    assert client.get(base+'/records/requests/stop').json()['revision'] == 1


def test_restart_preserves_records_and_requires_explicit_resume(configured):
    client, app, project, settings = configured
    base, _, _ = fixture_graphs(client, project)
    client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': 'occupied'}})
    task = settled(client, base, start(client, base, 'restart', inputs={'request_id': 'restart'}))
    assert task['status'] == 'waiting_input'
    # Simulate a process that died before finishing its running task's final status write.
    async def running():
        await app.state.services.projects.store.update_task(task['id'], status='running')
    client.portal.call(running)
    fresh = create_app(settings)
    with TestClient(fresh) as restarted:
        restarted.headers['Authorization'] = 'Bearer test'
        saved = restarted.get(base+'/tasks/'+task['id']).json()
        assert saved['status'] == 'interrupted'
        assert len(saved['runs']) == 3
        assert not fresh.state.services.projects.active
        assert restarted.get(base+'/records/requests/restart').json()['revision'] == 1
        restarted.put(base+'/records/resources/only', json={'expected_revision': 1, 'value': {'owner': ''}})
        assert restarted.post(base+'/tasks/'+task['id']+'/resume', json={}).status_code == 202
        assert settled(restarted, base, task)['status'] == 'succeeded'


def test_saved_tests_execute_project_members_and_record_report(configured):
    client, _, project, _ = configured
    base, _, _ = fixture_graphs(client, project)
    client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': 'live'}})
    test_member = client.post(base + '/members', json={'name': '保存测试准备资源', 'purpose': 'test'}).json()['id']
    graph(client, test_member, [node('start', 'start'),
        node('seed', 'project_record', action='put', collection='resources', key='only', value={'owner': ''}),
        node('call', 'tool', tool_name='workflow:' + project['id'],
             input={'request_id': ref('$inputs', 'request_id')}),
        node('end', 'end', outputs={'result': ref('call', 'output', 'result')})],
        [edge('start', 'seed'), edge('seed', 'call'), edge('call', 'end')])
    path = f'/api/v1/applications/{test_member}/draft'
    draft = client.get(path).json()
    added = client.post(path, json={'expected_revision': draft['revision'], 'idempotency_key': 'test',
        'op': 'add_test', 'data': {'test': {'id': 'happy', 'name': '实际分配', 'requirement': '有资源时分配',
            'inputs': {'request_id': 'saved-test'}, 'assertions': [{'path': ['result', 'allocated'], 'operator': 'equals', 'expected': True}]}}})
    assert added.status_code == 200, added.text
    result = client.post(base + '/members/' + test_member + '/tests/run')
    assert result.status_code == 200, result.text
    assert result.json()['passed'], result.json()
    assert result.json()['summary']['passed'] == 1
    assert client.get(path).json()['tested_hash'] == client.get(path).json()['content_hash']
    assert client.get(base + '/records/resources/only').json()['value'] == {'owner': 'live'}


def test_nested_human_input_persists_and_resumes_without_repeating_writes(configured):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/'+pid
    member = client.post(base+'/members', json={'name': '人工确认'}).json()['id']
    graph(client, member, [node('start', 'start'),
        node('save', 'project_record', action='put', collection='progress', key='once', value={'started': True}),
        node('human', 'human_input', title='补充结果', fields=[{'name': 'answer', 'label': '结果', 'type': 'string', 'required': True}]),
        node('end', 'end', outputs={'answer': ref('human', 'answer')})],
        [edge('start', 'save'), edge('save', 'human'), edge('human', 'end')])
    graph(client, pid, [node('start', 'start'), node('call', 'tool', tool_name='workflow:'+member),
        node('end', 'end', outputs={'answer': ref('call', 'output', 'answer')})], [edge('start', 'call'), edge('call', 'end')])
    task = settled(client, base, start(client, base, 'human'))
    assert task['status'] == 'waiting_input', task
    child = next(r for r in task['runs'] if r['application_id'] == member)
    assert child['status'] == 'paused' and child['waiting_node']['id'] == 'human'
    saved = client.post(f'{base}/tasks/{task["id"]}/runs/{child["id"]}/input', json={'values': {'answer': '已收到'}})
    assert saved.status_code == 200, saved.text
    assert client.get(base+'/tasks/'+task['id']).json()['status'] == 'waiting_input'
    client.post(base+'/tasks/'+task['id']+'/resume', json={})
    done = settled(client, base, task)
    assert done['status'] == 'succeeded' and done['outputs']['answer'] == '已收到', done
    assert len(done['runs']) == 2
    assert client.get(base+'/records/progress/once').json()['revision'] == 1


def test_codex_can_start_concurrent_tests_and_stop_them(configured, monkeypatch):
    from agent_platform.requirement_discussion import save_discussion
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    client, app, project, settings = configured
    base, _, _ = fixture_graphs(client, project)
    manager = app.state.services.local_agents
    state = manager.load(project['id'])
    state['phase'] = 'build'
    manager.save(project['id'], state)
    save_discussion(settings.workspace_root/project['id'], {'enabled': True, 'status': 'confirmed',
                    'revision': 1, 'turns': [], 'document': '# 共享资源平台测试'})
    original = _NODE_EXECUTORS['project_record']
    async def slow(runtime, run):
        await asyncio.sleep(.4)
        return await original(runtime, run)
    monkeypatch.setitem(_NODE_EXECUTORS, 'project_record', slow)
    started = []
    for key in ['async-a', 'async-b']:
        response = client.post(base+'/agent-tools', json={'name': 'workflow_run', 'arguments': {
            'action': 'start', 'request_key': key, 'inputs': {'request_id': key}, 'wait': False}})
        assert response.status_code == 200, response.text
        started.append(response.json())
    assert all(client.get(base+'/tasks/'+t['id']).json()['status'] == 'running' for t in started)
    assert client.post(base+'/agent-session/stop').status_code == 200
    for task in started:
        stopped = client.get(base+'/tasks/'+task['id']).json()
        assert stopped['status'] == 'interrupted'
        assert all(r['status'] not in {'queued', 'running'} for r in stopped['runs'])
