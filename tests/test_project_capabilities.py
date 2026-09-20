import json
from functools import partial

import httpx
import pytest

from agent_platform.workflow_runtime import _NODE_EXECUTORS
from tests.test_projects import configured, edge, graph, node, ref, settled, start  # noqa: F401
from tests.test_project_tool_help import prepare


def capability(client, pid, enabled):
    response = client.put(f'/api/v1/projects/{pid}/capabilities', json={'agent_modules_enabled': enabled})
    assert response.status_code == 200, response.text
    assert response.json()['agent_modules_enabled'] is enabled


def test_project_catalog_manual_and_builder_tools_share_capabilities(configured):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    member = client.post(base + '/members', json={'name': 'member'}).json()['id']
    assert not project['agent_modules_enabled']
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    for enabled in [False, True, False]:
        capability(client, pid, enabled)
        for target in [pid, member]:
            params = {'application_id': target}
            for path in ['/api/v1/blocks', '/api/v1/block-manuals']:
                response = client.get(path, params=params)
                assert response.status_code == 200
                types = {b['type'] for b in response.json()}
                assert {'llm', 'model_turn', 'tool', 'loop'} <= types
                assert ('claude_agent' in types) is enabled
                assert ('subagent_spawn' in types) is enabled
                if not enabled:
                    assert 'claude_agent' not in response.text
                    assert 'subagent_spawn' not in response.text
            for suffix in ['', '/manual']:
                response = client.get('/api/v1/blocks/claude_agent' + suffix, params=params)
                assert response.status_code == (200 if enabled else 404)
            blueprint = client.get('/api/v1/claude-architecture-blueprint', params=params)
            assert ('claude_agent' in blueprint.text) is enabled
            assert ('subagent_spawn' in blueprint.text) is enabled
        tools = client.post(base + '/agent-tools', json={'name': 'block_catalog', 'arguments': {}})
        assert ('claude_agent' in tools.text) is enabled
        help = client.post(base + '/agent-tools', json={'name': 'block_catalog', 'arguments': {'block_type': 'claude_agent'}})
        assert help.status_code == (200 if enabled else 404)
        assert 'claude_agent' not in client.get('/api/v1/blocks', params={'application_id': other}).text
    denied = client.post(base + '/agent-tools', json={'name': 'project_capabilities', 'arguments': {'agent_modules_enabled': True}})
    assert denied.status_code == 422
    assert not client.get(base).json()['agent_modules_enabled']


def test_classic_builder_manuals_and_template_library_follow_current_project(configured):
    from agent_platform.workflow_models import BuildTeamState
    client, app, project, _ = configured
    services = app.state.services; pid = project['id']
    workflow = services.blocks.expand_template('claude_like_coding_agent')
    services.templates.register('optional-agent-template', workflow, {'title': 'Optional agent'})
    state = BuildTeamState()
    def run(tool, data):
        return client.portal.call(partial(services.builder._execute, 'catalog-test', pid, state, tool, data,
                                         max_repair_cycles=4, auto_publish=False))
    for enabled in [False, True, False]:
        capability(client, pid, enabled)
        blocks = client.portal.call(services.builder._available_blocks, pid)
        overview = services.builder._catalog_overview(blocks)
        assert ('subagent_spawn' in overview) is enabled
        assert ('claude_agent' in overview) is enabled
        manuals = run('manual_search', {})
        assert ('subagent_spawn' in json.dumps(manuals)) is enabled
        for tool in ['catalog_get', 'manual_get']:
            if enabled:
                assert run(tool, {'type': 'subagent_spawn'})['type'] == 'subagent_spawn'
            else:
                with pytest.raises(KeyError):
                    run(tool, {'type': 'subagent_spawn'})
        templates = run('template_list', {})
        assert ('optional-agent-template' in {item['name'] for item in templates}) is enabled
        library = client.get('/api/v1/capability-modules', params={'application_id': pid, 'all_versions': True})
        assert ('optional-agent-template' in {item['module_id'] for item in library.json()}) is enabled


