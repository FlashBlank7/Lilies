"""Customer conversation, incremental delivery and recovery over the real runtime."""
import asyncio
import json
import time
import pytest
from uuid import uuid4

from tests.test_projects import configured as configured  # noqa: F401
from tests.test_projects import graph, node, edge, start, fixture_graphs, ref, settled
from tests.test_local_agents import settled as agent_settled
from agent_platform.requirement_discussion import save_discussion


def item(id='allocate', **changes):
    return {'id': id, 'title': '申请分配', 'goal': '申请获得资源或明确等待',
            'next_action': '搭建并试用资源分配', **changes}


def put_progress(client, base, items, revision=0):
    return client.put(base+'/progress', json={'expected_revision': revision,
        'value': {'goal': '处理申请并改善预测', 'summary': '边建设边试用', 'items': items}})


def configure_agent(configured, monkeypatch, agent_class):
    client, app, project, settings = configured
    base = '/api/v1/projects/'+project['id']
    monkeypatch.setattr('agent_platform.local_agents.ModelSession', agent_class)
    assert client.put(base+'/agent-session', json={'provider': 'api', 'model': 'test', 'base_url': 'https://example.test/v1', 'api_key': 'test'}).status_code == 200
    save_discussion(settings.workspace_root/project['id'], {'enabled': True, 'status': 'confirmed',
        'revision': 2, 'document': '# 测试需求\n申请分配以及独立的预测研究。', 'turns': []})
    return client, app, project, settings, base


class TestSession:
    __test__ = False
    def __init__(self, *args, **kwargs):
        self.turns = 0
    async def start(self, tools, instructions, thread_id=None):
        assert {'project_progress', 'project_action'} <= {t['name'] for t in tools}
        return thread_id or 'conversation-test'
    async def close(self):
        pass
    async def turn(self, message, on_event, on_tool, **kwargs):
        await on_tool('project_action', {'action': 'inspect'})
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '分配可以继续建设，预测在等待数据。'}})
        return {'status': 'completed'}


@pytest.mark.parametrize('action', ['inspect', 'finish', 'discuss', 'wait', 'build'])
def test_phase_change_preserves_messages_received_during_await(configured, monkeypatch, action):
    from agent_platform.project_conversation import ProjectAction
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    put_progress(client, base, [item(questions=[{'id': 'q', 'text': '还缺什么？',
        'impact': '当前事项', 'next_action': '补充后继续'}])])
    manager = app.state.services.local_agents
    state = manager.load(project['id'])
    state.update(conversation_enabled=True, phase='coordinate', continue_work=False)
    manager.save(project['id'], state)
    store = app.state.services.projects.store
    original = store.progress
    injected = False

    async def interleaved(project_id):
        nonlocal injected
        result = await original(project_id)
        if not injected:
            injected = True
            manager.event(project_id, 'user', '执行中补充：保留原材料')
            latest = manager.load(project_id)
            latest['pending_messages'] = ['执行中补充：保留原材料']
            latest['conversation_context'] = {'item_id': 'allocate', 'question_id': 'q'}
            manager.save(project_id, latest)
        return result

    monkeypatch.setattr(store, 'progress', interleaved)
    client.portal.call(app.state.services.projects.conversation.action, project['id'],
                       ProjectAction(action=action, item_id='allocate'))
    current = manager.load(project['id'])
    event = next(e for e in current['events'] if e.get('text') == '执行中补充：保留原材料')
    assert current['pending_messages'] == ['执行中补充：保留原材料']
    assert current['conversation_context']['question_id'] == 'q'
    assert client.get(base + '/conversation?after=' + event['id']).status_code == 200


