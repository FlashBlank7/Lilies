"""The employee step view reads frozen run state, with bounded supplementary events."""
import asyncio
import json

import pytest

from tests.test_projects import configured, edge, graph, node, ref, settled, start
from tests.test_users import signup


def execute(client, project, key='steps', inputs=None):
    base = '/api/v1/projects/' + project['id']
    task = settled(client, base, start(client, base, key, inputs=inputs or {}))
    return base, task, task['runs'][0]['id']


def steps(client, run_id, **params):
    response = client.get(f'/api/v1/runs/{run_id}/steps', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_success_uses_saved_graph_and_values_after_draft_changes_and_long_event_tail(configured):
    client, app, project, _ = configured
    transform = node('prepare', 'variable_assigner', assignments={'quantity': ref('$inputs', 'quantity')})
    transform.update(title='原来的处理步骤', description='按本次订单准备数量')
    graph(client, project['id'], [node('end', 'end', outputs={'quantity': ref('prepare', 'output', 'quantity')}),
          transform, node('start', 'start')], [edge('start', 'prepare'), edge('prepare', 'end')])
    _, task, run_id = execute(client, project, inputs={'quantity': 7})
    assert task['status'] == 'succeeded', task
    graph(client, project['id'], [node('new', 'start')], [])

    async def append_tail():
        for _ in range(1005):
            await app.state.services.storage.append_event(run_id, 'model.delta', {'text': 'later'})
    asyncio.run(append_tail())
    result = steps(client, run_id)
    assert result['draft_revision'] == 1
    assert [s['id'] for s in result['steps']] == ['start', 'prepare', 'end']
    assert all(s['status'] == 'completed' for s in result['steps'])
    assert all(s['duration_ms'] is None for s in result['steps'])
    prepared = result['steps'][1]
    assert prepared['title'] == '原来的处理步骤'
    assert prepared['description'] == '按本次订单准备数量'
    assert prepared['input_preview'] == {'assignments': {'quantity': 7}}
    assert prepared['input_source'] == '按本次记录还原'
    assert result['steps'][0]['input_source'] == '本次输入'
    assert prepared['output_preview'] == {'output': {'quantity': 7}}
    assert prepared['node_path'] == ['prepare'] and prepared['scope'] == ''
    first = steps(client, run_id, limit=2)
    second = steps(client, run_id, limit=2, offset=first['next_offset'])
    assert first['total'] == second['total'] == 3
    assert [s['id'] for s in first['steps'] + second['steps']] == ['start', 'prepare', 'end']
    assert second['next_offset'] is None
    assert steps(client, run_id, offset=99)['steps'] == []
    assert client.get(f'/api/v1/runs/{run_id}/steps?limit=201').status_code == 422


@pytest.mark.parametrize('strategy,expected', [('fail', 'failed'), ('continue', 'warning')])
def test_failure_and_continue_are_different_step_results(configured, strategy, expected):
    client, _, project, _ = configured
    bad = node('bad', 'variable_assigner', assignments={'quantity': {'$formula': '1 / 0'}})
    bad['error_strategy'] = strategy
    graph(client, project['id'], [node('start', 'start'), bad, node('end', 'end')],
          [edge('start', 'bad'), edge('bad', 'end')])
    _, task, run_id = execute(client, project)
    result = steps(client, run_id)
    assert task['status'] == ('failed' if strategy == 'fail' else 'succeeded'), task
    assert [s['status'] for s in result['steps']] == ['completed', expected, 'pending' if strategy == 'fail' else 'completed']
    assert result['steps'][1]['error']
    assert result['steps'][1]['duration_ms'] is not None


def test_long_python_traceback_keeps_final_missing_fields_and_redacts_credentials(configured, monkeypatch):
    from agent_platform.workflow_runtime import _NODE_EXECUTORS

    client, _, project, _ = configured
    traceback = ('Python 执行失败：Traceback (most recent call last):\n'
                 + '  File "workflow.py", line 10, in prepare\n    validate_columns(inputs)\n' * 30
                 + 'ValueError: 缺少必填字段\n销售表：订单编号、销售数量\n库存表：仓库编号\n'
                 + 'api_key=private-test-key\nAuthorization: Bearer private-test-token\n'
                 + '请补齐字段后重新运行。' * 150)

    async def missing_fields(runtime, run):
        raise ValueError(traceback)

    monkeypatch.setitem(_NODE_EXECUTORS, 'variable_assigner', missing_fields)
    graph(client, project['id'], [node('start', 'start'), node('prepare', 'variable_assigner'), node('end', 'end')],
          [edge('start', 'prepare'), edge('prepare', 'end')])
    _, task, run_id = execute(client, project)
    assert task['status'] == 'failed', task
    result = steps(client, run_id)
    for error in (result['error'], result['steps'][1]['error']):
        assert error.startswith('ValueError: 缺少必填字段')
        assert '销售表：订单编号、销售数量\n库存表：仓库编号' in error
        assert 'private-test-key' not in error and 'private-test-token' not in error
        assert len(error) <= 1001 and error.endswith('…')
    events = client.get(f'/api/v1/runs/{run_id}/events/list').json()['events']
    assert next(e['data']['error'] for e in events if e['type'] == 'node.failed') == traceback


def test_nested_pause_resume_has_distinct_occurrences_and_saved_inputs(configured):
    client, _, project, _ = configured
    inner = {'nodes': [node('start', 'start'),
        node('human', 'human_input', fields=[{'name': 'answer', 'label': '回答', 'type': 'string'}]),
        node('end', 'end', outputs={'item': ref('$inputs', 'item'), 'answer': ref('human', 'answer')})],
        'edges': [edge('start', 'human'), edge('human', 'end')]}
    graph(client, project['id'], [node('start', 'start'),
        node('each', 'iteration', items=['第一项', '第二项'], parallelism=1, workflow=inner, output_node_id='end'),
        node('end', 'end')], [edge('start', 'each'), edge('each', 'end')])
    base, task, run_id = execute(client, project)
    assert task['status'] == 'waiting_input', task
    paused = {s['id']: s for s in steps(client, run_id)['steps']}
    assert paused['each[0].human']['status'] == 'waiting'
    assert paused['each[0].start']['input_preview']['item'] == '第一项'
    assert paused['each[0].human']['node_path'] == ['each', 'human']
    assert paused['each[0].human']['scope'] == 'each · 第 1 轮'
    for answer in ('第一答', '第二答'):
        response = client.post(f'{base}/tasks/{task["id"]}/runs/{run_id}/input', json={'values': {'answer': answer}})
        assert response.status_code == 200, response.text
        response = client.post(f'{base}/tasks/{task["id"]}/resume', json={})
        assert response.status_code == 202, response.text
        task = settled(client, base, task)
    assert task['status'] == 'succeeded', task
    done = {s['id']: s for s in steps(client, run_id)['steps']}
    assert done['each[0].end']['output_preview'] == {'item': '第一项', 'answer': '第一答'}
    assert done['each[1].end']['output_preview'] == {'item': '第二项', 'answer': '第二答'}
    assert all(s['status'] == 'completed' for s in done.values())


def test_previews_are_bounded_and_hide_secrets_and_code(configured):
    client, _, project, _ = configured
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'payload': ref('$inputs')})],
          [edge('start', 'end')])
    inputs = {'api_key': 'key-should-not-appear', 'password': 'pass-should-not-appear',
              'nested': {'Authorization': 'Bearer auth-should-not-appear'},
              'code': 'private-complete-source', 'many': ['x' * 3000] * 100,
              'deep': {'a': {'a': {'a': {'a': {'a': 'hidden-depth'}}}}}}
    _, task, run_id = execute(client, project, inputs=inputs)
    assert task['status'] == 'succeeded', task
    result = steps(client, run_id)
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in ('key-should-not-appear', 'pass-should-not-appear', 'auth-should-not-appear',
                   'private-complete-source', 'hidden-depth'):
        assert secret not in serialized
    assert '••••••' in serialized and '…' in serialized
    assert len(serialized) < 20000


