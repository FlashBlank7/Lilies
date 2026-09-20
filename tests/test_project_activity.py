"""Persisted activity is the projection of actual local-agent tool calls."""
import asyncio
import time
from tests.test_projects import configured, graph, node, edge  # noqa: F401
from tests.test_project_conversation import configure_agent, TestSession, put_progress, item
from tests.test_local_agents import settled


def test_activity_has_stable_operations_scopes_and_incremental_pages(configured, monkeypatch):
    class Trial(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'trial', 'item_id': 'allocate', 'inputs': {}})
            await on_tool('project_task_result', {'status': 'succeeded', 'message': '已实际分配两个资源', 'markdown': '|资源|数量|\n|---|---|\n|分配|2|'})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Trial)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('s', 'start'), node('e', 'end', outputs={'quantity': 2})], [edge('s', 'e')])
    client.post(base + '/conversation/messages', json={'message': '试用申请', 'item_id': 'allocate'})
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    page = client.get(base + '/conversation').json()
    result = next(e for e in page['events'] if e['kind'] == 'result')
    task = client.get(base + '/tasks/' + result['task_id']).json()
    assert task['outputs'] == {'quantity': 2} and len(task['runs']) == 1
    activity = client.get(base + '/conversation?kind=activity').json()['events']
    started = activity[0]
    ended = next(op for op in activity if op['operation_id'] == started['operation_id'] and op['status'] == 'completed')
    assert started['status'] == 'running' and ended['status'] == 'completed'
    assert started['operation_id'] == ended['operation_id']
    assert started['request_id'] == ended['request_id'] == result['request_id']
    assert ended['task_id'] == task['id'] and ended['item_id'] == 'allocate'
    assert ended['duration_seconds'] >= 0 and ended['started_at'] and ended['ended_at']
    assert 'arguments' not in page['current_activity'] and 'result' not in page['current_activity']
    assert page['request_activity'][result['request_id']]['status'] == 'completed'
    first = client.get(base + '/conversation?kind=activity&limit=1').json()
    previous = client.get(base + '/conversation', params={'kind': 'activity', 'before': first['first_cursor']}).json()
    later = client.get(base + '/conversation', params={'kind': 'activity', 'after': previous['last_cursor']}).json()
    assert later['events'] == first['events']
    assert client.get(base + '/conversation?kind=activity&request_id=missing').json()['events'] == []
    app.state.services.local_agents.event(project['id'], 'result', '旧记录没有请求关联', request_id='', task_id=task['id'])
    assert not any(e['text'] == '旧记录没有请求关联' for e in client.get(base + '/conversation').json()['events'])
    assert any(e['text'] == '旧记录没有请求关联' for e in client.get(base + '/conversation?kind=tools').json()['events'])
    other = client.post('/api/v1/projects', json={'name': '隔离项目'}).json()['id']
    assert client.get('/api/v1/projects/' + other + '/conversation?kind=activity').json()['events'] == []


def test_batch_edits_and_progress_patches_are_presented_as_writes(configured, monkeypatch):
    class Edit(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            draft = await on_tool('workflow_draft', {})
            await on_tool('workflow_draft', {'batch': {
                'expected_revision': draft['revision'], 'expected_content_hash': draft['content_hash'],
                'idempotency_key': 'edit-output', 'operations': [{'op': 'update_node', 'data': {
                    'node_id': 'e', 'changes': {'config': {'outputs': {'quantity': 3}}}, 'merge_config': False}}]}})
            run = await on_tool('workflow_run', {'action': 'start', 'inputs': {}})
            assert run['outputs'] == {'quantity': 3}
            progress = await on_tool('project_progress', {})
            await on_tool('project_progress', {'action': 'patch', 'expected_revision': progress['revision'],
                'item_id': 'allocate', 'changes': {'status': 'done', 'summary': '数量已调整并验证'}})
            return {'status': 'completed'}
    client, _, project, _, base = configure_agent(configured, monkeypatch, Edit)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('s', 'start'), node('e', 'end', outputs={'quantity': 2})], [edge('s', 'e')])
    client.post(base + '/conversation/messages', json={'message': '调整数量并测试'})
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    activity = client.get(base + '/conversation?kind=activity').json()['events']
    drafts = [op['title'] for op in activity if op['tool_name'] == 'workflow_draft' and op['status'] == 'completed']
    progress = [op['title'] for op in activity if op['tool_name'] == 'project_progress' and op['status'] == 'completed']
    assert drafts == ['读取工作流草稿', '修改工作流草稿']
    assert progress == ['查看项目进展', '更新项目进展']


