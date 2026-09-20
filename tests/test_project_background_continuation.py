"""Normal agent replies wait on real in-flight workers without spending turns."""
import asyncio
import json

import pytest

from agent_platform.workflow_runtime import _NODE_EXECUTORS
from tests.test_projects import configured, graph, node, edge, settled  # noqa: F401
from tests.test_project_conversation import TestSession, configure_agent, put_progress, item
from tests.test_local_agents import settled as agent_settled
from agent_platform.project_conversation import ProjectAction


def background_session(configured, monkeypatch, *, fail=False):
    started, release = asyncio.Event(), asyncio.Event()
    tasks, observed, messages = [], [], []
    original = _NODE_EXECUTORS['end']

    async def held(runtime, run):
        await release.wait()
        if fail:
            raise ValueError('background execution failed')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)

    class Waiting(TestSession):
        turn_id = None

        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turn_id = 'test-turn'
            self.turns += 1
            try:
                if self.turns == 1:
                    await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
                    task = await on_tool('workflow_run', {'action': 'start', 'wait': False})
                    tasks.append(task['id'])
                    started.set()
                else:
                    messages.extend(json.loads(message).get('latest_messages', []))
                    task = await on_tool('workflow_run', {'action': 'inspect', 'task_id': tasks[0]})
                    if task['status'] not in {'queued', 'running'}:
                        observed.append(task)
                        await on_tool('project_action', {'action': 'discuss'})
                await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '已查看当前任务。'}})
                return {'status': 'completed'}
            finally:
                self.turn_id = None

    client, app, project, _, base = configure_agent(configured, monkeypatch, Waiting)
    put_progress(client, base, [item()])
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'ready': True})],
        [edge('start', 'end')])
    assert client.post(base+'/conversation/messages', json={'message': '启动并处理后台结果'}).status_code == 202
    client.portal.call(asyncio.wait_for, started.wait(), 2)
    client.portal.call(asyncio.sleep, .2)
    manager = app.state.services.local_agents
    session = manager.clients.get(project['id'])
    state = client.get(base+'/agent-session').json()
    assert state['status'] == 'running', state['error']
    assert session.turns == 1
    worker = app.state.services.projects.active[tasks[0]]
    assert not worker.done() and not worker.cancelled()
    return client, app, project, base, session, worker, release, tasks[0], observed, messages


@pytest.mark.parametrize('fail', [False, True])
def test_normal_reply_waits_then_handles_original_task_result(configured, monkeypatch, fail):
    client, app, project, base, session, worker, release, task_id, observed, _ = background_session(
        configured, monkeypatch, fail=fail)
    thread_id = client.get(base+'/agent-session').json()['thread_id']
    client.portal.call(release.set)
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert session.turns == 2 and state['thread_id'] == thread_id
    assert len(observed) == 1 and observed[0]['id'] == task_id
    assert observed[0]['status'] == ('failed' if fail else 'succeeded')
    if fail:
        assert 'background execution failed' in observed[0]['error']
    else:
        assert observed[0]['outputs'] == {'ready': True}
    assert worker.done() and not worker.cancelled()
    assert len(client.get(base+'/tasks').json()) == 1


def test_explicit_stop_still_cancels_worker_while_agent_waits(configured, monkeypatch):
    client, app, project, base, session, worker, release, task_id, _, _ = background_session(configured, monkeypatch)
    response = client.post(base+'/agent-session/stop')
    assert response.status_code == 200 and response.json()['status'] == 'interrupted'
    task = client.get(base+'/tasks/'+task_id).json()
    assert task['status'] == 'interrupted' and worker.done()
    assert task['runs'] and all(run['status'] == 'cancelled' for run in task['runs'])
    assert session.turns == 1 and not release.is_set()


def test_stopping_individual_task_does_not_wake_agent_and_manual_resume_keeps_task(configured, monkeypatch):
    client, app, project, base, session, worker, release, task_id, observed, _ = background_session(configured, monkeypatch)
    response = client.post(base + '/tasks/' + task_id + '/stop')
    assert response.status_code == 200 and response.json()['status'] == 'interrupted'
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert session.turns == 1 and observed == []
    assert worker.done() and not release.is_set()
    client.portal.call(release.set)
    resumed = client.post(base + '/tasks/' + task_id + '/resume', json={})
    assert resumed.status_code == 202
    assert settled(client, base, resumed.json())['status'] == 'succeeded'
    assert len(client.get(base+'/tasks').json()) == 1
    assert session.turns == 1


