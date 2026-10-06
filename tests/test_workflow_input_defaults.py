"""Declared defaults reach both start outputs and $inputs in the saved run."""
from copy import deepcopy

import pytest

from tests.test_projects import configured, edge, graph, node, ref, settled, start  # noqa: F401


FIELDS = [
    {'name': 'source_path', 'type': 'file', 'default': 'requirement-package/example.csv'},
    {'name': 'threshold_percent', 'type': 'number', 'default': 5},
    {'name': 'enabled', 'type': 'boolean', 'default': True},
    {'name': 'label', 'type': 'string', 'default': 'saved label'},
    {'name': 'nullable', 'type': 'string', 'required': False, 'default': 'fallback'},
    {'name': 'rows', 'type': 'array', 'default': [1]},
    {'name': 'options', 'type': 'object', 'default': {'mode': 'saved'}},
]


def compared_inputs():
    return {'via_inputs': {field['name']: ref('$inputs', field['name']) for field in FIELDS},
            'via_start': ref('start', 'output')}


@pytest.mark.parametrize('trigger', ['start', 'event_subscription_trigger', 'schedule_trigger'])
@pytest.mark.parametrize('supplied', [{}, {
    'source_path': '', 'threshold_percent': 0, 'enabled': False, 'label': '',
    'nullable': None, 'rows': [], 'options': {},
}])
def test_api_defaults_match_start_output_and_preserve_explicit_values(configured, trigger, supplied):
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    defaults = {field['name']: field['default'] for field in FIELDS}
    fields = defaults if trigger == 'schedule_trigger' else FIELDS
    trigger_options = {'subscription_name': 'saved-defaults'} if trigger == 'event_subscription_trigger' else {}
    graph(client, pid, [node('start', trigger, inputs=fields, **trigger_options), node('end', 'end', outputs=compared_inputs())],
          [edge('start', 'end')])
    snapshot = client.get(f'/api/v1/applications/{pid}/draft').json()['snapshot']
    task = settled(client, base, start(client, base, 'defaults', inputs=supplied))
    assert task['status'] == 'succeeded', task.get('error')
    expected = {**defaults, **supplied}
    assert task['outputs'] == {'via_inputs': expected, 'via_start': expected}
    stored = client.portal.call(app.state.services.workflow_store.get_run, task['runs'][0]['id'])
    assert stored['state'].inputs == supplied
    assert stored['state'].snapshot.model_dump(mode='json') == snapshot
    assert client.get(f'/api/v1/applications/{pid}/draft').json()['snapshot'] == snapshot


@pytest.mark.parametrize('nested', [False, True])
def test_defaults_survive_human_resume_with_original_snapshot(configured, nested):
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    body = {'nodes': [
        node('start', 'start', inputs=deepcopy(FIELDS)),
        node('save', 'project_record', action='put', collection='defaults', key='once',
             value={'source': ref('$inputs', 'source_path')}),
        node('human', 'human_input', fields=[{'name': 'decision', 'label': '处理意见', 'type': 'string'}]),
        node('end', 'end', outputs={**compared_inputs(), 'decision': ref('human', 'decision')}),
    ], 'edges': [edge('start', 'save'), edge('save', 'human'), edge('human', 'end')]}
    if nested:
        workflow = {'nodes': [node('outer', 'start', inputs=[
            {'name': 'threshold_percent', 'type': 'number', 'default': 99},
            {'name': 'parent_only', 'type': 'number', 'default': 42}]),
            node('each', 'iteration', items=[1], workflow=body, output_node_id='end'),
            node('end', 'end', outputs={'items': ref('each', 'items')})],
            'edges': [edge('outer', 'each'), edge('each', 'end')]}
    else:
        workflow = body
    graph(client, pid, **workflow)
    original = client.get(f'/api/v1/applications/{pid}/draft').json()['snapshot']
    task = settled(client, base, start(client, base, 'pause-defaults', inputs={}))
    assert task['status'] == 'waiting_input', task.get('error')
    run_id = task['runs'][0]['id']
    stored = client.portal.call(app.state.services.workflow_store.get_run, run_id)
    assert stored['state'].inputs == {}
    if nested:
        # Match existing checkpoints: inherited inputs omit parent defaults.
        assert stored['state'].nested_progress['each[0].']['inputs'] == {'item': 1, 'index': 0}
    # A later edit cannot replace defaults captured by this existing run.
    body['nodes'][0]['config']['inputs'][0]['default'] = 'requirement-package/changed.csv'
    graph(client, pid, **workflow)
    answered = client.post(f'{base}/tasks/{task["id"]}/runs/{run_id}/input', json={'values': {'decision': 'continue'}})
    assert answered.status_code == 200, answered.text
    resumed = client.post(f'{base}/tasks/{task["id"]}/resume', json={})
    assert resumed.status_code == 202, resumed.text
    task = settled(client, base, task)
    assert task['status'] == 'succeeded', task.get('error')
    output = task['outputs']['items'][0] if nested else task['outputs']
    expected = {field['name']: field['default'] for field in FIELDS}
    assert output == {'via_inputs': expected, 'via_start': expected, 'decision': 'continue'}
    assert client.get(base + '/records/defaults/once').json()['revision'] == 1
    stored = client.portal.call(app.state.services.workflow_store.get_run, run_id)
    assert stored['state'].snapshot.model_dump(mode='json') == original


