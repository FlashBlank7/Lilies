"""Small AI edits retain untouched code, scope and the existing atomic save."""
from copy import deepcopy
import json

import pytest

from agent_platform.models import StreamEvent
from agent_platform.project_workflow_edit import apply_generated_updates, scoped_workflow
from tests.test_projects import configured, graph, node, edge, ref, settled  # noqa: F401


def update(node_id, changes, **options):
    return {'op': 'update_node', 'data': {'node_id': node_id, 'changes': changes, **options}}


def provider(app, monkeypatch, document, *, during=None):
    seen = []

    class Provider:
        async def stream(self, **kwargs):
            seen.append(json.loads(kwargs['messages'][0].content[0].text))
            if during:
                await during()
            yield StreamEvent(type='content_block_start', data={'index': 0, 'content_block': {
                'type': 'text', 'text': json.dumps(document)}})

    monkeypatch.setattr(app.state.services.local_agents.connections, 'provider', lambda *a, **k: Provider())
    return seen


def example():
    return {'nodes': [node('s', 'start'),
        node('calculate', 'code', code='def main(inputs):\n    return {"value": inputs["value"]}\n',
             inputs={'value': 2}, timeout=20),
        node('e', 'end', outputs={'value': ref('calculate', 'output', 'value')})],
        'edges': [edge('s', 'calculate'), edge('calculate', 'e')]}


@pytest.mark.parametrize('path', [[], ['each'], ['each', 'inner']])
def test_node_updates_save_once_run_and_undo_without_repeating_unchanged_code(configured, monkeypatch, path):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    original = example()
    for ident in reversed(path):
        original = {'nodes': [node('s', 'start'), node(ident, 'iteration', items=[1], workflow=original,
            output_node_id='e'), node('e', 'end', outputs={'outer': True})],
            'edges': [edge('s', ident), edge(ident, 'e')]}
    graph(client, pid, **original)
    before = client.get(f'/api/v1/applications/{pid}/draft').json()
    operations = [update('calculate', {'config': {'inputs': {'value': 3}}}),
                  update('e', {'config': {'outputs': {'note': '修改后'}}})]
    seen = provider(app, monkeypatch, {'operations': operations})
    response = client.post(base + '/workflow-generation', json={'instruction': '把参数改成3，并增加结果说明',
        'workflow_id': pid, 'expected_revision': before['revision'], 'workflow_path': path,
        'node_ids': ['calculate', 'e']})
    assert response.status_code == 200, response.text
    saved = response.json()
    expected = deepcopy(before['snapshot']['workflow'])
    scope = scoped_workflow(expected, path)
    scope['nodes'][1]['config']['inputs']['value'] = 3
    scope['nodes'][2]['config']['outputs']['note'] = '修改后'
    # The two addressed nodes acquire the same normal form as a manual save.
    from agent_platform.workflow_models import NodeSpec
    scope['nodes'][1:] = [NodeSpec.model_validate(n).model_dump(mode='json') for n in scope['nodes'][1:]]
    assert saved['draft']['snapshot']['workflow'] == expected
    assert saved['previous_workflow'] == before['snapshot']['workflow']
    assert saved['revision'] == before['revision'] + 1
    assert len(seen) == 1 and client.get(base + '/tasks').json() == []
    if not path:
        task = client.post(base + '/tasks', json={'request_key': 'after-small-edit', 'workflow_id': pid, 'inputs': {}})
        assert task.status_code == 202, task.text
        result = settled(client, base, task.json())
        assert result['status'] == 'succeeded', result
        assert result['outputs'] == {'value': 3, 'note': '修改后'}
    undo = client.put(base + f'/workflows/{pid}/draft', json={'expected_revision': saved['revision'],
        'workflow': saved['previous_workflow']})
    assert undo.status_code == 200, undo.text
    assert undo.json()['draft']['snapshot']['workflow'] == before['snapshot']['workflow']


def test_nested_paths_disambiguate_repeated_ids_and_keep_unselected_nodes_exact(configured):
    _, app, _, _ = configured
    body = example()
    original = {'nodes': [node('s', 'start'), node('each', 'iteration', items=[1], workflow=body,
        output_node_id='e'), node('e', 'end')], 'edges': [edge('s', 'each'), edge('each', 'e')]}
    saved = deepcopy(original)
    operations = [{'op': 'update_node', 'data': {'node_path': ['each', 'e'], 'changes': {'title': '内层结果'}}}]
    result = apply_generated_updates(app.state.services, original, operations, [], ['each']).model_dump(mode='json')
    nested = result['nodes'][1]['config']['workflow']['nodes']
    assert nested[-1]['title'] == '内层结果'
    assert nested[:2] == saved['nodes'][1]['config']['workflow']['nodes'][:2]
    assert original == saved
    assert result['nodes'][-1]['title'] == original['nodes'][-1]['title']
    with pytest.raises(ValueError, match='选区外'):
        apply_generated_updates(app.state.services, original, [update('e', {'title': '错误'})], [], ['each'])


