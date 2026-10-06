"""A generated branch join preserves zero and resumes the same iteration."""
import pytest

from tests.test_projects import configured, node, edge, ref, settled, start  # noqa: F401
from tests.test_project_space_generation import provider


def joined_batches():
    inner = {
        'nodes': [
            node('s', 'start', inputs=[{'name': 'item', 'type': 'object', 'required': True}]),
            node('missing', 'if_else', cases=[{'id': 'yes', 'conditions': [
                {'value': ref('$inputs', 'item', 'missing'), 'operator': 'equals', 'expected': True}]}],
                default_branch='complete'),
            node('ask', 'human_input', title='补充当前批次', fields=[
                {'name': 'count', 'label': '实际数量', 'type': 'number', 'required': True}]),
            node('join', 'variable_aggregator', variables=[
                ref('ask', 'output', 'count'), ref('$inputs', 'item', 'count')], mode='first_non_null'),
            node('calculate', 'variable_assigner', assignments={
                'count': ref('join', 'output'),
                'rate': {'$formula': {'expression': 'count / total * 100', 'vars': {
                    'count': ref('join', 'output'), 'total': ref('$inputs', 'item', 'total')}}}}),
            node('e', 'end', outputs={'result': ref('calculate', 'output')}),
        ],
        'edges': [edge('s', 'missing'),
                  {**edge('missing', 'ask', 'yes'), 'source_port': 'branch'},
                  {**edge('missing', 'join', 'complete'), 'source_port': 'branch'},
                  edge('ask', 'join'), edge('join', 'calculate'), edge('calculate', 'e')],
    }
    return {
        'nodes': [node('s', 'start', inputs=[{'name': 'batches', 'type': 'array', 'required': True}]),
                  node('each', 'iteration', items=ref('$inputs', 'batches'), workflow=inner,
                       output_node_id='calculate', output_path=['output'], parallelism=1, reuse_completed=True),
                  node('e', 'end', outputs={'results': ref('each', 'items')})],
        'edges': [edge('s', 'each'), {**edge('each', 'e'), 'source_port': 'items'}],
    }


@pytest.mark.parametrize('answer', [0, 3])
def test_generated_join_waits_only_for_missing_values_and_keeps_completed_batches(configured, monkeypatch, answer):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    seen = []
    workflow = joined_batches()
    provider(monkeypatch, app.state.services, workflow, seen)
    created = client.post(base + '/workflow-generation', json={
        'instruction': '逐批计算比例，只对缺失数量提问，完整和补充的数据采用相同计算。'})
    assert created.status_code == 200, created.text
    assert created.json()['structure_check']['valid'] is True
    assert len(seen) == 1
    assert client.get(base + '/tasks').json() == []

    task = settled(client, base, start(client, base, 'joined-batches',
        workflow_id=created.json()['workflow_id'], inputs={'batches': [
            {'count': 0, 'total': 10, 'missing': False},
            {'count': None, 'total': 10, 'missing': True},
            {'count': 7, 'total': 10, 'missing': False},
        ]}))
    assert task['status'] == 'waiting_input', task
    run = task['runs'][0]
    assert run['waiting_input']['node_id'] == 'each[1].ask'
    url = f'{base}/tasks/{task["id"]}/runs/{run["id"]}/input'
    invalid = client.post(url, json={'node_id': 'each[1].ask', 'values': {}, 'resume': True})
    assert invalid.status_code == 422, invalid.text
    assert client.get(base + '/tasks/' + task['id']).json()['status'] == 'waiting_input'

    resumed = client.post(url, json={
        'node_id': 'each[1].ask', 'values': {'count': answer}, 'resume': True})
    assert resumed.status_code == 200, resumed.text
    done = settled(client, base, task)
    assert done['status'] == 'succeeded', done
    assert done['id'] == task['id'] and [r['id'] for r in done['runs']] == [run['id']]
    assert done['outputs']['results'] == [
        {'count': 0, 'rate': 0}, {'count': answer, 'rate': answer * 10}, {'count': 7, 'rate': 70}]
    assert len(seen) == 1  # Human resume does not generate another graph.