@pytest.mark.parametrize('container', [None, 'iteration', 'loop'])
@pytest.mark.parametrize('agent_type', ['claude_agent', 'subagent_spawn', 'soft_block'])
def test_direct_and_builder_edits_cannot_insert_forbidden_agent(configured, container, agent_type):
    client, _, pid, base = prepare(configured)
    n = (node('agent', 'claude_agent', agent_id='test-agent', task='test') if agent_type == 'claude_agent' else
         node('agent', 'subagent_spawn', settings={'task': 'test'}) if agent_type == 'subagent_spawn' else
         node('agent', 'soft_block', strategy='agent_spawn_subagent', settings={'task': 'test'}))
    if container:
        inner = {'nodes': [node('start', 'start'), n, node('end', 'end')], 'edges': [edge('start', 'agent'), edge('agent', 'end')]}
        n = node('container', container, workflow=inner, output_node_id='end',
                 **({'items': [1]} if container == 'iteration' else {'break_value': True, 'break_condition': {'value': True, 'expected': True}}))
    path = f'/api/v1/applications/{pid}/draft'
    before = client.get(path).json()
    operation = {'op': 'add_node', 'expected_revision': before['revision'], 'idempotency_key': 'forbidden', 'data': {'node': n}}
    direct = client.post(path, json=operation)
    assert direct.status_code == 422 and '禁止使用智能体积木' in direct.text
    builder = client.post(base + '/agent-tools', json={'name': 'workflow_draft', 'arguments': {'operation': operation}})
    assert builder.status_code == 422 and '禁止使用智能体积木' in builder.text
    batch = client.post(base + '/agent-tools', json={'name': 'workflow_draft', 'arguments': {'batch': {
        'expected_revision': before['revision'], 'expected_content_hash': before['content_hash'],
        'idempotency_key': 'forbidden-batch', 'operations': [{'op': operation['op'], 'data': operation['data']}]}}})
    assert batch.status_code == 422 and '禁止使用智能体积木' in batch.text
    assert client.get(path).json()['revision'] == before['revision']


def prepare_agent(configured, monkeypatch, *, human=False, member=False, agent_type='claude_agent'):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    capability(client, pid, True)
    target = client.post(base + '/members', json={'name': 'agent member'}).json()['id'] if member else pid
    # No provider calls. A fake executor records whether dispatch crossed the hard boundary.
    calls = []
    async def execute(runtime, run):
        calls.append(run.scoped_id)
        return {'text': 'agent result', 'output': 'agent result'}
    monkeypatch.setitem(_NODE_EXECUTORS, agent_type, execute)
    assert client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test',
        'base_url': 'https://model.test/v1', 'api_key': 'fake-key', 'runtime_enabled': True}).status_code == 200
    nodes = [node('start', 'start')]
    if human:
        nodes.append(node('human', 'human_input', title='继续', fields=[{'name': 'answer', 'type': 'string', 'label': '回答'}]))
    agent = (node('agent', 'claude_agent', agent_id='test-agent', task='test') if agent_type == 'claude_agent' else
             node('agent', 'subagent_spawn', settings={'task': 'test'}) if agent_type == 'subagent_spawn' else
             node('agent', 'soft_block', strategy='agent_spawn_subagent', settings={'task': 'test'}))
    nodes += [agent, node('end', 'end', outputs={'text': ref('agent', 'text' if agent_type == 'claude_agent' else 'output')})]
    graph(client, target, nodes, [edge(a['id'], b['id']) for a, b in zip(nodes, nodes[1:])])
    if member:
        graph(client, pid, [node('start', 'start'), node('call', 'tool', tool_name='workflow:' + target),
            node('end', 'end', outputs={'text': ref('call', 'output', 'text')})], [edge('start', 'call'), edge('call', 'end')])
    return client, pid, base, calls, target