def test_run_steps_require_login_and_project_membership(configured):
    client, _, project, _ = configured
    graph(client, project['id'], [node('start', 'start'), node('end', 'end')], [edge('start', 'end')])
    _, _, run_id = execute(client, project)
    _, outsider = signup(client, '无关员工')
    route = f'/api/v1/runs/{run_id}/steps'
    assert client.get(route, headers={'Authorization': ''}).status_code == 401
    assert client.get(route, headers=outsider).status_code == 404
    client.post(f'/api/v1/projects/{project["id"]}/access-members', json={'name': '无关员工'}).raise_for_status()
    assert client.get(route, headers=outsider).status_code == 200
    assert client.get('/api/v1/runs/missing/steps').status_code == 404


def test_business_summary_prefers_saved_report_even_after_technical_keys(configured):
    client, _, project, _ = configured
    report = '共4根物料、6个单料组合。\n\n料三：长度不足；料四：没有同类型需求。\n\n只覆盖本次声明条件。'
    result = {**{f'technical_{i}': i for i in range(14)}, 'markdown': report,
              'sha256': 'fingerprint', 'comparison_inputs': {'source_path': 'results/candidates.csv'}}
    graph(client, project['id'], [node('start', 'start'),
          node('prepare', 'variable_assigner', assignments={'result': result}),
          node('end', 'end', outputs={'result': ref('prepare', 'output', 'result')})],
          [edge('start', 'prepare'), edge('prepare', 'end')])
    _, task, run_id = execute(client, project)
    assert task['status'] == 'succeeded', task
    saved = steps(client, run_id)['steps'][1]
    assert saved['output_summary'] == {'markdown': report}
    assert 'technical_0' in json.dumps(saved['output_preview'])
    graph(client, project['id'], [node('new', 'start')], [])
    assert steps(client, run_id)['steps'][1]['output_summary'] == {'markdown': report}
