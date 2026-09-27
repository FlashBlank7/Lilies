"""Project graph checks do not require saved tests or runtime resource bindings."""
from functools import partial

import pytest

from agent_platform.workflow_models import WorkflowSpec
from tests.test_projects import configured, edge, graph, node, ref  # noqa: F401


def run_tool(client, pid, action, **arguments):
    response = client.post(f'/api/v1/projects/{pid}/agent-tools', json={
        'name': 'workflow_run', 'arguments': {'action': action, **arguments}})
    assert response.status_code == 200, response.text
    return response.json()


def save_test(client, pid, **changes):
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path).json()
    test = {'id': 'quantity', 'name': '数量', 'requirement': '返回数量',
            'assertions': [{'path': ['quantity'], 'operator': 'equals', 'expected': 2}],
            **changes}
    response = client.post(path, json={'op': 'add_test', 'data': {'test': test},
        'expected_revision': draft['revision'], 'idempotency_key': 'saved-test'})
    assert response.status_code == 200, response.text


def test_project_validates_and_runs_without_creating_acceptance_tests(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [
        node('start', 'start', inputs=[
            {'name': 'quantity', 'type': 'number', 'required': False, 'default': 1}]),
        node('end', 'end', outputs={'quantity': ref('start', 'quantity')}),
    ], [edge('start', 'end')])
    path = f'/api/v1/applications/{pid}/draft'
    before = client.get(path).json()
    checked = run_tool(client, pid, 'validate')
    assert checked['valid'] and checked['errors'] == [] and checked['test_count'] == 0
    assert checked['validation_scope'] == 'structure'
    assert run_tool(client, pid, 'validate', view='full') == checked
    assert checked['revision'] == before['revision']
    assert checked['content_hash'] == before['content_hash']
    assert client.get(f'/api/v1/projects/{pid}/tasks').json() == []
    for inputs, quantity in [({}, 1), ({'quantity': 4}, 4)]:
        task = run_tool(client, pid, 'start', inputs=inputs)
        assert task['status'] == 'succeeded' and task['outputs'] == {'quantity': quantity}
    assert client.get(path).json() == before


@pytest.mark.parametrize('work_node', [
    node('work', 'tool', tool_name='unbound-tool'),
    node('work', 'llm', model='not-configured', prompt='Return a number'),
    node('work', 'model_predict', model_ref='', dataset_id=''),
])
def test_project_structure_allows_unbound_runtime_resources(configured, work_node):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('start', 'start'), work_node, node('end', 'end')],
        [edge('start', 'work'), edge('work', 'end')])
    checked = run_tool(client, pid, 'validate')
    assert checked['valid'] and checked['errors'] == []
    assert client.get(f'/api/v1/projects/{pid}/tasks').json() == []


def test_missing_model_resource_fails_when_workflow_runs(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('start', 'start'),
        node('predict', 'model_predict', model_ref='', dataset_id=''), node('end', 'end')],
        [edge('start', 'predict'), edge('predict', 'end')])
    assert run_tool(client, pid, 'validate')['valid']
    task = run_tool(client, pid, 'start')
    assert task['status'] == 'failed'
    assert '模型预测节点尚未选择模型' in task['error']


