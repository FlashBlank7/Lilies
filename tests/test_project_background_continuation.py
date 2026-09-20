"""Normal agent replies wait on real in-flight workers without spending turns."""
import asyncio
import json

import pytest

from agent_platform.workflow_runtime import _NODE_EXECUTORS
from tests.test_projects import configured, graph, node, edge, settled  # noqa: F401
from tests.test_project_conversation import TestSession, configure_agent, put_progress, item
from tests.test_local_agents import settled as agent_settled


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


def test_empty_replies_without_background_work_still_stop(configured, monkeypatch):
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
    assert state['status'] == 'error' and '连续三轮' in state['error']
    assert len(turns) == 3 and client.get(base+'/tasks').json() == []