@pytest.mark.parametrize('outcome', ['succeeded', 'failed', 'interrupted'])
def test_wait_observes_existing_task_without_progress_item(configured, monkeypatch, outcome):
    client, app, project, _, base = configure_agent(configured, monkeypatch, TestSession)
    services = app.state.services
    state = services.local_agents.load(project['id'])
    state['conversation_enabled'] = True
    services.local_agents.save(project['id'], state)
    started, release = asyncio.Event(), asyncio.Event()
    original = _NODE_EXECUTORS['end']

    async def held(runtime, run):
        started.set()
        await release.wait()
        if outcome == 'failed':
            raise ValueError('training task failed')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'ready': True})],
          [edge('start', 'end')])
    progress = client.get(base + '/progress').json()
    task = client.post(base + '/tasks', json={'request_key': 'wait-original'}).json()
    client.portal.call(asyncio.wait_for, started.wait(), 2)
    action = ProjectAction(action='wait', task_id=task['id'])
    waiting = client.portal.start_task_soon(services.projects.conversation.action, project['id'], action)
    client.portal.call(asyncio.sleep, .05)
    assert not waiting.done()
    if outcome == 'interrupted':
        assert client.post(base + '/tasks/' + task['id'] + '/stop').status_code == 200
    else:
        client.portal.call(release.set)
    result = waiting.result(timeout=3)
    assert result['id'] == task['id'] and result['status'] == outcome
    if outcome == 'succeeded':
        assert result['outputs'] == {'ready': True}
    if outcome == 'failed':
        assert 'training task failed' in result['error']
    # Re-reading a completed or stopped task must not start any new work.
    again = client.portal.call(services.projects.conversation.action, project['id'], action)
    assert again['status'] == outcome
    assert len(client.get(base + '/tasks').json()) == 1
    assert client.get(base + '/progress').json() == progress
    other = client.post('/api/v1/projects', json={'name': '另一个项目'}).json()
    state = services.local_agents.load(other['id'])
    state['conversation_enabled'] = True
    services.local_agents.save(other['id'], state)
    with pytest.raises(KeyError):
        client.portal.call(services.projects.conversation.action, other['id'], action)


def test_discuss_ends_waiting_session_without_cancelling_worker(configured, monkeypatch):
    client, app, project, base, session, worker, release, task_id, _, _ = background_session(configured, monkeypatch)
    assert client.post(base+'/agent-tools', json={'name': 'project_action', 'arguments': {'action': 'discuss'}}).status_code == 200
    state = agent_settled(client, base)
    assert state['status'] == 'idle' and state['continue_work'] is False
    assert session.turns == 1 and not worker.done()
    client.portal.call(release.set)
    assert settled(client, base, {'id': task_id})['status'] == 'succeeded'


def test_customer_message_is_processed_while_background_task_keeps_running(configured, monkeypatch):
    client, app, project, base, session, worker, release, task_id, observed, messages = background_session(configured, monkeypatch)
    assert client.post(base+'/conversation/messages', json={'message': '补充：完成后说明结果'}).status_code == 202

    async def received():
        while not messages:
            await asyncio.sleep(.01)

    client.portal.call(asyncio.wait_for, received(), 2)
    assert messages == ['补充：完成后说明结果']
    client.portal.call(asyncio.sleep, .1)
    assert session.turns == 2 and not worker.done()
    client.portal.call(release.set)
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert session.turns == 3 and len(observed) == 1


def test_reply_without_background_work_ends_without_extra_model_calls(configured, monkeypatch):
    turns = []

    class EmptyReply(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            turns.append(message)
            if len(turns) == 1:
                await on_tool('project_action', {'action': 'build', 'item_id': 'allocate'})
            return {'status': 'completed'}

    client, app, project, _, base = configure_agent(configured, monkeypatch, EmptyReply)
    put_progress(client, base, [item()])
    assert client.post(base+'/conversation/messages', json={'message': '继续执行'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle' and not state['error']
    assert len(turns) == 1 and client.get(base+'/tasks').json() == []


@pytest.mark.parametrize('tool_name,extra', [('project_action', {'action':'wait'}),
                                            ('workflow_run', {'action':'inspect','wait_seconds':30})])
def test_stopped_active_wait_ends_raw_model_turn_but_history_remains_readable(configured, monkeypatch, tool_name, extra):
    from types import SimpleNamespace
    from agent_platform.connected_model import completion_events
    from agent_platform.model_connections import ModelConnections

    client, app, project, settings = configured
    base = '/api/v1/projects/' + project['id']
    entered, release = asyncio.Event(), asyncio.Event()
    original = _NODE_EXECUTORS['end']

    async def held(runtime, run):
        entered.set()
        await release.wait()
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)
    graph(client, project['id'], [node('s','start'), node('e','end')], [edge('s','e')])
    task = client.post(base+'/tasks', json={'request_key':'live-wait-stop'}).json()
    client.portal.call(asyncio.wait_for, entered.wait(), 2)
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 2:
            blocks = [{'type':'tool_use','id':'wait-'+str(len(calls)), 'name':tool_name,
                       'input':{**extra,'task_id':task['id']}}]
            reason = 'tool_use'
        else:
            blocks, reason = [{'type':'text','text':'历史中断状态可读'}], 'end_turn'
        for e in completion_events(blocks, stop_reason=reason):
            yield e

    monkeypatch.setattr(ModelConnections, 'provider', lambda *a, **k: SimpleNamespace(stream=stream))
    client.put(base+'/agent-session', json={'provider':'api','model':'test',
               'base_url':'https://example.test/v1','api_key':'test'}).raise_for_status()
    client.post(base+'/conversation/messages', json={'message':'等待原任务'}).raise_for_status()

    async def waiting():
        while not any(e['kind']=='tool_started' and e['text']==tool_name
                      for e in app.state.services.local_agents.load(project['id'])['events']):
            await asyncio.sleep(.01)
    client.portal.call(asyncio.wait_for, waiting(), 2)
    client.post(base+'/tasks/'+task['id']+'/stop').raise_for_status()
    state = agent_settled(client, base)
    assert state['status'] == 'interrupted' and len(calls) == 1
    assert client.get(base+'/tasks/'+task['id']).json()['status'] == 'interrupted'
    # A new explicit request can inspect the old interruption and answer normally.
    client.post(base+'/conversation/messages', json={'message':'只查看刚才的状态'}).raise_for_status()
    state = agent_settled(client, base)
    assert state['status'] == 'idle' and len(calls) == 3
    assert len(client.get(base+'/tasks').json()) == 1