@pytest.mark.parametrize('member', [False, True])
@pytest.mark.parametrize('agent_type', ['claude_agent', 'subagent_spawn', 'soft_block'])
def test_allowed_agent_runs_then_old_draft_is_denied_after_switch(configured, monkeypatch, member, agent_type):
    client, pid, base, calls, target = prepare_agent(configured, monkeypatch, member=member, agent_type=agent_type)
    passed = settled(client, base, start(client, base, 'allowed'))
    assert passed['status'] == 'succeeded', passed
    assert passed['outputs']['text'] == 'agent result' and len(calls) == 1
    capability(client, pid, False)
    denied = settled(client, base, start(client, base, 'forbidden'))
    assert denied['status'] == 'failed' and '禁止使用智能体积木' in denied['error']
    assert len(calls) == 1
    direct = client.post(f'/api/v1/applications/{target}/runs', json={'use_draft': True})
    assert direct.status_code == 422 and '禁止使用智能体积木' in direct.text
    assert len(calls) == 1


@pytest.mark.parametrize('agent_type', ['claude_agent', 'subagent_spawn', 'soft_block'])
def test_revoked_agent_cannot_execute_when_paused_task_resumes(configured, monkeypatch, agent_type):
    client, pid, base, calls, _ = prepare_agent(configured, monkeypatch, human=True, agent_type=agent_type)
    pending = settled(client, base, start(client, base, 'pause'))
    assert pending['status'] == 'waiting_input', pending
    assert calls == []
    capability(client, pid, False)
    run = pending['runs'][0]
    assert client.post(f'{base}/tasks/{pending["id"]}/runs/{run["id"]}/input', json={'values': {'answer': 'continue'}}).status_code == 200
    assert client.post(f'{base}/tasks/{pending["id"]}/resume', json={}).status_code == 202
    denied = settled(client, base, pending)
    assert denied['status'] == 'failed' and '禁止使用智能体积木' in denied['error']
    assert calls == []


@pytest.mark.parametrize('agent_type', ['claude_agent', 'subagent_spawn', 'soft_block'])
def test_enabled_macro_uses_real_platform_agent_loop_with_mock_model(configured, monkeypatch, agent_type):
    from agent_platform.connected_model import ConnectedModel
    from agent_platform.model_connections import ModelConnections
    from tests.test_runtime import FakeSandboxes
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    capability(client, pid, True)
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'platform agent result'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, owner: ConnectedModel(
        self.load(owner), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    app.state.services.runtime.sandboxes = FakeSandboxes(settings.workspace_root / pid)
    assert client.put(base + '/agent-session', json={'provider': 'api', 'model': 'trusted-model',
        'base_url': 'https://model.test/v1', 'api_key': 'fake-key', 'runtime_enabled': True}).status_code == 200
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path).json()
    agent = {'id': 'test-agent', 'name': 'test', 'description': 'test agent',
        'system_prompt': 'Return a short answer to the given task.', 'tools': [], 'network_policy': 'none', 'allow_subagents': False}
    saved = client.post(path, json={'op': 'upsert_agent', 'data': {'agent': agent},
        'expected_revision': draft['revision'], 'idempotency_key': 'bind-agent'})
    assert saved.status_code == 200, saved.text
    n = (node('agent', 'claude_agent', agent_id='test-agent', task='test') if agent_type == 'claude_agent' else
         node('agent', 'subagent_spawn', settings={'task': 'test'}) if agent_type == 'subagent_spawn' else
         node('agent', 'soft_block', strategy='agent_spawn_subagent', settings={'task': 'test'}))
    graph(client, pid, [node('start', 'start'), n,
        node('end', 'end', outputs={'text': ref('agent', 'text' if agent_type == 'claude_agent' else 'output')})], [edge('start', 'agent'), edge('agent', 'end')])
    passed = settled(client, base, start(client, base, 'actual-agent-loop'))
    assert passed['status'] == 'succeeded' and passed['outputs'] == {'text': 'platform agent result'}, passed
    assert len(requests) == 1 and requests[0]['model'] == 'trusted-model'
    capability(client, pid, False)
    denied = settled(client, base, start(client, base, 'same-draft-denied'))
    assert denied['status'] == 'failed' and len(requests) == 1
