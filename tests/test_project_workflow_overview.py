"""Business explanations stay project scoped; routes reflect actual draft edges."""
from tests.test_projects import configured, fixture_graphs, start, settled, graph, node, edge  # noqa: F401


def test_requirement_comparison_persists_with_real_references_and_rejects_foreign_ones(configured):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    _, validate, allocate = fixture_graphs(client, project)
    task = settled(client, base, start(client, base, 'explanation', inputs={'request_id': 'a'}))
    value = {'goal': '资源分配', 'workflows': [{'workflow_id': validate, 'purpose': '校验申请', 'inputs': '申请标识', 'outputs': '有效申请'}],
             'items': [{'id': 'allocate', 'title': '资源分配', 'goal': '有效申请得到资源', 'status': 'working', 'next_action': '核实分配结果',
                        'workflow_ids': [validate, allocate], 'requirements': [
                            {'requirement': '资源不足时保留等待申请', 'source': '需求第2节', 'current': '本次实际返回等待资源',
                             'status': 'partial', 'gap': '待释放资源后继续验证', 'results': [{'label': '实际结果', 'task_id': task['id']}]}]}]}
    response = client.put(base+'/progress', json={'expected_revision': 0, 'value': value})
    assert response.status_code == 200, response.text
    saved = client.get(base+'/progress').json()
    assert saved['value']['workflows'][0]['inputs'] == '申请标识'
    assert saved['value']['items'][0]['requirements'][0]['results'][0]['task_id'] == task['id']
    assert client.put(base+'/progress', json={'expected_revision': 0, 'value': value}).status_code == 409
    other = client.post('/api/v1/projects', json={'name': '外部项目'}).json()['id']
    value['workflows'][0]['workflow_id'] = other
    assert client.put(base+'/progress', json={'expected_revision': 1, 'value': value}).status_code == 422
    value['workflows'][0]['workflow_id'] = validate
    comparison = value['items'][0]['requirements'][0]
    comparison['results'] = [{'label': '越界文件', 'file_path': '../outside.txt'}]
    assert client.put(base+'/progress', json={'expected_revision': 1, 'value': value}).status_code == 422
    outside = start(client, '/api/v1/projects/'+other, 'outside')
    comparison['results'] = [{'label': '越界任务', 'task_id': outside['id']}]
    assert client.put(base+'/progress', json={'expected_revision': 1, 'value': value}).status_code == 404
    assert client.get(base+'/progress').json() == saved


def test_topology_preserves_call_order_and_conditional_branches_without_running(configured):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    _, validate, allocate = fixture_graphs(client, project)
    topology = client.get(base+'/topology').json()
    main = topology['flows'][pid]
    assert [(c['node_id'], c['target']) for c in topology['calls'] if c['source'] == pid] == [('check', validate), ('allocate', allocate)]
    assert {'source': 'check', 'target': 'allocate', 'branch': None} in main['edges']
    conditional = topology['flows'][allocate]
    assert {'source': 'available', 'target': 'wait', 'branch': 'else'} in conditional['edges']
    assert next(n for n in conditional['nodes'] if n['id'] == 'available')['branches'][0]['conditions'][0]['expected'] == ''
    assert client.get(base+'/tasks').json() == []
    graph(client, validate, [node('s', 'start'), node('e', 'end', outputs={'checked': True})], [edge('s', 'e')])
    updated = client.get(base+'/topology').json()['flows'][validate]
    assert updated['revision'] > topology['flows'][validate]['revision']
    assert [n['id'] for n in updated['nodes']] == ['s', 'e']
