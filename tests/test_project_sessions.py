"""Independent project chats use real persistence, model loops and HTTP authorization."""
import asyncio
import json
import time
from types import SimpleNamespace

from agent_platform.connected_model import completion_events
from agent_platform.conversation_scope import conversation_scope
from tests.test_projects import configured  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401


def new(client, base, title='新会话', **kwargs):
    response = client.post(base + '/conversations', json={'title': title}, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()['id']


def settled(client, path):
    for _ in range(300):
        state = client.get(path).json()
        if state['status'] not in {'running', 'connecting'}:
            return state
        time.sleep(.01)
    raise AssertionError(state)


def configure(client, base):
    result = client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test',
        'base_url': 'https://example.test/v1', 'api_key': 'test'})
    assert result.status_code == 200, result.text


def test_personal_chats_preserve_legacy_and_share_only_project_resources(platform):
    client, app = platform
    _, a = signup(client, '甲')
    bob, b = signup(client, '乙')
    pid = project(client, a)
    base = '/api/v1/projects/' + pid
    assert client.post(base + '/access-members', headers=a, json={'name': '乙'}).status_code == 200
    manager = app.state.services.local_agents
    manager.event(pid, 'user', '旧项目的私有对话')
    ca, cb = new(client, base, headers=a), new(client, base, headers=b)
    assert {row['id'] for row in client.get(base + '/conversations', headers=a).json()} == {ca, 'legacy'}
    assert [row['id'] for row in client.get(base + '/conversations', headers=b).json()] == [cb]
    for cid, other in [(ca, b), (cb, a), ('legacy', b)]:
        path = base + '/conversations/' + cid
        assert client.get(path, headers=other).status_code == 404
        assert client.post(path + '/messages', headers=other, json={'message': 'read'}).status_code == 404
        assert client.post(path + '/stop', headers=other).status_code == 404
        assert client.patch(path, headers=other, json={'title': 'rename'}).status_code == 404
    assert client.get(base + '/conversation', headers=b).status_code == 404
    assert client.get(base + '/agent-session', headers=b).json()['events'] == []
    assert client.get(base + '/conversations/' + cb, headers=b).json()['events'] == []
    assert client.get('/api/v1/applications/' + pid + '/draft', headers=b).status_code == 200
    assert client.patch(base + '/conversations/' + cb, headers=b, json={'title': '训练研究'}).status_code == 200
    assert client.get(base + '/conversations', headers=b).json()[0]['title'] == '训练研究'
    assert client.delete(base + '/access-members/' + bob['user']['id'], headers=a).status_code == 200
    assert client.get(base + '/conversations/' + cb, headers=b).status_code == 404


def test_real_model_history_is_separate_and_survives_reconnection(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    configure(client, base)
    seen = []
    async def stream(**kwargs):
        seen.append(json.dumps([message.model_dump() for message in kwargs['messages']], ensure_ascii=False))
        for event in completion_events([{'type': 'text', 'text': '收到'}], stop_reason='end_turn'):
            yield event
    manager = app.state.services.local_agents
    monkeypatch.setattr(manager.connections, 'provider', lambda *args, **kwargs: SimpleNamespace(stream=stream))
    ca, cb = new(client, base, '设计分析'), new(client, base, '模型训练')
    paths = [base + '/conversations/' + cid for cid in (ca, cb)]
    for path, message in zip(paths, ['会话甲秘密内容', '会话乙秘密内容']):
        assert client.post(path + '/messages', json={'message': message}).status_code == 202
        assert settled(client, path)['status'] == 'idle'
    assert '会话甲秘密内容' in seen[0] and '会话乙秘密内容' not in seen[0]
    assert '会话乙秘密内容' in seen[1] and '会话甲秘密内容' not in seen[1]
    # Switching the shared connection drops cached clients and reloads each own history.
    configure(client, base)
    assert client.post(paths[0] + '/messages', json={'message': '继续甲的分析'}).status_code == 202
    assert settled(client, paths[0])['status'] == 'idle'
    assert '会话甲秘密内容' in seen[2] and '继续甲的分析' in seen[2]
    assert '会话乙秘密内容' not in seen[2]
    first, second = [client.get(path).json() for path in paths]
    assert first['session_id'] != second['session_id']
    assert first['thread_id'] != second['thread_id']
    assert [e['text'] for e in second['events'] if e['kind'] == 'user'] == ['会话乙秘密内容']
    assert client.get(paths[1] + '?after=' + first['events'][0]['id']).status_code == 422
    assert manager.load(pid)['events'] == []


def test_stop_only_own_concurrent_turn_and_restart_keeps_progress(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    configure(client, base)
    manager = app.state.services.local_agents
    entered = set()
    async def stream(**kwargs):
        entered.add(json.dumps([m.model_dump() for m in kwargs['messages']], ensure_ascii=False))
        await asyncio.sleep(60)
        yield  # cancellation should occur before this
    monkeypatch.setattr(manager.connections, 'provider', lambda *args, **kwargs: SimpleNamespace(stream=stream))
    ca, cb = new(client, base), new(client, base)
    paths = [base + '/conversations/' + cid for cid in (ca, cb)]
    for i, path in enumerate(paths):
        assert client.post(path + '/messages', json={'message': f'运行 {i}'}).status_code == 202
    for _ in range(200):
        if len(entered) == 2: break
        time.sleep(.01)
    assert len(entered) == 2
    assert client.post(paths[0] + '/stop').status_code == 200
    assert client.get(paths[0]).json()['status'] == 'interrupted'
    assert client.get(paths[1]).json()['status'] == 'running'
    assert client.put(base + '/agent-session', json={'provider': 'api', 'model': 'new'}).status_code == 422
    client.post(paths[1] + '/stop')
    with conversation_scope(pid, cb):
        state = manager.load(pid); state['status'] = 'running'; manager.save(pid, state)
    client.portal.call(manager.initialize)
    recovered = client.get(paths[1]).json()
    assert recovered['status'] == 'interrupted'
    assert any(e['text'] == '运行 1' for e in recovered['events'])
    assert client.get(paths[0]).json()['events'] != recovered['events']


def test_agent_task_resume_restores_its_conversation_and_denies_other_owner(platform, monkeypatch):
    client, app = platform
    _, a = signup(client, '甲'); _, b = signup(client, '乙')
    pid = project(client, a); base = '/api/v1/projects/' + pid
    client.post(base + '/access-members', headers=a, json={'name': '乙'})
    cid = new(client, base, headers=b)
    services = app.state.services
    from uuid import uuid4
    tid = str(uuid4())
    async def create_task():
        with conversation_scope(pid, cid):
            return await services.projects.store.create_task(tid, pid, 'once', 'agent', pid, {}, 'train', {})
    task, created = client.portal.call(create_task)
    assert created and task['conversation_id'] == cid
    async def mark():
        await services.projects.store.update_task(tid, status='interrupted')
    client.portal.call(mark)
    assert client.post(base + '/tasks/' + tid + '/resume', headers=a, json={'message': 'continue'}).status_code == 404
    assert client.post(base + '/tasks/' + tid + '/stop', headers=a).status_code == 404
    seen = []
    async def message(pid, text, **kwargs):
        from agent_platform.conversation_scope import conversation_for
        seen.append((conversation_for(pid), text))
    monkeypatch.setattr(services.local_agents, 'message', message)
    assert client.post(base + '/tasks/' + tid + '/resume', headers=b, json={'message': '继续训练'}).status_code == 202
    assert seen == [(cid, '继续训练')]
    assert services.local_agents.load(pid).get('project_task_id') is None
