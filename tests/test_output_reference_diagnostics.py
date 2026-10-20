"""Output-wrapper mistakes are repairable draft feedback, not save gates."""
from copy import deepcopy

import pytest

from tests.test_projects import configured, node, edge, ref  # noqa: F401
from tests.test_project_tool_help import prepare
from tests.test_project_workflow_copy import call
from tests.test_generation_feedback import await_generation
from tests.test_official_agent import official, FakeAgent, signup, project, enable  # noqa: F401
from tests.test_users import platform  # noqa: F401


def graph(kind='code'):
    config = ({'code': "def main(inputs):\n    return {'rows': [1]}"} if kind == 'code' else
              {'assignments': {'rows': [1]}} if kind == 'variable_assigner' else
              {'variables': [{'rows': [1]}], 'mode': 'first_non_null'})
    return {'nodes': [node('s', 'start'), node('produce', kind, **config),
                      node('e', 'end', outputs={'rows': ref('produce', 'rows')})],
            'edges': [edge('s', 'produce'), edge('produce', 'e')]}


@pytest.mark.parametrize('kind', ['code', 'variable_assigner', 'variable_aggregator'])
def test_save_bad_wrapper_then_repair_without_execution(configured, kind):
    client, _, pid, base = prepare(configured)
    current = call(client, base, 'workflow_draft')
    flow = graph(kind)
    response = client.put(base + f'/workflows/{pid}/draft', json={
        'expected_revision': current['revision'], 'workflow': flow})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert not saved['structure_check']['valid']
    assert any("['output', 'rows']" in error and 'produce' in error
               for error in saved['structure_check']['errors'])
    flow['nodes'][-1]['config']['outputs'] = {
        'rows': ref('produce', 'output', 'rows'), 'whole': ref('produce'),
        'business': ref('produce', 'output'),
        'legacy_dotted_path': ref('produce', 'output.rows'),
        'literal_data': {'$ref': {'node_id': 'produce', 'path': ['rows']}, 'note': 'not a reference'},
        # Dynamic result fields are not guessed or required to already exist.
        'future_field': ref('produce', 'output', 'not_known_until_run'),
    }
    if kind == 'code':
        flow['nodes'][-1]['config']['outputs']['logs'] = ref('produce', 'logs')
    fixed = client.put(base + f'/workflows/{pid}/draft', json={
        'expected_revision': saved['revision'], 'workflow': flow})
    assert fixed.status_code == 200 and fixed.json()['structure_check']['valid'], fixed.text
    assert client.get(base + '/tasks').json() == []


def test_nested_reference_uses_its_own_node_contract(configured):
    client, _, pid, base = prepare(configured)
    inner = graph()
    # The outer node with the same ID has direct, declared start fields.
    flow = {'nodes': [node('produce', 'start', inputs=[{'name': 'rows', 'type': 'array'}]),
        node('each', 'iteration', items=ref('produce', 'rows'), workflow=inner,
             output_node_id='e'), node('e', 'end')],
        'edges': [edge('produce', 'each'), edge('each', 'e')]}
    current = call(client, base, 'workflow_draft')
    result = client.put(base + f'/workflows/{pid}/draft', json={
        'expected_revision': current['revision'], 'workflow': flow}).json()
    errors = result['structure_check']['errors']
    assert len(errors) == 1 and 'each/e.config.outputs.rows' in errors[0], errors


def test_generator_receives_output_path_feedback_before_final_delivery(official, monkeypatch):
    client, app, _ = official
    _, headers = signup(client, '输出引用员工')
    pid = project(client, headers); enable(client, pid)
    base = '/api/v1/projects/' + pid
    broken = graph()
    fixed = deepcopy(broken)
    fixed['nodes'][-1]['config']['outputs']['rows'] = ref('produce', 'output', 'rows')

    async def turn(self, message, on_event, on_tool, **kwargs):
        first = await on_tool('return_workflow', {'workflow': broken})
        assert not first['structure_check']['valid']
        assert any("['output', 'rows']" in e for e in first['structure_check']['errors'])
        assert not kwargs['tool_result_ready']('return_workflow', first)
        second = await on_tool('return_workflow', {'workflow': fixed})
        assert second['structure_check']['valid']
        assert kwargs['tool_result_ready']('return_workflow', second)
        return {'status': 'completed'}

    async def no_run(*args, **kwargs):
        raise AssertionError('Reference feedback must not execute code')

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    monkeypatch.setattr(app.state.services.workflow_runtime, 'create_run', no_run)
    result = await_generation(client, base, headers, {'instruction': '准备列表处理流程'})
    assert result['status'] == 'completed', result
    assert result['result']['draft']['snapshot']['workflow']['nodes'][-1]['config']['outputs']['rows'] == ref('produce', 'output', 'rows')
    assert client.get(base + '/tasks', headers=headers).json() == []
