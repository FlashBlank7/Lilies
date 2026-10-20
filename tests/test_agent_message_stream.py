"""Visible replies use protocol substitutes; no model traffic is enabled."""
import asyncio
import copy
import json
import threading
from uuid import uuid4

import pytest

from agent_platform.agent_message_stream import AgentMessageStream
from agent_platform.conversation_scope import conversation_scope
from agent_platform.local_agents import LocalAgents
from tests.test_projects import configured as configured  # noqa: F401
from tests.test_project_conversation import configure_agent, TestSession
from tests.test_local_agents import settled
from tests.test_official_agent import official as official, enable, chat, FakeAgent  # noqa: F401
from tests.test_users import platform as platform, signup, project as create_project  # noqa: F401


@pytest.fixture
def streaming(configured, monkeypatch):
    ready = threading.Event()
    agents = []

    class StreamingSession(TestSession):
        outcome = 'completed'

        async def start(self, *args, **kwargs):
            self.thread_id = await super().start(*args, **kwargs)
            return self.thread_id

        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turn_id = 'turn-current'
            self.release = asyncio.Event()
            self.notify = on_event
            agents.append(self)
            await self.emit('item/reasoning/textDelta', {'delta': '内部推理不可见'})
            await self.emit('item/agentMessage/delta', {'itemId': 'reply', 'delta': '已经读取结果，'})
            ready.set()
            await self.release.wait()
            if self.outcome == 'error':
                raise RuntimeError('离线连接中断')
            if self.outcome == 'missing_completed':
                return {'status': 'completed'}
            for _ in range(2):
                await self.emit('item/completed', {'item': {
                    'type': 'agentMessage', 'id': 'reply', 'phase': 'final_answer',
                    'text': '已核对结果，两个模型的数值对应正确。'}})
            self.turn_id = None
            return {'status': 'completed'}

        async def emit(self, method, params):
            await self.notify(method, {'threadId': self.thread_id, 'turnId': self.turn_id, **params})

    client, app, project, settings, base = configure_agent(configured, monkeypatch, StreamingSession)
    assert client.post(base + '/conversation/messages', json={'message': '解释已有结果'}).status_code == 202
    assert ready.wait(5), client.get(base + '/conversation').text
    yield client, app, project, settings, base, agents[0]
    client.post(base + '/agent-session/stop')


def test_partial_reply_visible_through_cursor_and_replaced_once_on_completion(streaming):
    client, app, project, settings, base, agent = streaming
    page = client.get(base + '/conversation').json()
    partial = page['streaming_message']
    assert partial['text'] == '已经读取结果，'
    assert not any(e['kind'] == 'assistant' for e in page['events'])
    cursor = page['last_cursor']
    assert client.get(base + '/conversation', params={'after': cursor}).json()['streaming_message'] == partial
    # A different manager loading only disk still sees the saved partial reply.
    assert LocalAgents(app.state.services).load(project['id'])['streaming_message'] == partial
    assert '内部推理不可见' not in json.dumps(page, ensure_ascii=False)
    client.portal.call(agent.release.set)
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    final = client.get(base + '/conversation', params={'after': cursor}).json()
    assert not final.get('streaming_message')
    messages = [e for e in final['events'] if e['kind'] == 'assistant']
    assert len(messages) == 1
    assert messages[0]['id'] == partial['id']
    assert messages[0]['text'] == '已核对结果，两个模型的数值对应正确。'
    assert not messages[0].get('incomplete')


@pytest.mark.parametrize('outcome', ['stop', 'error', 'missing_completed'])
def test_partial_reply_survives_stop_error_or_missing_completion(streaming, outcome):
    client, app, project, settings, base, agent = streaming
    partial = client.get(base + '/conversation').json()['streaming_message']
    # Include a just-received fragment inside the checkpoint interval.
    client.portal.call(agent.emit, 'item/agentMessage/delta', {'itemId': 'reply', 'delta': '还有一部分'})
    if outcome == 'stop':
        client.post(base + '/agent-session/stop').raise_for_status()
    else:
        agent.outcome = outcome
        client.portal.call(agent.release.set)
    state = settled(client, base)
    assert state['status'] == {'stop': 'interrupted', 'error': 'error', 'missing_completed': 'idle'}[outcome]
    reply = [e for e in state['events'] if e['kind'] == 'assistant']
    assert len(reply) == 1 and reply[0]['id'] == partial['id']
    assert reply[0]['text'] == '已经读取结果，还有一部分' and reply[0]['incomplete']
    assert not state.get('streaming_message')
    # A late notification after stop must not revive the answer.
    before = app.state.services.local_agents.load(project['id'])
    client.portal.call(agent.emit, 'item/agentMessage/delta', {'itemId': 'reply', 'delta': '过期片段'})
    assert app.state.services.local_agents.load(project['id']) == before


def test_restart_keeps_checkpoint_but_does_not_claim_finished(configured):
    _, app, project, _ = configured
    manager = app.state.services.local_agents
    state = manager.load(project['id'])
    state.update(status='running', request_id='old-request', streaming_message={
        'id': 'partial', 'kind': 'assistant', 'text': '重启前已收到', 'time': '', 'request_id': 'old-request'})
    manager.save(project['id'], state)
    app.state.services.local_agents = LocalAgents(app.state.services)
    recovered = app.state.services.local_agents
    configured[0].portal.call(recovered.initialize)
    state = recovered.load(project['id'])
    assert state['status'] == 'interrupted' and not state.get('streaming_message')
    assert state['events'][0]['text'] == '重启前已收到' and state['events'][0]['incomplete']