def test_supplement_shares_request_and_stop_closes_the_operation(configured, monkeypatch):
    from agent_platform.project_agent_tools import WorkspaceProjectTools
    original = WorkspaceProjectTools.call
    async def slow(self, name, arguments):
        if name == 'project_progress':
            await asyncio.sleep(10)
        return await original(self, name, arguments)
    monkeypatch.setattr(WorkspaceProjectTools, 'call', slow)
    class Slow(TestSession):
        async def steer(self, message):
            self.supplement = message
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_progress', {'view': 'full'})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Slow)
    put_progress(client, base, [item()])
    client.post(base + '/conversation/messages', json={'message': '检查进展', 'item_id': 'allocate'})
    for _ in range(200):
        page = client.get(base + '/conversation').json()
        if page['current_activity']:
            break
        time.sleep(.01)
    assert page['current_activity']['status'] == 'running'
    request_id = page['request_id']
    client.post(base + '/conversation/messages', json={'message': '补充：只检查已有记录'})
    users = [e for e in client.get(base + '/conversation').json()['events'] if e['kind'] == 'user']
    assert '补充：只检查已有记录' in app.state.services.local_agents.clients[project['id']].supplement
    assert len(users) == 2 and {e['request_id'] for e in users} == {request_id}
    assert users[-1]['item_id'] == 'allocate'
    assert client.get(base + '/conversation').json()['conversation_context']['item_id'] == 'allocate'
    client.post(base + '/agent-session/stop')
    stopped = client.get(base + '/conversation').json()
    assert stopped['status'] == 'interrupted'
    assert stopped['current_activity']['status'] == 'interrupted'
    assert stopped['current_activity']['operation_id'] == page['current_activity']['operation_id']
    assert stopped['current_activity']['ended_at']


def test_restart_closes_unfinished_operations_without_executing(configured, monkeypatch):
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    manager = app.state.services.local_agents
    state = manager.load(project['id'])
    state.update(status='running', request_id='request-before-restart')
    manager.save(project['id'], state)
    manager.event(project['id'], 'tool_started', 'project_file', operation_id='operation-before-restart',
                  status='running', started_at='2026-09-12T00:00:00+00:00', title='读取项目文件')
    with client.portal.wrap_async_context_manager(_initialize(manager)):
        pass
    page = client.get(base + '/conversation').json()
    assert page['status'] == 'interrupted' and '重启' in page['error']
    assert page['current_activity']['status'] == 'interrupted'
    assert page['current_activity']['request_id'] == 'request-before-restart'
    assert not manager.running(project['id'])


from contextlib import asynccontextmanager
@asynccontextmanager
async def _initialize(manager):
    await manager.initialize()
    yield


def test_saved_test_activity_links_real_task_and_stays_in_development(configured, monkeypatch):
    class SavedTest(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            report = await on_tool('workflow_run', {'action': 'tests'})
            assert report['passed']
            await on_tool('project_task_result', {'task_id': report['project_task_id'], 'status': 'succeeded', 'message': '建设测试通过'})
            progress = await on_tool('project_progress', {'view': 'full'})
            progress['value']['items'][0]['status'] = 'done'
            await on_tool('project_progress', {'action': 'update', 'expected_revision': progress['revision'], 'value': progress['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, SavedTest)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('s', 'start'), node('e', 'end', outputs={'quantity': 2})], [edge('s', 'e')])
    path = '/api/v1/applications/' + project['id'] + '/draft'
    draft = client.get(path).json()
    assert client.post(path, json={'expected_revision': draft['revision'], 'idempotency_key': 'saved-test', 'op': 'add_test',
        'data': {'test': {'id': 'quantity', 'name': '数量', 'requirement': '返回2', 'inputs': {},
                          'assertions': [{'path': ['quantity'], 'operator': 'equals', 'expected': 2}]}}}).status_code == 200
    client.post(base + '/conversation/messages', json={'message': '运行保存测试'})
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    operations = [op for op in client.get(base + '/conversation?kind=activity').json()['events'] if op['tool_name'] == 'workflow_run']
    assert operations[-1]['status'] == 'completed'
    assert operations[1]['task_id'] == operations[-1]['task_id']  # visible before return
    task = client.get(base + '/tasks/' + operations[-1]['task_id']).json()
    assert task['purpose'] == 'build_test' and task['outputs']['passed'] and task['runs']
    assert not any(e['kind'] == 'result' for e in client.get(base + '/conversation').json()['events'])
    assert any(e['kind'] == 'result' for e in client.get(base + '/conversation?kind=tools').json()['events'])


def test_concurrent_tool_calls_keep_separate_task_associations(configured, monkeypatch):
    targets = []
    class Concurrent(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            await asyncio.gather(*(on_tool('workflow_run', {'action': 'start', 'workflow_id': target, 'inputs': {}}) for target in targets))
            progress = await on_tool('project_progress', {'view': 'full'})
            progress['value']['items'][0]['status'] = 'done'
            await on_tool('project_progress', {'action': 'update', 'expected_revision': progress['revision'], 'value': progress['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Concurrent)
    member = client.post(base + '/members', json={'name': '第二条测试流程', 'purpose': 'test'}).json()['id']
    targets.extend([project['id'], member])
    put_progress(client, base, [item()])
    for quantity, target in enumerate(targets, 1):
        graph(client, target, [node('s', 'start'), node('e', 'end', outputs={'quantity': quantity})], [edge('s', 'e')])
    client.post(base + '/conversation/messages', json={'message': '并行测试两条流程'})
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    calls = [op for op in client.get(base + '/conversation?kind=activity').json()['events'] if op['tool_name'] == 'workflow_run' and op['status'] == 'completed']
    assert len(calls) == 2 and len({op['task_id'] for op in calls}) == 2
    for op in calls:
        task = client.get(base + '/tasks/' + op['task_id']).json()
        assert task['runs'][0]['application_id'] == op['workflow_id']
        assert task['outputs']['quantity'] == targets.index(op['workflow_id']) + 1