def test_supplement_arriving_while_loading_progress_reaches_the_next_turn(configured, monkeypatch):
    seen = []
    class CapturingSession(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            seen.extend(json.loads(message).get('latest_messages', []))
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, CapturingSession)
    put_progress(client, base, [item()])
    manager = app.state.services.local_agents
    state = manager.load(project['id'])
    state['pending_messages'] = ['先到的补充']
    manager.save(project['id'], state)
    store = app.state.services.projects.store
    original = store.progress
    injected = False
    async def interleaved(project_id):
        nonlocal injected
        result = await original(project_id)
        if not injected and manager.load(project_id)['status'] == 'running':
            injected = True
            manager.event(project_id, 'user', '加载进展期间收到的补充')
            current = manager.load(project_id)
            current.setdefault('pending_messages', []).append('加载进展期间收到的补充')
            manager.save(project_id, current)
        return result
    monkeypatch.setattr(store, 'progress', interleaved)
    assert client.post(base + '/conversation/messages', json={'message': '继续当前事项'}).status_code == 202
    state = agent_settled(client, base)
    assert seen == ['先到的补充', '加载进展期间收到的补充']
    assert any(e.get('text') == '加载进展期间收到的补充' for e in state['events'])
    assert not state.get('pending_messages')


def test_progress_revision_and_cross_project_references(configured):
    client, app, project, settings = configured
    base = '/api/v1/projects/'+project['id']
    assert client.get(base+'/progress').json()['revision'] == 0
    first = put_progress(client, base, [item(workflow_ids=[project['id']], availability='trial')])
    assert first.status_code == 200
    assert first.json()['value']['items'][0]['status'] == 'planned'
    assert put_progress(client, base, [item()], revision=0).status_code == 409
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    assert put_progress(client, base, [item(workflow_ids=[other])], revision=1).status_code == 422
    for path in ['../other/secret', '/etc/passwd', 'results/missing.json']:
        assert put_progress(client, base, [item(results=[{'label': '结果', 'file_path': path}])], revision=1).status_code == 422
    outside_task = start(client, '/api/v1/projects/'+other, 'outside')
    assert put_progress(client, base, [item(task_ids=[outside_task['id']])], revision=1).status_code == 404
    assert put_progress(client, base, [item(status='waiting')], revision=1).status_code == 422
    persisted = client.get(base+'/progress').json()
    assert persisted == first.json()