def test_partial_isolated_by_conversation_request_and_native_turn(configured):
    client, app, project, _ = configured
    manager = app.state.services.local_agents
    project_id = project['id']
    with conversation_scope(project_id, str(uuid4())):
        state = manager.load(project_id)
        state['request_id'] = 'request-a'
        manager.save(project_id, state)
        reply = AgentMessageStream(manager, project_id, 'request-a')
        for thread, turn in [('foreign', 'current'), ('thread', 'stale')]:
            reply.handle('item/agentMessage/delta', {'threadId': thread, 'turnId': turn, 'itemId': 'x', 'delta': '不应显示'},
                         thread_id='thread', turn_id='current')
        assert not manager.load(project_id).get('streaming_message')
        reply.handle('item/agentMessage/delta', {'itemId': 'x', 'delta': '本会话的正文'})
        assert manager.load(project_id)['streaming_message']['text'] == '本会话的正文'
        with conversation_scope(project_id, str(uuid4())):
            assert not manager.load(project_id).get('streaming_message')
        state = manager.load(project_id)
        state.update(request_id='request-b', streaming_message=None)
        manager.save(project_id, state)
        reply.handle('item/completed', {'item': {'type': 'agentMessage', 'id': 'x', 'text': '过期完成'}})
        assert not manager.load(project_id).get('streaming_message')
        assert manager.load(project_id)['events'] == []


@pytest.mark.asyncio
async def test_many_deltas_are_checkpointed_without_rewriting_history_each_time(monkeypatch):
    class MemoryStore:
        def __init__(self):
            self.state = {'request_id': 'r', 'events': []}
            self.writes = 0
        def load(self, _):
            return copy.deepcopy(self.state)
        def save(self, _, state, **options):
            self.state = copy.deepcopy(state)
            self.writes += 1

    store = MemoryStore()
    monkeypatch.setattr('agent_platform.agent_message_stream.time.monotonic', lambda: 10.)
    reply = AgentMessageStream(store, 'p', 'r')
    for _ in range(100):
        reply.handle('item/agentMessage/delta', {'itemId': 'one', 'delta': '哈'})
    assert store.writes == 1 and not store.state['events']
    reply.finish(close=True)
    assert store.writes == 2
    assert store.state['events'][0]['text'] == '哈' * 100
    assert store.state['events'][0]['incomplete']


def test_trailing_fragment_is_saved_without_another_delta_or_completion(streaming, monkeypatch):
    client, app, project, _, base, agent = streaming
    manager = app.state.services.local_agents
    page = client.get(base + '/conversation').json()
    revision = page['revision']
    checkpointed = threading.Event()
    save = manager.save

    def observe(project_id, state, **options):
        save(project_id, state, **options)
        if (state.get('streaming_message') or {}).get('text') == '已经读取结果，补充片段':
            checkpointed.set()

    monkeypatch.setattr(manager, 'save', observe)
    client.portal.call(agent.emit, 'item/agentMessage/delta', {'itemId': 'reply', 'delta': '补充片段'})
    assert checkpointed.wait(3)
    page = client.get(base + '/conversation', params={'after': page['last_cursor']}).json()
    assert page['streaming_message']['text'] == '已经读取结果，补充片段'
    assert page['revision'] == revision and page['status'] == 'running'
    assert not page['events']


def test_official_reply_is_visible_only_to_its_employee_conversation(official):
    client, app, service = official
    ready = threading.Event()

    class PartialAgent(FakeAgent):
        async def turn(self, message, on_event, on_tool, **kwargs):
            self.turn_id = str(uuid4())
            await on_event('item/agentMessage/delta', {'threadId': self.thread_id,
                'turnId': self.turn_id, 'itemId': 'reply', 'delta': '员工甲的部分回复'})
            ready.set()
            await asyncio.Event().wait()

    service.client_factory = PartialAgent
    _, alice = signup(client, '正文甲')
    _, bob = signup(client, '正文乙')
    pid = create_project(client, alice)
    client.post('/api/v1/projects/' + pid + '/access-members', headers=alice, json={'name': '正文乙'}).raise_for_status()
    enable(client, pid)
    first, second = chat(client, pid, alice), chat(client, pid, bob)
    client.post(first + '/messages', headers=alice, json={'message': '查看已保存结果', 'request_key': 'stream'}).raise_for_status()
    assert ready.wait(5)
    assert client.get(first, headers=alice).json()['streaming_message']['text'] == '员工甲的部分回复'
    assert client.get(first, headers=bob).status_code == 404
    assert not client.get(second, headers=bob).json().get('streaming_message')
    client.post(first + '/stop', headers=alice).raise_for_status()
    saved = client.get(first, headers=alice).json()
    assert saved['status'] == 'interrupted'
    assert saved['events'][-1]['text'] == '员工甲的部分回复' and saved['events'][-1]['incomplete']
    assert client.get('/api/v1/projects/' + pid + '/tasks', headers=alice).json() == []
