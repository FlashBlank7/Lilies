import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_requirement_package import package
from tests.test_v04_00_ai_requirement_intake import IntakeProvider


DOCUMENT = '# 输入回显需求\n用户提交文本，工作流原样返回该文本。输入为空时按必填约束报错。'


class ScriptedCodex:
    starts = []
    contexts = []
    closed = 0
    slow = False

    def __init__(self, *args, **kwargs):
        self.thread_id = None

    async def start(self, tools, instructions, thread_id=None):
        assert {tool['name'] for tool in tools} == {
            'project_file', 'block_catalog', 'workflow_draft', 'workflow_run', 'requirements_submit'}
        assert '历史答案' in instructions
        self.thread_id = thread_id or 'project-thread'
        self.starts.append(thread_id)
        return self.thread_id

    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        self.contexts.append(context)
        if self.slow:
            await asyncio.sleep(30)
        if context['phase'] == 'discuss':
            listing = await on_tool('project_file', {'action': 'list'})
            assert any(x['path'].endswith('需求.md') for x in listing['files'])
            await on_tool('project_file', {'action': 'read', 'path': 'requirement-package/需求.md'})
            await on_tool('requirements_submit', {
                'understanding': '请确认是否原样返回输入文本。',
                'questions': [] if '确认' in context['user_message'] else [
                    {'id': 'echo', 'question': '是否原样返回？'}],
                'document': DOCUMENT if '确认' in context['user_message'] else '',
            })
        else:
            await on_tool('block_catalog', {'block_type': 'end'})
            draft = await on_tool('workflow_draft', {})
            if not draft['snapshot']['workflow']['nodes']:
                ops = [
                    ('add_node', {'node': {'id': 'input', 'type': 'start', 'title': '输入',
                                         'config': {'inputs': [{'name': 'text', 'type': 'string', 'required': True}]}}}),
                    ('add_node', {'node': {'id': 'output', 'type': 'end', 'title': '输出',
                                         'config': {'outputs': {'text': {'$ref': {'node_id': '$inputs', 'path': ['text']}}}}}}),
                    ('add_edge', {'edge': {'id': 'flow', 'source': 'input', 'target': 'output'}}),
                ]
                for index, (op, data) in enumerate(ops):
                    draft = await on_tool('workflow_draft', {})
                    await on_tool('workflow_draft', {'operation': {'expected_revision': draft['revision'],
                        'idempotency_key': f'codex-{index}', 'op': op, 'data': data}})
            await on_tool('workflow_run', {'action': 'validate'})
            await on_tool('workflow_run', {'action': 'start', 'inputs': {'text': context['user_message']}})
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '已完成本轮。'}})
        return {'status': 'completed'}

    async def steer(self, message):
        self.contexts.append({'steer': message})

    async def close(self):
        type(self).closed += 1


@pytest.fixture
def configured(tmp_path, monkeypatch):
    ScriptedCodex.starts, ScriptedCodex.contexts, ScriptedCodex.closed = [], [], 0
    ScriptedCodex.slow = False
    async def inspect(_):
        return {'path': '/test/codex', 'version': 'codex-cli 0.153.4'}
    monkeypatch.setattr('agent_platform.local_agents.inspect_executable', inspect)
    provider = IntakeProvider({})
    settings = Settings(api_token='test', data_dir=tmp_path/'data', workspace_root=tmp_path/'ws',
                        model_egress_enabled=False, scheduler_poll_seconds=3600)
    app = create_app(settings, provider)
    app.state.services.local_agents.client_factory = ScriptedCodex
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer test'
        imported = client.post('/api/v1/requirement-packages/import', files={'file': ('input.zip', package())}).json()
        app_id = imported['application']['id']
        base = f'/api/v1/applications/{app_id}'
        yield client, app, base, app_id, settings, provider


def settled(client, base):
    for _ in range(200):
        state = client.get(base+'/agent-session').json()
        if state['status'] not in {'running', 'connecting'}:
            return state
        time.sleep(.02)
    raise AssertionError('Agent did not settle')


def select(client, base):
    result = client.put(base+'/agent-session', json={'provider': 'codex'})
    assert result.status_code == 200, result.text


def discuss_and_confirm(client, base):
    assert client.post(base+'/agent-session/messages', json={'message': '先读资料'}).status_code == 202
    first = settled(client, base)
    assert first['status'] == 'idle', first['error']
    assert first['requirements']['status'] == 'discussing'
    assert first['requirements']['source'] == 'codex'
    assert client.post(base+'/agent-session/messages', json={'message': '确认原样返回文本，请整理需求'}).status_code == 202
    ready = settled(client, base)
    assert ready['requirements']['status'] == 'review'
    assert client.post(base+'/requirements/confirm', json={'revision': ready['requirements']['revision']}).status_code == 200