def test_status_question_does_not_reopen_requirements_or_execute(configured, monkeypatch):
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    put_progress(client, base, [item()])
    before = client.get(base+'/requirements').json()
    assert client.post(base+'/conversation/messages', json={'message': '现在做到哪了？'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert client.get(base+'/requirements').json() == before
    assert client.get(base+'/tasks').json() == []
    assert client.get(base+'/progress').json()['value']['items'][0]['status'] == 'planned'
    page = client.get(base+'/conversation').json()
    assert [e['kind'] for e in page['events']] == ['user', 'assistant']
    assert 'outputs' not in page


def test_customer_answer_is_durable_and_bound_to_item(configured, monkeypatch):
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    question = {'id': 'coordinate', 'text': '坐标从哪端开始？', 'impact': '决定测点位置', 'next_action': '检查坐标映射'}
    put_progress(client, base, [item(status='waiting', questions=[question]), item('other')])
    assert client.post(base+'/conversation/messages', json={'message': '从头部开始', 'item_id': 'other', 'question_id': 'coordinate'}).status_code == 422
    assert client.post(base+'/conversation/messages', json={'message': '从头部开始', 'item_id': 'allocate', 'question_id': 'coordinate'}).status_code == 202
    agent_settled(client, base)
    answer = client.get(base+'/progress').json()
    assert answer['value']['items'][0]['questions'][0]['answer'] == '从头部开始'
    # The customer can correct their answer; an agent's stale summary cannot erase it.
    assert client.post(base+'/conversation/messages', json={'message': '更正：尾部', 'item_id': 'allocate', 'question_id': 'coordinate'}).status_code == 202
    agent_settled(client, base)
    current = client.get(base+'/progress').json()
    current['value']['items'][0]['questions'][0]['answer'] = '从头部开始'
    written = client.put(base+'/progress', json={'expected_revision': current['revision'], 'value': current['value']})
    assert written.json()['value']['items'][0]['questions'][0]['answer'] == '更正：尾部'
    assert client.get(base+'/requirements').json()['status'] == 'confirmed'


def test_local_wait_does_not_end_authorized_building(configured, monkeypatch):
    class Building(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turns += 1
            p = await on_tool('project_progress', {'view': 'full'})
            if self.turns == 1:
                await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
                draft = await on_tool('workflow_draft', {})
                await on_tool('workflow_draft', {'operation': {'op': 'replace_workflow', 'expected_revision': draft['revision'],
                    'idempotency_key': 'working', 'data': {'workflow': {'nodes': [node('s', 'start'), node('e', 'end', outputs={'ready': True})], 'edges': [edge('s', 'e')]}}}})
                # End the model turn while actionable work remains. The platform must invoke the next turn.
            else:
                run = await on_tool('project_action', {'action': 'trial', 'item_id': 'allocate', 'inputs': {}})
                assert run['status'] == 'succeeded' and run['outputs']['ready']
                await on_tool('project_task_result', {'status': 'succeeded', 'message': '分配可以试用', 'markdown': '|结果|值|\n|---|---|\n|可处理|是|', 'outputs': {'forged': 1}})
                p = await on_tool('project_progress', {'view': 'full'})
                p['value']['items'][0].update(status='done', availability='trial', summary='已完成本次试用')
                await on_tool('project_progress', {'action': 'update', 'expected_revision': p['revision'], 'value': p['value']})
                await on_tool('project_action', {'action': 'finish'})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Building)
    put_progress(client, base, [item(), item('prediction', status='waiting', blocker={'kind': 'data', 'owner': '数据负责人', 'reason': '缺少棒身标签', 'next_action': '补充后验证'})])
    client.post(base+'/conversation/messages', json={'message': '其他部分继续做'})
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert app.state.services.local_agents.clients[project['id']].turns == 2
    items = client.get(base+'/progress').json()['value']['items']
    assert items[0]['availability'] == 'trial' and items[0]['status'] == 'done'
    assert items[1]['status'] == 'waiting'
    task = client.get(base+'/tasks?purpose=customer_trial').json()[0]
    assert task['outputs'] == {'ready': True}  # Presentation cannot replace actual runtime output.
    assert task['presentation']['markdown'].startswith('|结果|')
    assert len(client.get(base+'/tasks/'+task['id']).json()['runs']) == 1


def test_discuss_disables_auto_continuation_without_cancelling_background_task(configured, monkeypatch):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    task_started, finish_reply, finish_workflow = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = _NODE_EXECUTORS['end']
    task_ids = []

    async def held(runtime, run):
        await finish_workflow.wait()
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)

    class BackgroundWork(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turns += 1
            if self.turns == 1:
                await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
                task = await on_tool('workflow_run', {'action': 'start', 'wait': False})
                task_ids.append(task['id'])
                task_started.set()
                await finish_reply.wait()
            else:
                assert '查看已有结果' in json.loads(message)['user_message']
                task = await on_tool('workflow_run', {'action': 'inspect', 'task_id': task_ids[0]})
                assert task['status'] == 'succeeded' and task['outputs'] == {'ready': True}
            await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '本轮答复结束。'}})
            return {'status': 'completed'}

    client, app, project, _, base = configure_agent(configured, monkeypatch, BackgroundWork)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'ready': True})],
        [edge('start', 'end')])
    assert client.post(base+'/conversation/messages', json={'message': '开始后台执行'}).status_code == 202
    client.portal.call(asyncio.wait_for, task_started.wait(), 2)
    before = client.get(base+'/agent-session').json()
    progress = client.get(base+'/progress').json()
    assert before['status'] == 'running' and before['continue_work'] is True
    worker = app.state.services.projects.active[task_ids[0]]

    response = client.post(base+'/agent-tools', json={'name': 'project_action', 'arguments': {'action': 'discuss'}})
    assert response.status_code == 200, response.text
    current = client.get(base+'/agent-session').json()
    assert current['phase'] == 'discuss' and current['continue_work'] is False
    assert current['status'] == 'running' and not worker.done()
    assert current['requirements'] == before['requirements']
    assert client.get(base+'/progress').json() == progress

    client.portal.call(finish_reply.set)
    current = agent_settled(client, base)
    assert current['status'] == 'idle', current['error']
    assert current['thread_id'] == before['thread_id']
    assert app.state.services.local_agents.clients[project['id']].turns == 1
    assert not worker.done() and not worker.cancelled()
    assert client.get(base+'/tasks/'+task_ids[0]).json()['status'] == 'running'
    client.portal.call(finish_workflow.set)
    done = settled(client, base, {'id': task_ids[0]})
    assert done['status'] == 'succeeded' and done['outputs'] == {'ready': True}

    assert client.post(base+'/conversation/messages', json={'message': '查看已有结果'}).status_code == 202
    resumed = agent_settled(client, base)
    assert resumed['status'] == 'idle', resumed['error']
    assert resumed['thread_id'] == before['thread_id']
    assert resumed['requirements'] == before['requirements']
    assert len(client.get(base+'/tasks').json()) == 1