@pytest.mark.parametrize(('nodes', 'edges', 'error_fragment'), [
    ([node('start', 'start'), node('end', 'end', outputs={'value': ref('missing')})],
        [edge('start', 'end')], '不存在的节点'),
    ([node('start', 'start'), node('end', 'end', outputs={'value': ref('end')})],
        [edge('start', 'end')], '自己'),
    ([node('start', 'start'), node('llm', 'llm', prompt='text', max_output_tokens=0),
        node('end', 'end')], [edge('start', 'llm'), edge('llm', 'end')], 'max_output_tokens'),
    ([node('start', 'start'), node('end', 'end')], [], 'unreachable'),
])
def test_project_validation_rejects_invalid_persisted_graphs(configured, nodes, edges, error_fragment):
    client, app, project, _ = configured
    pid = project['id']
    store = app.state.services.workflow_store
    draft = client.portal.call(store.get_draft, pid)
    snapshot = draft['snapshot'].model_copy(deep=True)
    snapshot.workflow = WorkflowSpec.model_validate({'nodes': nodes, 'edges': edges})
    # Historical snapshots can predate today's edit-time validation.
    client.portal.call(partial(store.save_draft, pid, snapshot,
        expected_revision=draft['revision'], idempotency_key='historical-graph'))
    checked = run_tool(client, pid, 'validate')
    assert not checked['valid']
    assert any(error_fragment in error for error in checked['errors']), checked


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('agent_type', ['claude_agent', 'subagent_spawn', 'soft_block'])
def test_project_validation_enforces_revoked_agent_capability(configured, nested, agent_type):
    client, _, project, _ = configured
    pid = project['id']
    capability_path = f'/api/v1/projects/{pid}/capabilities'
    assert client.put(capability_path, json={'agent_modules_enabled': True}).status_code == 200
    agent = (node('agent', 'claude_agent', agent_id='unbound-agent', task='task')
        if agent_type == 'claude_agent' else
        node('agent', 'subagent_spawn', settings={'task': 'task'})
        if agent_type == 'subagent_spawn' else
        node('agent', 'soft_block', strategy='agent_spawn_subagent', settings={'task': 'task'}))
    if nested:
        agent = node('agent', 'iteration', items=[1], output_node_id='end', workflow={
            'nodes': [node('start', 'start'), agent, node('end', 'end')],
            'edges': [edge('start', 'agent'), edge('agent', 'end')]})
    graph(client, pid, [node('start', 'start'), agent, node('end', 'end')],
        [edge('start', 'agent'), edge('agent', 'end')])
    assert run_tool(client, pid, 'validate')['valid']
    assert client.put(capability_path, json={'agent_modules_enabled': False}).status_code == 200
    checked = run_tool(client, pid, 'validate')
    assert not checked['valid']
    assert any('禁止使用智能体积木' in error for error in checked['errors'])


def test_default_legacy_validation_keeps_binding_and_acceptance_requirements(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('start', 'start'),
        node('call', 'tool', tool_name='unbound-tool'), node('end', 'end')],
        [edge('start', 'call'), edge('call', 'end')])
    assert run_tool(client, pid, 'validate')['valid']
    legacy = client.post(f'/api/v1/applications/{pid}/draft/validate').json()
    assert not legacy['valid']
    assert 'validation_scope' not in legacy
    assert 'at least one mandatory acceptance test is required' in legacy['errors']
    assert any('tool binding not found: unbound-tool' in error for error in legacy['errors'])


def test_structure_check_ignores_saved_test_constraints_and_preserves_tests(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'quantity': 1})],
        [edge('start', 'end')])
    save_test(client, pid, required_node_types=['code'])
    path = f'/api/v1/applications/{pid}/draft'
    before = client.get(path).json()
    checked = run_tool(client, pid, 'validate')
    assert checked['valid'] and checked['test_count'] == 1
    legacy = client.post(path + '/validate').json()
    assert not legacy['valid']
    assert any('missing required node types' in error for error in legacy['errors'])
    assert client.get(path).json() == before


def test_explicit_tests_still_execute_and_report_real_assertion_failure(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'quantity': 1})],
        [edge('start', 'end')])
    save_test(client, pid)
    assert run_tool(client, pid, 'validate')['valid']
    report = run_tool(client, pid, 'tests', view='full')
    assert not report['passed'] and report['summary']['failed'] == 1
    result = report['tests'][0]
    assert result['run_status'] == 'succeeded' and result['outputs'] == {'quantity': 1}
    assert result['assertions'][0]['actual'] == 1
    assert result['assertions'][0]['expected'] == 2
    assert not result['assertions'][0]['passed']