def test_explicit_null_does_not_fall_back_for_required_input(configured):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    graph(client, pid, [node('start', 'start', inputs=FIELDS),
        node('end', 'end', outputs=compared_inputs())], [edge('start', 'end')])
    task = settled(client, base, start(client, base, 'null-required', inputs={'source_path': None}))
    assert task['status'] == 'failed'
    assert 'source_path' in task['error']
    assert '必填' in task['error']


@pytest.mark.parametrize('trigger', ['start', 'event_subscription_trigger', 'schedule_trigger'])
def test_declared_internal_default_cannot_answer_a_human_question(configured, trigger):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    preset = {'human': {'decision': 'not a human answer'}}
    fields = {'__human__': preset} if trigger == 'schedule_trigger' else [
        {'name': '__human__', 'type': 'object', 'default': preset}]
    trigger_options = {'subscription_name': 'human-default'} if trigger == 'event_subscription_trigger' else {}
    graph(client, pid, [node('start', trigger, inputs=fields, **trigger_options),
        node('human', 'human_input', fields=[{'name': 'decision', 'label': '处理意见', 'type': 'string'}]),
        node('end', 'end', outputs={'answer': ref('human', 'decision')})],
        [edge('start', 'human'), edge('human', 'end')])
    task = settled(client, base, start(client, base, 'reserved-default', inputs={}))
    assert task['status'] == 'waiting_input', task.get('error')
    assert task['runs'][0]['waiting_input']['node_id'] == 'human'


def test_result_reuse_compares_effective_default_inputs(configured):
    client, _, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    nodes = [node('start', 'start', inputs=[{'name': 'threshold', 'type': 'number', 'default': 5}]),
        node('render', 'template_transform', template='threshold={{ threshold }}',
             variables={'threshold': ref('$inputs', 'threshold')}),
        node('end', 'end', outputs={'text': ref('render', 'text')})]
    edges = [edge('start', 'render'), edge('render', 'end')]
    graph(client, pid, nodes, edges)
    first = settled(client, base, start(client, base, 'first', inputs={}))
    assert first['status'] == 'succeeded', first.get('error')
    reused = settled(client, base, start(client, base, 'same-default', inputs={}, reuse_task_id=first['id']))
    assert reused['outputs'] == {'text': 'threshold=5'}
    assert reused['runs'][0]['reuse']['nodes'] == ['start', 'render', 'end']
    nodes[0]['config']['inputs'][0]['default'] = 7
    graph(client, pid, nodes, edges)
    changed = settled(client, base, start(client, base, 'changed-default', inputs={}, reuse_task_id=first['id']))
    assert changed['outputs'] == {'text': 'threshold=7'}
    assert changed['runs'][0]['reuse']['nodes'] == []
    assert client.get(base + '/tasks/' + first['id']).json()['outputs'] == {'text': 'threshold=5'}
