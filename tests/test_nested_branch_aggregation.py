"""Branch joins use the skipped nodes of their own persisted graph occurrence."""
import pytest

from tests.test_projects import configured, edge, graph, node, ref, settled, start  # noqa: F401


def branch_body():
    return {'nodes': [
        node('start', 'start', inputs=[{'name': 'choice', 'type': 'boolean'}]),
        node('choose', 'if_else', cases=[{'id': 'yes', 'conditions': [
            {'value': ref('start', 'output', 'choice'), 'expected': True}]}], default_branch='no'),
        node('yes', 'variable_assigner', assignments={'decision': 'yes'}),
        node('no', 'variable_assigner', assignments={'decision': 'no'}),
        node('join.with.dot', 'variable_aggregator', variables=[ref('yes', 'output'), ref('no', 'output')]),
        node('end', 'end', outputs={'result': ref('join.with.dot', 'output')}),
    ], 'edges': [edge('start', 'choose'), edge('choose', 'yes', 'yes'), edge('choose', 'no', 'no'),
                 edge('yes', 'join.with.dot'), edge('no', 'join.with.dot'), edge('join.with.dot', 'end')]}


@pytest.mark.parametrize('scope', ['top', 'iteration', 'loop'])
@pytest.mark.parametrize('choice', [True, False])
def test_branch_join_ignores_only_its_unselected_branch(configured, scope, choice):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    body = branch_body()
    expected = {'decision': 'yes' if choice else 'no'}
    if scope == 'top':
        workflow = body
        inputs = {'choice': choice}
    else:
        if scope == 'iteration':
            # Concurrent occurrences take opposite branches with the same IDs.
            nested = node('nested', 'iteration', workflow=body, items=[choice, not choice, choice],
                          item_name='choice', output_node_id='end', output_path=['result'], parallelism=3)
            output = ref('nested', 'items')
            expected = [expected, {'decision': 'no' if choice else 'yes'}, expected]
        else:
            nested = node('nested', 'loop', workflow=body, variables={'choice': choice},
                          output_node_id='end', break_value=True,
                          break_condition={'value': True, 'operator': 'equals', 'expected': True})
            output = ref('nested', 'output', 'result')
        workflow = {'nodes': [node('start', 'start'), nested, node('end', 'end', outputs={'result': output})],
                    'edges': [edge('start', 'nested'), edge('nested', 'end')]}
        inputs = {}
    graph(client, pid, **workflow)
    task = settled(client, base, start(client, base, 'branch-' + scope, inputs=inputs))
    assert task['status'] == 'succeeded', task.get('error')
    assert task['outputs']['result'] == expected


def test_nested_branch_join_resumes_each_human_answer_without_repeating_completed_records(configured):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    body = branch_body()
    body['nodes'][0]['config']['inputs'] = [{'name': 'item', 'type': 'object'}]
    body['nodes'][1]['config']['cases'][0]['conditions'][0]['value'] = ref('start', 'item', 'needs_input')
    body['nodes'][2] = node('yes', 'human_input', fields=[
        {'name': 'decision', 'label': '处理意见', 'type': 'string', 'required': True}])
    body['nodes'].insert(1, node('record', 'project_record', action='put', collection='visited',
                                 key=ref('start', 'item', 'id'), value={'visited': True}))
    body['edges'][0:1] = [edge('start', 'record'), edge('record', 'choose')]
    records = [{'id': 'auto', 'needs_input': False}, {'id': 'first', 'needs_input': True},
               {'id': 'second', 'needs_input': True}]
    graph(client, pid, [node('start', 'start'),
        node('nested', 'iteration', workflow=body, items=records, item_name='item',
             output_node_id='end', output_path=['result'], parallelism=1),
        node('end', 'end', outputs={'result': ref('nested', 'items')})],
        [edge('start', 'nested'), edge('nested', 'end')])
    task = settled(client, base, start(client, base, 'human-branches'))
    for index, answer in [(1, 'first answer'), (2, 'second answer')]:
        assert task['status'] == 'waiting_input', task.get('error')
        run = task['runs'][0]
        assert run['waiting_input']['node_id'] == f'nested[{index}].yes'
        supplied = client.post(f'{base}/tasks/{task["id"]}/runs/{run["id"]}/input',
                               json={'values': {'decision': answer}})
        assert supplied.status_code == 200, supplied.text
        resumed = client.post(f'{base}/tasks/{task["id"]}/resume', json={})
        assert resumed.status_code == 202, resumed.text
        task = settled(client, base, task)
    assert task['status'] == 'succeeded', task.get('error')
    assert task['outputs']['result'] == [
        {'decision': 'no'}, {'decision': 'first answer'}, {'decision': 'second answer'}]
    for record in records:
        assert client.get(base + '/records/visited/' + record['id']).json()['revision'] == 1