def test_feedback_uses_new_snapshot_and_links_previous_result(configured, monkeypatch):
    class Feedback(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turns += 1
            context = json.loads(message)
            if self.turns == 2:
                await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
                draft = await on_tool('workflow_draft', {})
                await on_tool('workflow_draft', {'operation': {'op': 'update_node', 'expected_revision': draft['revision'],
                    'idempotency_key': 'feedback', 'data': {'node_id': 'end', 'changes': {'config': {'outputs': {'quantity': 2}}}}}})
            result = await on_tool('project_action', {'action': 'trial', 'item_id': 'allocate', 'inputs': {},
                'feedback_task_id': context['conversation_context'].get('task_id', '')})
            await on_tool('project_task_result', {'status': 'succeeded', 'message': '分配结果', 'markdown': '数量：'+str(result['outputs']['quantity'])})
            p = await on_tool('project_progress', {'view': 'full'})
            p['value']['items'][0].update(status='done', availability='trial')
            await on_tool('project_progress', {'action': 'update', 'expected_revision': p['revision'], 'value': p['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Feedback)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'quantity': 1})], [edge('start', 'end')])
    client.post(base+'/conversation/messages', json={'message': '试一下', 'item_id': 'allocate'})
    agent_settled(client, base)
    old = client.get(base+'/tasks').json()[0]
    old_detail = client.get(base+'/tasks/'+old['id']).json()
    client.post(base+'/conversation/messages', json={'message': '数量应该是2，修改后再试', 'item_id': 'allocate', 'task_id': old['id']})
    assert agent_settled(client, base)['status'] == 'idle'
    new = client.get(base+'/tasks').json()[0]
    assert new['id'] != old['id'] and new['feedback_task_id'] == old['id']
    assert new['outputs']['quantity'] == 2
    unchanged = client.get(base+'/tasks/'+old['id']).json()
    assert unchanged['outputs'] == old_detail['outputs'] and unchanged['runs'] == old_detail['runs']
    assert client.get(base+'/requirements').json()['status'] == 'confirmed'


def test_history_is_incremental_and_usage_is_explicit(configured, monkeypatch):
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    manager = app.state.services.local_agents
    for i in range(65):
        manager.event(project['id'], 'assistant', f'进展{i}')
        manager.event(project['id'], 'tool', 'project_file', result='x'*1000, success=True)
    page = client.get(base+'/conversation').json()
    assert len(page['events']) == 50 and page['events'][0]['text'] == '进展15' and page['has_more']
    earlier = client.get(base+'/conversation', params={'before': page['first_cursor']}).json()
    assert len(earlier['events']) == 15 and not earlier['has_more']
    assert client.get(base+'/conversation', params={'after': page['last_cursor']}).json()['events'] == []
    manager.event(project['id'], 'assistant', '最新进展')
    assert client.get(base+'/conversation', params={'after': page['last_cursor']}).json()['events'][0]['text'] == '最新进展'
    assert len(client.get(base+'/conversation?kind=tools').json()['events']) == 50
    assert client.get(base+'/conversation?after=other-project').status_code == 422
    member = client.post(base+'/members', json={'name': '业务名不能判断用途', 'purpose': 'test'}).json()
    assert client.get(base).json()['members'][-1]['purpose'] == 'test'
    assert client.patch(base+'/members/'+member['id'], json={'purpose': 'business'}).status_code == 200
    assert client.get(base).json()['members'][-1]['purpose'] == 'business'


def test_repeated_failure_pauses_one_item_and_continues_another(configured, monkeypatch):
    class Failing(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turns += 1
            if self.turns == 1:
                await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
                for attempt in range(3):
                    await on_tool('project_file', {'action': 'write', 'path': 'solution/repair.txt', 'content': str(attempt)})
                    try:
                        await on_tool('workflow_draft', {'operation': {'op': 'update_node', 'expected_revision': 999,
                            'idempotency_key': 'bad', 'data': {'node_id': str(uuid4()), 'changes': {'title': 'bad'}}}})
                    except Exception:
                        pass
            else:
                p = await on_tool('project_progress', {'view': 'full'})
                p['value']['items'][1].update(status='done', summary='另一个事项已处理')
                await on_tool('project_progress', {'action': 'update', 'expected_revision': p['revision'], 'value': p['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Failing)
    put_progress(client, base, [item(), item('other')])
    client.post(base+'/conversation/messages', json={'message': '继续搭建'})
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    progress = client.get(base+'/progress').json()['value']['items']
    assert progress[0]['status'] == 'waiting' and progress[0]['blocker']['kind'] == 'runtime'
    assert progress[1]['status'] == 'done'


@pytest.mark.parametrize('inspect_history', [True, False])
def test_failure_pause_counts_executions_not_failed_task_reads(configured, monkeypatch, inspect_history):
    observed = []

    class Inspecting(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            failed = await on_tool('workflow_run', {'action': 'start'})
            assert failed['status'] == 'failed'
            for _ in range(2):
                arguments = {'action': 'inspect', 'task_id': failed['id']} if inspect_history else {'action': 'start'}
                result = await on_tool('workflow_run', arguments)
                assert result['status'] == 'failed' and result['error'] == failed['error']
            progress = await on_tool('project_progress', {'view': 'full'})
            observed.append(progress['value']['items'][0]['status'])
            # End this scripted observation without scheduling another model turn.
            progress['value']['items'][0].update(status='done', blocker=None)
            await on_tool('project_progress', {'action': 'update', 'expected_revision': progress['revision'],
                'value': progress['value']})
            return {'status': 'completed'}

    client, app, project, _, base = configure_agent(configured, monkeypatch, Inspecting)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('start', 'start'),
        node('read', 'project_record', action='get', collection='items', key='missing'),
        node('end', 'end', outputs={'value': ref('read', 'value', 'missing')})],
        [edge('start', 'read'), edge('read', 'end')])
    assert client.post(base + '/conversation/messages', json={'message': '检查运行错误'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert observed == ['working' if inspect_history else 'waiting']
    assert bool(state.get('blocked_this_request')) is not inspect_history
    assert len(client.get(base + '/tasks').json()) == (1 if inspect_history else 3)
    operations = [e for e in state['events'] if e['kind'] == 'tool' and e['text'] == 'workflow_run']
    assert [e['success'] for e in operations] == ([False, True, True] if inspect_history else [False] * 3)


def test_corrected_tool_arguments_do_not_pause_authorized_building(configured, monkeypatch):
    from pydantic import ValidationError
    from agent_platform.project_store import ProjectConflict

    class Correcting(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            await on_tool('project_file', {'action': 'write', 'path': 'solution/input.txt', 'content': 'ready'})
            for _ in range(5):
                with pytest.raises(ValidationError):
                    await on_tool('project_file', {'action': 'read', 'path': 'solution/input.txt', 'limit': 25000})
            await on_tool('project_file', {'action': 'read', 'path': 'solution/input.txt', 'limit': 2000})
            for _ in range(3):
                with pytest.raises(ValidationError, match='expected_revision'):
                    await on_tool('project_progress', {'action':'patch', 'item_id':'allocate', 'changes':{'summary':'missing revision'}})
                with pytest.raises(ProjectConflict):
                    await on_tool('project_progress', {'action':'patch', 'item_id':'allocate', 'expected_revision':0, 'changes':{'summary':'stale'}})
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            await on_tool('project_file', {'action': 'write', 'path': 'solution/fixed.txt', 'content': 'continued'})
            progress = await on_tool('project_progress', {'view': 'full'})
            progress['value']['items'][0].update(status='done', summary='参数纠正后继续完成')
            await on_tool('project_progress', {'action': 'update', 'expected_revision': progress['revision'],
                'value': progress['value']})
            return {'status': 'completed'}

    client, app, project, settings, base = configure_agent(configured, monkeypatch, Correcting)
    put_progress(client, base, [item()])
    client.post(base+'/conversation/messages', json={'message': '继续搭建'})
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert (settings.workspace_root/project['id']/'solution/fixed.txt').read_text() == 'continued'
    assert not app.state.services.local_agents.load(project['id']).get('blocked_this_request')
    progress = client.get(base+'/progress').json()['value']['items'][0]
    assert progress['status'] == 'done' and progress['blocker'] is None
    assert len([e for e in state['events'] if e.get('success') is False]) == 11


def test_stop_and_restart_preserve_progress_and_do_not_auto_execute(configured, monkeypatch):
    class Slow(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            await asyncio.sleep(10)
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Slow)
    put_progress(client, base, [item()])
    client.post(base+'/conversation/messages', json={'message': '继续'})
    for _ in range(200):
        if client.get(base+'/progress').json()['value']['items'][0]['status'] == 'working':
            break
        time.sleep(.01)
    assert client.post(base+'/agent-session/stop').status_code == 200
    progress = client.get(base+'/progress').json()
    assert progress['value']['items'][0]['status'] == 'paused'
    assert progress['value']['items'][0]['next_action']
    state = app.state.services.local_agents.load(project['id'])
    state['status'] = 'running'
    app.state.services.local_agents.save(project['id'], state)
    client.portal.call(app.state.services.local_agents.initialize)
    assert client.get(base+'/agent-session').json()['status'] == 'interrupted'
    assert client.get(base+'/progress').json() == progress
    assert client.get(base+'/tasks').json() == []


def test_conversation_resume_keeps_frozen_members_and_does_not_repeat_claims(configured, monkeypatch):
    class Resume(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            task_id = context['conversation_context'].get('task_id')
            result = await on_tool('project_action', {'action': 'resume' if task_id else 'trial',
                'item_id': 'allocate', 'task_id': task_id or '', 'inputs': {} if task_id else {'request_id': 'customer'}})
            await on_tool('project_task_result', {'status': result['status'], 'message': '已分配' if task_id else '等待资源释放'})
            p = await on_tool('project_progress', {'view': 'full'})
            p['value']['items'][0].update(status='done' if task_id else 'waiting', availability='trial',
                blocker=None if task_id else {'kind': 'decision', 'owner': '资源负责人', 'reason': '资源已占用', 'next_action': '释放后继续'})
            await on_tool('project_progress', {'action': 'update', 'expected_revision': p['revision'], 'value': p['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Resume)
    _, validate, allocate_id = fixture_graphs(client, project)
    put_progress(client, base, [item()])
    client.put(base+'/records/resources/only', json={'expected_revision': 0, 'value': {'owner': 'occupied'}})
    client.post(base+'/conversation/messages', json={'message': '试用分配', 'item_id': 'allocate'})
    assert agent_settled(client, base)['status'] == 'idle'
    old = client.get(base+'/tasks').json()[0]
    detail = client.get(base+'/tasks/'+old['id']).json()
    validation_run = next(r for r in detail['runs'] if r['application_id'] == validate)
    assert old['status'] == 'waiting_input'
    graph(client, allocate_id, [node('start', 'start'), node('end', 'end', outputs={'wrong_version': True})], [edge('start', 'end')])
    client.put(base+'/records/resources/only', json={'expected_revision': 1, 'value': {'owner': ''}})
    client.post(base+'/conversation/messages', json={'message': '资源已释放，继续原申请', 'item_id': 'allocate', 'task_id': old['id']})
    assert agent_settled(client, base)['status'] == 'idle'
    done = client.get(base+'/tasks/'+old['id']).json()
    assert done['status'] == 'succeeded' and done['outputs']['result']['allocated']
    assert next(r for r in done['runs'] if r['application_id'] == validate)['id'] == validation_run['id']
    assert client.get(base+'/records/requests/customer').json()['revision'] == 1
    resource = client.get(base+'/records/resources/only').json()
    assert resource['revision'] == 3 and resource['value']['owner'] == 'customer'
    assert len(client.get(base+'/tasks').json()) == 1


def test_tool_upgrade_keeps_platform_thread_and_project_context(configured, monkeypatch):
    class Upgrade(TestSession):
        async def start(self, tools, instructions, thread_id=None):
            assert thread_id == 'old-tools-thread'
            return thread_id
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            assert context['progress']['items'][0]['id'] == 'allocate'
            await on_tool('project_action', {'action': 'inspect'})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Upgrade)
    put_progress(client, base, [item()])
    manager = app.state.services.local_agents
    manager.event(project['id'], 'assistant', '上次已完成试用')
    state = manager.load(project['id'])
    state.update(thread_id='old-tools-thread', tool_contract='old')
    original_session = state['session_id']
    manager.save(project['id'], state)
    client.post(base+'/conversation/messages', json={'message': '现在做到哪了'})
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert state['thread_id'] == 'old-tools-thread' and not state.get('previous_threads')
    assert state['session_id'] == original_session
    assert any(e['text'] == '上次已完成试用' for e in state['events'])
    assert client.get(base+'/tasks').json() == []


def test_existing_result_can_be_explained_without_rerun_or_foreign_access(configured, monkeypatch):
    foreign_id = ''
    class Explain(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            task_id = context['conversation_context']['task_id']
            await on_tool('project_action', {'action': 'inspect'})
            for invalid in [{'task_id': foreign_id}, {'task_id': task_id,
                    'artifacts': [{'label': '越界', 'file_path': '../other/result.csv'}]}]:
                with pytest.raises((ValueError, KeyError)):
                    await on_tool('project_task_result', {'status': 'succeeded', 'message': '结果', **invalid})
            await on_tool('project_task_result', {'task_id': task_id, 'status': 'succeeded',
                'message': '申请已校验', 'markdown': '可以分配资源。', 'outputs': {'forged': True}})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Explain)
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'ready': True})], [edge('start', 'end')])
    old = settled(client, base, start(client, base, 'existing'))
    foreign = client.post('/api/v1/projects', json={'name': '另一个项目'}).json()['id']
    foreign_id = start(client, '/api/v1/projects/'+foreign, 'foreign')['id']
    client.post(base+'/conversation/messages', json={'message': '解释这次结果', 'task_id': old['id']})
    assert agent_settled(client, base)['status'] == 'idle'
    result = client.get(base+'/tasks/'+old['id']).json()
    assert result['outputs'] == {'ready': True} and result['runs'] == old['runs']
    assert result['presentation']['markdown'] == '可以分配资源。'
    assert len(client.get(base+'/tasks').json()) == 1


def test_legacy_request_identity_survives_purpose_migration_and_paging(configured):
    from agent_platform.db import connect
    client, app, project, _ = configured
    base = '/api/v1/projects/'+project['id']
    old = settled(client, base, start(client, base, 'old-client'))
    with connect(app.state.services.projects.store.db_path) as db:
        db.execute("UPDATE project_tasks SET purpose='unclassified' WHERE id=?", (old['id'],))
    duplicate = start(client, base, 'old-client')
    assert duplicate['id'] == old['id'] and duplicate['purpose'] == 'unclassified'
    first = settled(client, base, start(client, base, 'trial-one', purpose='customer_trial'))
    second = settled(client, base, start(client, base, 'trial-two', purpose='customer_trial'))
    page = client.get(base+'/tasks?purpose=customer_trial&compact=true&limit=1').json()
    assert [t['id'] for t in page] == [second['id']] and 'outputs' not in page[0]
    earlier = client.get(base+'/tasks', params={'purpose': 'customer_trial', 'before': second['id'], 'limit': 1}).json()
    assert [t['id'] for t in earlier] == [first['id']]


def test_bound_question_supplies_real_paused_node_without_repeating_record(configured, monkeypatch):
    class Answer(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            task_id = context['conversation_context']['task_id']
            task = await on_tool('workflow_run', {'action': 'inspect', 'task_id': task_id})
            child = next(r for r in task['runs'] if r.get('waiting_node', {}).get('id') == 'human')
            progress = await on_tool('project_progress', {'view': 'full'})
            answer = progress['value']['items'][0]['questions'][0]['answer']
            result = await on_tool('project_action', {'action': 'resume', 'item_id': 'allocate',
                'task_id': task_id, 'run_id': child['id'], 'inputs': {'answer': answer}})
            assert result['status'] == 'succeeded' and result['outputs']['answer'] == answer
            progress = await on_tool('project_progress', {'view': 'full'})
            progress['value']['items'][0].update(status='done', blocker=None)
            await on_tool('project_progress', {'action': 'update', 'expected_revision': progress['revision'], 'value': progress['value']})
            return {'status': 'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Answer)
    member = client.post(base+'/members', json={'name': '人工补充'}).json()['id']
    graph(client, member, [node('start', 'start'),
        node('save', 'project_record', action='put', collection='progress', key='once', value={'started': True}),
        node('human', 'human_input', fields=[{'name': 'answer', 'label': '结果', 'type': 'string', 'required': True}]),
        node('end', 'end', outputs={'answer': ref('human', 'answer')})],
        [edge('start', 'save'), edge('save', 'human'), edge('human', 'end')])
    graph(client, project['id'], [node('start', 'start'), node('call', 'tool', tool_name='workflow:'+member),
        node('end', 'end', outputs={'answer': ref('call', 'output', 'answer')})], [edge('start', 'call'), edge('call', 'end')])
    task = settled(client, base, start(client, base, 'question'))
    put_progress(client, base, [item(status='waiting', task_ids=[task['id']], questions=[{
        'id': 'answer', 'text': '实测结果是什么？', 'impact': '决定后续分配', 'next_action': '继续原任务'}])])
    client.post(base+'/conversation/messages', json={'message': '已收到', 'item_id': 'allocate', 'question_id': 'answer', 'task_id': task['id']})
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    done = client.get(base+'/tasks/'+task['id']).json()
    assert done['status'] == 'succeeded' and done['outputs']['answer'] == '已收到'
    assert len(done['runs']) == 2
    assert client.get(base+'/records/progress/once').json()['revision'] == 1