@pytest.mark.parametrize('failure, recover', [
    ('executable', True), ('anonymous', True),
    ('session_file', False), ('anonymous_existing_executable', False),
])
def test_resume_after_cli_relocation_preserves_session(configured, monkeypatch, failure, recover):
    import errno

    client, app, base, app_id, settings, _ = configured
    select(client, base)
    discuss_and_confirm(client, base)
    manager = app.state.services.local_agents
    previous = manager.load(app_id)
    if failure == 'anonymous_existing_executable':
        executable = settings.data_dir / 'existing-codex'
        executable.write_text('existing executable')
        previous['executable'] = str(executable)
        manager.save(app_id, previous)
    client.portal.call(manager.clients.pop(app_id).close)
    discovered = []

    async def inspect(command):
        discovered.append(command)
        return {'path': '/test/updated-codex', 'version': 'codex-cli 0.154.0'}

    class RelocatedCodex(ScriptedCodex):
        def __init__(self, executable, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executable = executable

        async def start(self, tools, instructions, thread_id=None):
            if self.executable == previous['executable']:
                filename = (self.executable if failure == 'executable' else
                            '/test/missing-session-file' if failure == 'session_file' else None)
                raise FileNotFoundError(errno.ENOENT, 'No such file or directory', filename)
            return await super().start(tools, instructions, thread_id)

    monkeypatch.setattr('agent_platform.local_agents.inspect_executable', inspect)
    manager.client_factory = RelocatedCodex
    client.post(base+'/agent-session/messages', json={'message': '升级后继续运行', 'intent': 'build'})
    state = settled(client, base)
    assert state['session_id'] == previous['session_id']
    assert state['thread_id'] == previous['thread_id']
    assert state['requirements']['status'] == 'confirmed'
    assert {e['id'] for e in previous['events']} <= {e['id'] for e in state['events']}
    if recover:
        assert state['status'] == 'idle', state['error']
        assert state['executable'] == '/test/updated-codex'
        assert state['version'] == 'codex-cli 0.154.0'
        assert discovered == ['codex']
        assert ScriptedCodex.starts[-1] == previous['thread_id']
        # Single-application start is asynchronous; the agent turn can finish first.
        for _ in range(100):
            run = client.get(base+'/runs').json()[0]
            if run['status'] not in {'queued', 'running'}:
                break
            time.sleep(.02)
        assert run['status'] == 'succeeded', run
        assert run['state']['outputs']['output']['text'] == '升级后继续运行'
    else:
        assert state['status'] == 'error'
        assert 'No such file or directory' in state['error']
        assert not discovered


def test_discuss_confirm_build_run_and_continue_from_manual_edit(configured):
    client, app, base, app_id, settings, provider = configured
    select(client, base)
    assert ScriptedCodex.starts == []  # Selection alone cannot launch or spend tokens.
    assert client.post(base+'/builds', json={'requirement': DOCUMENT}).status_code == 409
    assert client.post(base+'/requirements/messages', json={'revision': 0}).status_code == 409
    assert client.post(base+'/agent-session/messages', json={'message': '开始', 'intent': 'build'}).status_code == 409
    discuss_and_confirm(client, base)
    assert client.get(base+'/draft').json()['snapshot']['workflow']['nodes'] == []
    assert client.get(base+'/runs').json() == []
    assert client.post(base+'/agent-tools', json={'name': 'workflow_draft', 'arguments': {
        'operation': {'expected_revision': 0, 'idempotency_key': 'early', 'op': 'remove_node', 'data': {'node_id': 'x'}}}}).status_code == 422
    assert client.post(base+'/agent-session/messages', json={'message': '第一次运行', 'intent': 'build'}).status_code == 202
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    for _ in range(100):
        runs = client.get(base+'/runs').json()
        if runs and runs[0]['status'] not in {'queued', 'running'}:
            break
        time.sleep(.02)
    assert runs[0]['status'] == 'succeeded', runs[0]
    assert runs[0]['state']['outputs']['output']['text'] == '第一次运行'
    draft = client.get(base+'/draft').json()
    edited = client.post(base+'/draft', json={'expected_revision': draft['revision'], 'idempotency_key': 'human-title',
        'op': 'update_node', 'data': {'node_id': 'output', 'changes': {'title': '用户改过的输出'}}})
    assert edited.status_code == 200, edited.text
    assert client.post(base+'/agent-session/messages', json={'message': '保留人工修改继续运行', 'intent': 'build'}).status_code == 202
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert client.get(base+'/draft').json()['snapshot']['workflow']['nodes'][1]['title'] == '用户改过的输出'
    assert len(client.get(base+'/builds').json()) == 1
    assert len(ScriptedCodex.starts) == 1
    assert provider.calls == []
    assert app.state.services.builders.has('codex')


def test_project_files_and_tools_cannot_reach_answers_other_apps_or_change_inputs(configured, tmp_path):
    client, app, base, app_id, settings, _ = configured
    outside = tmp_path/'answers.txt'
    outside.write_text('secret-old-answer')
    package_root = settings.workspace_root/app_id/'requirement-package'
    (package_root/'answer-link').symlink_to(outside)
    for path in ('../answers.txt', str(outside), 'requirements/conversation.json', 'other-project/README.md',
                 'requirement-package/answer-link'):
        response = client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {'action': 'read', 'path': path}})
        assert response.status_code == 422, (path, response.text)
        assert 'secret-old-answer' not in response.text
    for path in ('requirement-package/README.md', 'requirements/conversation.json'):
        assert client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {
            'action': 'write', 'path': path, 'content': 'change'}}).status_code == 422
    listing = client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {'action': 'list'}}).json()
    assert not any(x['path'].endswith('answer-link') for x in listing['files'])
    assert client.post(base+'/agent-tools', json={'name': 'Bash', 'arguments': {'command': 'cat /etc/passwd'}}).status_code == 422
    assert client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {
        'action': 'write', 'path': 'solution/new.py', 'content': 'print(1)'}}).status_code == 200
    profile = client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {
        'action': 'profile', 'path': 'requirement-package/数据/input.csv'}})
    assert profile.status_code == 200 and profile.json()['scan_complete']
    assert client.get(base+'/agent-tools', headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_stop_steer_and_reconnect_same_project_thread(configured):
    client, app, base, app_id, settings, provider = configured
    select(client, base)
    ScriptedCodex.slow = True
    client.post(base+'/agent-session/messages', json={'message': '先读资料'})
    for _ in range(100):
        if client.get(base+'/agent-session').json()['status'] == 'running':
            break
        time.sleep(.02)
    assert client.post(base+'/agent-session/messages', json={'message': '只看本项目'}).status_code == 202
    assert {'steer': '只看本项目'} in ScriptedCodex.contexts
    assert client.put(base+'/agent-session', json={'provider': 'classic'}).status_code == 422
    async def active_run():
        task = asyncio.create_task(asyncio.sleep(30))
        app.state.services.workflow_runtime.active_tasks['owned-run'] = task
        app.state.services.local_agents.track_run(app_id, 'owned-run')
        return task
    owned_run = client.portal.call(active_run)
    stopped = client.post(base+'/agent-session/stop').json()
    assert stopped['status'] == 'interrupted'
    assert ScriptedCodex.closed == 1
    assert owned_run.cancelled()
    ScriptedCodex.slow = False
    client.post(base+'/agent-session/messages', json={'message': '继续读取资料'})
    state = settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert ScriptedCodex.starts == [None, 'project-thread']
    assert provider.calls == []


def test_provider_failure_keeps_messages_and_never_falls_back(configured):
    client, app, base, app_id, _, provider = configured
    class FailedCodex(ScriptedCodex):
        async def start(self, *args):
            raise RuntimeError('Codex 登录不可用')
    app.state.services.local_agents.client_factory = FailedCodex
    select(client, base)
    client.post(base+'/agent-session/messages', json={'message': '请分析企业资料'})
    state = settled(client, base)
    assert state['status'] == 'error' and '登录不可用' in state['error']
    assert any(e['text'] == '请分析企业资料' for e in state['events'])
    assert provider.calls == []


def test_previous_unconfirmed_model_analysis_is_not_input_for_codex(configured):
    client, app, base, app_id, settings, _ = configured
    from agent_platform.requirement_discussion import save_discussion
    save_discussion(settings.workspace_root/app_id, {
        'enabled': True, 'revision': 5, 'status': 'discussing', 'document': '',
        'turns': [{'user': '旧会话', 'analysis': {'detected_goal': 'OLD ANSWER'}}]})
    select(client, base)
    state = client.get(base+'/agent-session').json()
    assert state['requirements']['turns'] == []
    assert list(app.state.services.local_agents.folder(app_id).glob('previous-requirements-*.json'))
    client.post(base+'/agent-session/messages', json={'message': '重新读资料'})
    settled(client, base)
    assert 'OLD ANSWER' not in json.dumps(ScriptedCodex.contexts)


@pytest.mark.asyncio
async def test_restart_marks_active_session_interrupted_without_relaunch(tmp_path):
    settings = Settings(api_token='test', data_dir=tmp_path/'data', workspace_root=tmp_path/'ws')
    app = create_app(settings)
    manager = app.state.services.local_agents
    from uuid import uuid4
    app_id = str(uuid4())
    state = manager.load(app_id)
    state.update(provider='codex', status='running', thread_id='saved-project-thread')
    manager.save(app_id, state)
    await manager.initialize()
    assert manager.load(app_id)['status'] == 'interrupted'
    assert manager.load(app_id)['thread_id'] == 'saved-project-thread'
    assert manager.clients == {} and manager.tasks == {}