@pytest.mark.parametrize('bad', [
    {'op': 'set_metadata', 'data': {'name': '不属于本次图修改'}},
    update('missing', {'title': '不存在'}),
    update('calculate', {'id': 'renamed'}),
    update('calculate', {'config': {}}, merge_config='false'),
    {'op': 'update_node', 'data': {'node_id': 'calculate', 'node_path': ['calculate'], 'changes': {'title': '歧义'}}},
    {'op': 'update_node', 'data': {'node_path': 'calculate', 'changes': {'title': '格式错误'}}},
])
def test_failed_batch_does_not_save_partial_changes_or_start_business(configured, monkeypatch, bad):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    graph(client, pid, **example())
    before = client.get(f'/api/v1/applications/{pid}/draft').json()
    provider(app, monkeypatch, {'operations': [update('calculate', {'title': '未保存'}), bad]})
    response = client.post(base + '/workflow-generation', json={'instruction': '调整节点',
        'workflow_id': pid, 'expected_revision': before['revision']})
    assert response.status_code == 422, response.text
    assert client.get(f'/api/v1/applications/{pid}/draft').json() == before
    assert client.get(base + '/tasks').json() == []


def test_updates_preserve_config_merge_replace_semantics_and_explicit_file_bindings(configured):
    _, app, _, _ = configured
    original = example()
    original['nodes'][0]['config']['inputs'] = [{'name': 'source_path', 'type': 'file', 'default': 'old.csv'}]
    files = [{'path': 'requirement-package/selected/new.csv'}]
    operations = [update('s', {'title': '改标题但保持旧文件'}),
        update('calculate', {'config': {'inputs': {'extra': 1}}}),
        update('e', {'config': {'outputs': {'only': True}}}, merge_config=False)]
    result = apply_generated_updates(app.state.services, original, operations, [], [], files)
    assert result.nodes[0].config['inputs'][0]['default'] == 'old.csv'
    assert result.nodes[1].config['inputs'] == {'value': 2, 'extra': 1}
    assert result.nodes[1].config['code'] == original['nodes'][1]['config']['code']
    assert result.nodes[2].config['outputs'] == {'only': True}
    operations = [update('s', {'config': {'inputs': [{'name': 'source_path', 'type': 'file', 'default': 'new.csv'}]}})]
    result = apply_generated_updates(app.state.services, original, operations, [], [], files)
    assert result.nodes[0].config['inputs'][0]['default'] == files[0]['path']


def test_concurrent_manual_edit_is_not_overwritten(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    graph(client, pid, **example())
    before = client.get(f'/api/v1/applications/{pid}/draft').json()

    async def manual_edit():
        from agent_platform.project_workflow_edit import save_workflow, SaveWorkflow
        from agent_platform.workflow_models import WorkflowSpec
        changed = deepcopy(before['snapshot']['workflow'])
        changed['nodes'][0]['title'] = '人工刚修改的标题'
        await save_workflow(app.state.services, pid, pid, SaveWorkflow(expected_revision=before['revision'],
            workflow=WorkflowSpec.model_validate(changed)))

    provider(app, monkeypatch, {'operations': [update('calculate', {'title': 'AI较晚返回'})]}, during=manual_edit)
    response = client.post(base + '/workflow-generation', json={'instruction': '改计算标题',
        'workflow_id': pid, 'expected_revision': before['revision']})
    assert response.status_code == 409, response.text
    saved = client.get(f'/api/v1/applications/{pid}/draft').json()
    assert saved['revision'] == before['revision'] + 1
    assert saved['snapshot']['workflow']['nodes'][0]['title'] == '人工刚修改的标题'
    assert saved['snapshot']['workflow']['nodes'][1]['title'] != 'AI较晚返回'


def test_updates_do_not_require_bound_resources_or_runnable_structure(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    graph(client, pid, [node('s', 'start'), node('predict', 'model_predict', model_ref='', dataset_id=''),
        node('e', 'end')], [edge('s', 'e')])
    before = client.get(f'/api/v1/applications/{pid}/draft').json()
    provider(app, monkeypatch, {'operations': [update('predict', {'title': '稍后绑定预测模型'})]})
    response = client.post(base + '/workflow-generation', json={'instruction': '改成易懂标题',
        'workflow_id': pid, 'expected_revision': before['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['structure_check']['valid'] is False
    assert response.json()['draft']['snapshot']['workflow']['nodes'][1]['config']['model_ref'] == ''
    assert client.get(base + '/tasks').json() == []


@pytest.mark.parametrize('nested', [False, True])
def test_updates_keep_project_agent_capability_restrictions(configured, monkeypatch, nested):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    original = example()
    if nested:
        original = {'nodes': [node('s', 'start'), node('each', 'iteration', items=[1], workflow=original,
            output_node_id='e'), node('e', 'end')], 'edges': [edge('s', 'each'), edge('each', 'e')]}
    graph(client, pid, **original)
    before = client.get(f'/api/v1/applications/{pid}/draft').json()
    operation = update('calculate', {'type': 'claude_agent', 'config': {'agent_id': 'later', 'task': 'test'}}, merge_config=False)
    if nested:
        operation['data'].pop('node_id')
        operation['data']['node_path'] = ['each', 'calculate']
    provider(app, monkeypatch, {'operations': [operation]})
    response = client.post(base + '/workflow-generation', json={'instruction': '更换节点',
        'workflow_id': pid, 'expected_revision': before['revision']})
    assert response.status_code == 422 and '禁止使用智能体积木' in response.text
    assert client.get(f'/api/v1/applications/{pid}/draft').json() == before


def test_new_workflow_requires_a_graph_and_does_not_create_half_member(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    before = client.get(base + '/members').json()
    provider(app, monkeypatch, {'operations': [update('e', {'title': '没有原图'})]})
    response = client.post(base + '/workflow-generation', json={'instruction': '新建流程'})
    assert response.status_code == 422 and '完整 workflow' in response.text
    assert client.get(base + '/members').json() == before
