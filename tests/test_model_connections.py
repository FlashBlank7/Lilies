import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from agent_platform.agent_core import collect_model_stream
from agent_platform.connected_model import ConnectedModel, completion_events
from agent_platform.model_connections import ModelConnection, ModelConnections, ProjectModelProvider, project_model
from agent_platform.model_session import ModelSession
from agent_platform.models import ChatMessage, ContentBlock, ToolDefinition
from agent_platform.providers.base import ProviderError
from tests.test_projects import configured, graph, node, edge, ref, start, settled
from tests.test_local_agents import settled as agent_settled


def connection(**values):
    return ModelConnection(provider='api', model='test-model', base_url='https://example.test/v1', api_key='private-test-key', **values)


async def complete(provider, tools=None):
    return await collect_model_stream(provider.stream(model='ignored-default', system='test system',
        messages=[ChatMessage(role='user', content=[ContentBlock(type='text', text='original input')])],
        tools=tools or [], max_output_tokens=1000, thinking_enabled=True, effort='medium'))


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,thinking', [('openai', 'high'), ('anthropic', 'off'), ('anthropic', 'high'), ('openai', 'default')])
async def test_api_protocol_key_thinking_and_real_tool_response(tmp_path, protocol, thinking):
    def respond(request):
        body = json.loads(request.content)
        assert body['model'] == 'test-model'
        if protocol == 'openai':
            assert request.url.path == '/v1/chat/completions'
            assert request.headers['authorization'] == 'Bearer private-test-key'
            if thinking == 'default':
                assert 'reasoning_effort' not in body
            else:
                assert body['reasoning_effort'] == 'high'
            return httpx.Response(200, json={'choices': [{'message': {'content': 'checking', 'tool_calls': [
                {'id': 't1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"path":"input.txt"}'}}]}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
        assert request.url.path == '/v1/messages'
        assert request.headers['x-api-key'] == 'private-test-key'
        assert body['thinking'] == {'type': 'disabled' if thinking == 'off' else 'adaptive'}
        if thinking == 'high':
            assert body['output_config'] == {'effort': 'high'}
        return httpx.Response(200, json={'content': [{'type': 'tool_use', 'id': 't1', 'name': 'read', 'input': {'path': 'input.txt'}}],
            'stop_reason': 'tool_use', 'usage': {'input_tokens': 10, 'output_tokens': 20}})
    model = ConnectedModel(connection(protocol=protocol, thinking=thinking), tmp_path, egress_enabled=True, transport=httpx.MockTransport(respond))
    result = await complete(model, [ToolDefinition(name='read', description='read', input_schema={'type': 'object'})])
    assert result.stop_reason == 'tool_use'
    assert result.blocks[-1].input == {'path': 'input.txt'}
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 20


def test_key_redaction_rotation_and_file_mode(configured):
    client, app, project, settings = configured
    base = '/api/v1/projects/' + project['id']
    data = {'provider': 'api', 'protocol': 'openai', 'base_url': 'https://example.test/v1',
            'model': 'test-model', 'api_key': 'private-test-key', 'thinking': 'high', 'runtime_enabled': True}
    response = client.put(base + '/agent-session', json=data)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['has_api_key'] and 'private-test-key' not in response.text
    assert 'api_key' not in state
    store = app.state.services.local_agents.connections
    assert store.path(project['id']).stat().st_mode & 0o777 == 0o600
    assert not list(settings.workspace_root.rglob('*model-connections*'))
    data.pop('api_key')
    data['thinking'] = 'low'
    updated = client.put(base + '/agent-session', json=data).json()
    assert updated['session_id'] == state['session_id'] and updated['thinking'] == 'low'
    assert store.load(project['id']).api_key.get_secret_value() == 'private-test-key'
    data['base_url'] = 'https://elsewhere.test/v1'
    assert client.put(base + '/agent-session', json=data).status_code == 422
    assert store.load(project['id']).base_url == 'https://example.test/v1'
    bad = client.put(base + '/agent-session', json={**data, 'base_url': 'invalid', 'api_key': 'private-test-key'})
    assert bad.status_code == 422 and 'private-test-key' not in bad.text
    oversized = client.put(base + '/agent-session', json={**data, 'api_key': 'oversized-private-key' * 500})
    assert oversized.status_code == 422 and 'oversized-private-key' not in oversized.text
    assert store.load(project['id']).api_key.get_secret_value() == 'private-test-key'
    switched = client.put(base + '/agent-session', json={**data, 'api_key': 'replacement-test-key'})
    assert switched.status_code == 200, switched.text
    assert switched.json()['session_id'] != state['session_id']
    assert switched.json()['thread_id'] is None
    assert switched.json()['context_handoff'] is True
    assert 'replacement-test-key' not in switched.text


def test_api_selection_labels_new_discussion_and_preserves_previous_source(configured):
    from agent_platform.requirement_discussion import save_discussion

    client, app, project, settings = configured
    previous = {'enabled': True, 'status': 'discussing', 'revision': 1, 'document': '',
                'turns': [{'user': 'previous request', 'analysis': {'detected_goal': 'previous understanding'}}],
                'source': 'codex'}
    save_discussion(settings.workspace_root / project['id'], previous)
    base = '/api/v1/projects/' + project['id']
    selected = client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test',
        'base_url': 'https://example.test/v1', 'api_key': 'test'})
    assert selected.status_code == 200
    discussion = client.get(base + '/agent-session').json()['requirements']
    assert discussion['source'] == 'lilies' and not discussion['turns']
    archive = next(app.state.services.local_agents.folder(project['id']).glob('previous-requirements-*.json'))
    assert json.loads(archive.read_text()) == previous


@pytest.mark.parametrize('model_kind', ['llm', 'model_turn'])
def test_project_runtime_enabled_explicitly_and_isolated(configured, monkeypatch, model_kind):
    client, app, project, _ = configured
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': body['messages'][-1]['content']}, 'finish_reason': 'stop'}]})
    store = app.state.services.local_agents.connections
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(self.load(pid), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    base = '/api/v1/projects/' + project['id']
    data = {'provider': 'api', 'base_url': 'https://example.test/v1', 'model': 'selected-model',
            'api_key': 'key', 'thinking': 'high', 'runtime_enabled': False}
    assert client.put(base + '/agent-session', json=data).status_code == 200
    model_node = (node('read', 'llm', prompt=ref('$inputs', 'text')) if model_kind == 'llm' else
                  node('read', 'model_turn', input=ref('$inputs', 'text'), settings={'tools': ['Read']}))
    graph(client, project['id'], [node('start', 'start', inputs=[{'name':'text','type':'string'}]), model_node,
        node('end', 'end', outputs={'text': ref('read', 'text')})], [edge('start', 'read'), edge('read', 'end')])
    failed = settled(client, base, start(client, base, 'disabled', inputs={'text': 'first'}))
    assert failed['status'] == 'failed' and not calls
    data['runtime_enabled'] = True
    assert client.put(base + '/agent-session', json=data).status_code == 200
    passed = settled(client, base, start(client, base, 'enabled', inputs={'text': 'second'}))
    assert passed['status'] == 'succeeded', passed
    assert passed['outputs']['text'] == 'second'
    assert calls[0]['model'] == 'selected-model' and calls[0]['reasoning_effort'] == 'high'
    if model_kind == 'model_turn':
        assert calls[0]['tools'][0]['function']['name'] == 'Read'
    assert project_model.get() is None
    other = client.post('/api/v1/projects', json={'name': 'unconfigured'}).json()['id']
    graph(client, other, [node('start', 'start'), node('m', 'llm', prompt='do not call'), node('end', 'end', outputs={'text': ref('m', 'text')})], [edge('start', 'm'), edge('m', 'end')])
    other_base = '/api/v1/projects/' + other
    assert settled(client, other_base, start(client, other_base, 'off'))['status'] == 'failed'
    assert len(calls) == 1


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('output_limit', [None, 32768])
def test_llm_output_limit_is_discoverable_and_reaches_model_http(configured, monkeypatch, protocol, output_limit):
    client, app, project, _ = configured
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['max_tokens'] == (16384 if output_limit is None else output_limit)
        if protocol == 'openai':
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"ok":true}'}], 'stop_reason': 'end_turn'})

    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(self.load(pid),
        self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    base = '/api/v1/projects/' + project['id']
    assert client.put(base + '/agent-session', json={'provider': 'api', 'protocol': protocol,
        'base_url': 'https://example.test/v1', 'model': 'test', 'api_key': 'test',
        'runtime_enabled': True}).status_code == 200
    manual = client.post(base + '/agent-tools', json={'name': 'block_catalog',
        'arguments': {'block_type': 'llm'}}).json()['manual']
    field = manual['config_schema']['properties']['max_output_tokens']
    assert field['type'] == 'integer' and field['minimum'] == 1 and field['default'] == 16384
    assert 'model API' in field['description']
    config = {'prompt': 'Return JSON.', 'structured_output': {'type': 'object'}}
    if output_limit is not None:
        config['max_output_tokens'] = output_limit
    graph(client, project['id'], [node('start', 'start'), node('llm', 'llm', **config),
        node('end', 'end', outputs={'ok': ref('llm', 'structured', 'ok')})],
        [edge('start', 'llm'), edge('llm', 'end')])
    result = settled(client, base, start(client, base, 'output-limit'))
    assert result['status'] == 'succeeded' and result['outputs']['ok'] is True
    assert len(requests) == 1


@pytest.mark.parametrize('invalid_limit', [0, -1, True, 1.5, '8192'])
def test_llm_output_limit_rejects_non_positive_integers_without_changing_draft(configured, invalid_limit):
    client, _, project, _ = configured
    path = '/api/v1/applications/' + project['id'] + '/draft'
    before = client.get(path).json()
    result = client.post(path, json={'op': 'add_node', 'expected_revision': before['revision'],
        'idempotency_key': 'invalid-limit', 'data': {'node': node('llm', 'llm', prompt='test', max_output_tokens=invalid_limit)}})
    assert result.status_code == 422 and 'max_output_tokens' in result.text
    assert client.get(path).json()['revision'] == before['revision']


@pytest.mark.parametrize('text,stop_reason', [
    ('{"items": ["' + 'item ' * 400 + 'unfinished', 'max_tokens'),
    ('explanation before {"value": invalid} after', 'end_turn'),
    ('plain text instead of JSON', 'end_turn'),
])
def test_api_structured_failure_exposes_bounded_diagnostics_to_project_tools(configured, monkeypatch, text, stop_reason):
    client, app, project, _ = configured
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': text}], 'stop_reason': stop_reason})

    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(self.load(pid),
        self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    base = '/api/v1/projects/' + project['id']
    assert client.put(base + '/agent-session', json={'provider': 'api', 'protocol': 'anthropic',
        'base_url': 'https://example.test/v1', 'model': 'test', 'api_key': 'private-key',
        'runtime_enabled': True}).status_code == 200
    graph(client, project['id'], [node('start', 'start'),
        node('parse', 'llm', prompt='Return a JSON object.', structured_output={'type': 'object'}),
        node('end', 'end', outputs={'value': ref('parse', 'structured')})],
        [edge('start', 'parse'), edge('parse', 'end')])
    failed = settled(client, base, start(client, base, 'invalid-json'))
    assert failed['status'] == 'failed' and len(calls) == 1
    for view in ['summary', 'full']:
        inspected = client.post(base + '/agent-tools', json={'name': 'workflow_run',
            'arguments': {'action': 'inspect', 'task_id': failed['id'], 'view': view}})
        assert inspected.status_code == 200
        error = inspected.json()['error']
        assert error == failed['error']
        assert f'stop_reason={stop_reason}' in error and f'output_chars={len(text)}' in error
        assert 'JSON parser:' in error and 'column ' in error and 'output_preview=' in error
        assert text[:20] in error and text[-10:] in error
        assert len(error) < 1400 and 'private-key' not in error
    events = client.get('/api/v1/runs/' + failed['runs'][0]['id'] + '/events/list').json()['events']
    node_error = next(e['data']['error'] for e in events if e['type'] == 'node.failed')
    assert f'node parse failed: {node_error}' == failed['error']


def test_api_builder_invokes_platform_tool_and_keeps_session(configured, monkeypatch):
    client, app, project, _ = configured
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert any(t['function']['name'] == 'project_progress' for t in body['tools'])
            message = {'tool_calls': [{'id': 'read-progress', 'type': 'function', 'function': {
                'name': 'project_progress', 'arguments': '{}'}}]}
            reason = 'tool_calls'
        else:
            assert any(m['role'] == 'tool' for m in body['messages'])
            message, reason = {'content': '已查看项目进度。'}, 'stop'
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': reason}]})
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(self.load(pid), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    base = '/api/v1/projects/' + project['id']
    data = {'provider': 'api', 'base_url': 'https://example.test/v1', 'model': 'selected-model', 'api_key': 'private-key'}
    assert client.put(base + '/agent-session', json=data).status_code == 200
    assert client.post(base + '/conversation/messages', json={'message': '查看进度'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert len(calls) == 2
    assert any(e['kind'] == 'tool' and e['success'] for e in state['events'])
    data['thinking'] = 'high'
    changed = client.put(base + '/agent-session', json=data).json()
    assert changed['thread_id'] == state['thread_id'] and changed['events'] == state['events']
    assert client.post(base + '/conversation/messages', json={'message': '继续说明'}).status_code == 202
    final = agent_settled(client, base)
    assert final['status'] == 'idle' and final['thread_id'] == state['thread_id'], final['error']
    assert calls[-1]['reasoning_effort'] == 'high'


@pytest.mark.parametrize('provider', ['codex', 'claude', 'kimi', 'classic'])
def test_project_rejects_external_agent_including_old_sessions(configured, monkeypatch, provider):
    client, app, project, _ = configured
    manager = app.state.services.local_agents
    def no_agent(*args, **kwargs):
        raise AssertionError('an external agent must never start for a project')
    monkeypatch.setattr(manager, 'client_factory', no_agent)
    base = '/api/v1/projects/' + project['id']
    for route in [base, '/api/v1/applications/' + project['id']]:
        response = client.put(route + '/agent-session', json={'provider': provider})
        assert response.status_code == 422, response.text
    # Historical settings remain readable, but cannot resume an external agent.
    state = manager.load(project['id'])
    state.update(provider=provider, session_id='historical', phase='coordinate')
    manager.save(project['id'], state)
    manager.connections.save(project['id'], ModelConnection(provider=provider, runtime_enabled=True))
    assert not manager.connections.enabled(project['id'])
    assert client.get(base + '/agent-session').json()['session_id'] == 'historical'
    for suffix in ['/conversation/messages', '/agent-session/resume']:
        result = client.post(base + suffix, json={'message': '继续'})
        assert result.status_code == 422 and '模型 API' in result.text
    assert not manager.tasks


@pytest.mark.parametrize('provider', ['codex', 'claude', 'kimi', 'classic'])
def test_project_member_legacy_entry_rejects_external_agent(configured, monkeypatch, provider):
    client, app, project, _ = configured
    manager = app.state.services.local_agents

    async def inspect(_):
        return {'path': '/test/local-agent', 'version': 'codex-cli 0.153.4'}

    async def validate(_):
        pass

    def no_agent(*args, **kwargs):
        raise AssertionError('a project member must never start an external agent')

    monkeypatch.setattr('agent_platform.local_agents.inspect_executable', inspect)
    monkeypatch.setattr('agent_platform.connected_model.validate_kimi_cli', validate)
    monkeypatch.setattr(manager, 'client_factory', no_agent)
    member = client.post('/api/v1/projects/' + project['id'] + '/members', json={'name': 'member'}).json()['id']
    base = '/api/v1/applications/' + member
    selected = client.put(base + '/agent-session', json={'provider': provider})
    assert selected.status_code == 422 and '模型 API' in selected.text

    state = manager.load(member)
    state.update(provider=provider, session_id='historical', phase='discuss')
    manager.save(member, state)
    assert client.get(base + '/agent-session').json()['session_id'] == 'historical'
    resumed = client.post(base + '/agent-session/messages', json={'message': 'continue'})
    assert resumed.status_code == 409 and '模型 API' in resumed.text
    assert not manager.tasks
    # Background builds must apply the same check before constructing a client.
    client.portal.call(manager._run, member, 'continue')
    assert manager.load(member)['status'] == 'error'
    assert '模型 API' in manager.load(member)['error']


def test_platform_loop_builds_runs_reads_failure_and_repairs_via_model_api(configured, monkeypatch):
    """Only inference is scripted: conversation, tools, drafts and runtime are real."""
    client, app, project, settings = configured
    base = '/api/v1/projects/' + project['id']
    requests = []
    executions = []
    revisions = []
    def tool(name, args):
        return {'tool_calls': [{'id': f'call-{len(requests)}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}]}
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        step = len(requests)
        result = json.loads(body['messages'][-1]['content']) if body['messages'][-1]['role'] == 'tool' else None
        if step == 1:
            message = tool('requirements_submit', {'understanding': '读取记录并返回是否存在。',
                'document': '# 测试需求\n读取 items 中的 absent 记录；不存在时返回 exists=false。'})
        elif step == 2:
            assert result is not None
            message = {'content': '请确认读取记录的需求。'}
        elif step == 3:
            message = tool('workflow_draft', {'view': 'full'})
        elif step == 4:
            assert result['snapshot']['workflow']['nodes'] == []
            revisions.append(result['revision'])
            message = tool('workflow_draft', {'operation': {'op': 'replace_workflow',
                'expected_revision': result['revision'], 'idempotency_key': 'initial', 'data': {'workflow': {
                    'nodes': [node('start', 'start'), node('read', 'project_record', action='get', collection='items', key='absent'),
                        node('end', 'end', outputs={'exists': ref('read', 'value', 'exists')})],
                    'edges': [edge('start', 'read'), edge('read', 'end')]}}}})
        elif step == 5:
            assert result['applied_revision'] == revisions[0] + 1
            message = tool('workflow_run', {'action': 'start', 'inputs': {}})
        elif step == 6:
            assert result['status'] == 'failed', result
            executions.append(result)
            message = tool('workflow_run', {'action': 'inspect', 'task_id': result['id'], 'view': 'full'})
        elif step == 7:
            assert result['status'] == 'failed' and result['error'], result
            message = tool('workflow_draft', {'view': 'full'})
        elif step == 8:
            assert result['revision'] == revisions[0] + 1
            message = tool('workflow_draft', {'operation': {'op': 'update_node',
                'expected_revision': result['revision'], 'idempotency_key': 'repair', 'data': {
                    'node_id': 'end', 'changes': {'config': {'outputs': {'exists': ref('read', 'found')}}}}}})
        elif step == 9:
            assert result['applied_revision'] == revisions[0] + 2
            message = tool('workflow_run', {'action': 'start', 'inputs': {}})
        elif step == 10:
            assert result['status'] == 'succeeded' and result['outputs'] == {'exists': False}, result
            executions.append(result)
            message = {'content': '已修复空记录处理，重新运行返回 exists=false。'}
        else:
            raise AssertionError(f'unexpected inference step {step}')
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop'}]})
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(
        self.load(pid), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    def no_external_agent(*args, **kwargs):
        raise AssertionError('platform must own the agent loop')
    monkeypatch.setattr(app.state.services.local_agents, 'client_factory', no_external_agent)
    assert client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test-model',
        'base_url': 'https://example.test/v1', 'api_key': 'test'}).status_code == 200
    assert client.post(base + '/agent-session/messages', json={'message': '读取不存在的记录，返回是否存在'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert client.post(base + '/requirements/confirm', json={'revision': state['requirements']['revision']}).status_code == 200
    assert client.post(base + '/agent-session/messages', json={'message': '搭建并测试', 'intent': 'build'}).status_code == 202
    final = agent_settled(client, base)
    assert final['status'] == 'idle', final['error']
    assert len(requests) == 10 and len(executions) == 2
    assert final['thread_id'] == state['thread_id']
    assert isinstance(app.state.services.local_agents.clients[project['id']], ModelSession)
    assert len(client.get(base + '/tasks').json()) == 2
    saved = json.loads((settings.data_dir / 'local-agents' / project['id'] / final['session_id'] / 'conversation.json').read_text())
    assert len([b for m in saved['messages'] for b in m['content'] if b['type'] == 'tool_result']) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'claude', 'kimi', 'classic'])
async def test_model_adapter_rejects_agent_sessions_before_starting_a_process(tmp_path, monkeypatch, provider):
    async def no_process(*args, **kwargs):
        raise AssertionError('a model call must not start an external agent process')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', no_process)
    model = ConnectedModel(ModelConnection(provider=provider, runtime_enabled=True), tmp_path / 'sessions')
    with pytest.raises(ProviderError, match='模型积木仅支持原始 LLM API') as error:
        await complete(model)
    assert not error.value.retryable
    assert not (tmp_path / 'sessions').exists()


@pytest.mark.asyncio
async def test_api_errors_do_not_leak_echoed_credentials(tmp_path):
    model = ConnectedModel(connection(), tmp_path, egress_enabled=True, transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text=request.headers['authorization'])))
    with pytest.raises(Exception, match='HTTP 401') as error:
        await complete(model)
    assert 'private-test-key' not in str(error.value)


@pytest.mark.asyncio
async def test_cancelled_tool_is_recorded_and_not_replayed_after_resume(tmp_path):
    # Keep provider behavior explicit: a resumed conversation sees the interrupted
    # tool result before the user's new message, and returns without another write.
    async def stream(**kwargs):
        previous = [b for m in kwargs['messages'] for b in m.content if b.type == 'tool_result']
        if previous:
            assert previous[-1].is_error
            blocks, stop = [{'type': 'text', 'text': '先核对上次写入'}], 'end_turn'
        else:
            blocks, stop = [{'type': 'tool_use', 'name': 'write', 'id': 'write-once', 'input': {}}], 'tool_use'
        for event in completion_events(blocks, stop_reason=stop):
            yield event
    from types import SimpleNamespace
    provider = SimpleNamespace(stream=stream)
    specs = [{'name': 'write', 'description': 'write once', 'inputSchema': {'type': 'object'}}]
    session = ModelSession(provider, tmp_path)
    thread = await session.start(specs, 'test')
    started = asyncio.Event()
    writes = []
    async def tool(name, args):
        writes.append(name)
        started.set()
        await asyncio.sleep(30)
    async def event(*args):
        pass
    task = asyncio.create_task(session.turn('write', event, tool))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resumed = ModelSession(provider, tmp_path)
    await resumed.start(specs, 'test', thread)
    assert (await resumed.turn('继续', event, tool))['status'] == 'completed'
    assert writes == ['write']


@pytest.mark.asyncio
async def test_concurrent_projects_keep_distinct_models_and_keys(tmp_path, monkeypatch):
    store = ModelConnections(tmp_path)
    ids = [str(uuid4()), str(uuid4())]
    for index, pid in enumerate(ids):
        store.save(pid, ModelConnection(provider='api', base_url='https://example.test/v1',
            model=f'model-{index}', api_key=f'key-{index}', runtime_enabled=True))
    seen = []
    async def respond(request):
        await asyncio.sleep(.01)
        model = json.loads(request.content)['model']
        seen.append((model, request.headers['authorization']))
        return httpx.Response(200, json={'choices': [{'message': {'content': model}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(store, 'provider', lambda pid: ConnectedModel(store.load(pid), tmp_path, egress_enabled=True, transport=httpx.MockTransport(respond)))
    routed = ProjectModelProvider(None, store)
    async def run(pid):
        token = project_model.set(pid)
        try:
            return (await complete(routed)).blocks[0].text
        finally:
            project_model.reset(token)
    assert await asyncio.gather(*(run(pid) for pid in ids)) == ['model-0', 'model-1']
    assert set(seen) == {('model-0', 'Bearer key-0'), ('model-1', 'Bearer key-1')}
    assert project_model.get() is None


@pytest.mark.asyncio
async def test_anthropic_thinking_signature_survives_tool_roundtrip(tmp_path):
    calls = []
    thinking = {'type': 'thinking', 'thinking': 'provider thinking', 'signature': 'signed-block'}
    redacted = {'type': 'redacted_thinking', 'data': 'opaque-provider-data'}
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200, json={'content': [thinking, redacted,
                {'type': 'tool_use', 'id': 'read', 'name': 'read', 'input': {}}], 'stop_reason': 'tool_use'})
        assistant = next(m for m in body['messages'] if m['role'] == 'assistant')
        assert assistant['content'][:2] == [thinking, redacted]
        assert body['messages'][-1]['content'][0]['content'] == '{"text": "input"}'
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'done'}], 'stop_reason': 'end_turn'})
    provider = ConnectedModel(connection(protocol='anthropic', thinking='high'), tmp_path, egress_enabled=True, transport=httpx.MockTransport(respond))
    session = ModelSession(provider, tmp_path / 'conversation')
    await session.start([{'name': 'read', 'description': 'read', 'inputSchema': {'type': 'object'}}], 'test')
    async def tool(name, arguments):
        return {'text': 'input'}
    async def event(*args):
        pass
    assert (await session.turn('read', event, tool))['status'] == 'completed'
    assert len(calls) == 2


def test_project_request_limits_reach_provider_and_new_message_can_continue(configured, monkeypatch):
    client, app, project, settings = configured
    settings.project_agent_max_model_calls = 1
    settings.project_agent_max_output_tokens = 192
    base = '/api/v1/projects/' + project['id']
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload['max_tokens'] == 192
        message = {'content': '继续完成。'} if len(requests) > 1 else {'content': '', 'tool_calls': [
            {'id': 'discover', 'type': 'function', 'function': {'name': 'project_workflows',
             'arguments': json.dumps({'action': 'list'})}}]}
        return httpx.Response(200, json={'choices': [{'message': message,
            'finish_reason': 'tool_calls' if 'tool_calls' in message else 'stop'}]})

    monkeypatch.setattr(ModelConnections, 'provider', lambda self, pid: ConnectedModel(
        self.load(pid), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test-model',
        'base_url': 'https://example.test/v1', 'api_key': 'test'}).raise_for_status()
    client.post(base + '/agent-session/messages', json={'message': '查看流程'}).raise_for_status()
    state = agent_settled(client, base)
    assert '1 次对话模型调用上限' in state['error'] and len(requests) == 1
    client.post(base + '/agent-session/messages', json={'message': '请继续完成'}).raise_for_status()
    state = agent_settled(client, base)
    assert state['status'] == 'idle' and not state['error'] and len(requests) == 2
    assert any(b.get('role') == 'tool' and b.get('tool_call_id') == 'discover'
               for b in requests[-1]['messages'])
