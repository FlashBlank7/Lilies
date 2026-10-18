"""Business explanations stay project scoped; routes reflect actual draft edges."""
from tests.test_projects import configured as _configured, fixture_graphs, start, settled, graph, node, edge  # noqa: F401


configured = _configured

def test_requirement_comparison_persists_with_real_references_and_rejects_foreign_ones(configured):
    client, app, project, settings = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
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
    pid = project['id']
    base = '/api/v1/projects/' + pid
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


def test_saved_input_instructions_follow_current_draft_without_running(configured):
    from agent_platform.blocks import StartConfig
    client, app, project, settings = configured
    pid = project['id']
    field = {'name': 'source_path', 'label': '生产原始表', 'description': '保留Sheet2和两行表头',
             'type': 'file', 'required': True, 'default': 'private-default.xlsx'}
    assert StartConfig(inputs=[field]).inputs[0].description == field['description']
    graph(client, pid, [node('s', 'start', inputs=[field]), node('e', 'end', outputs={'完整报告': 'results/report.md'})], [edge('s', 'e')])
    flow = client.get(f'/api/v1/projects/{pid}/topology').json()['flows'][pid]
    assert flow['inputs'] == [{key: field[key] for key in ('name', 'label', 'type', 'required', 'description')}]
    assert flow['outputs'] == ['完整报告']
    assert 'default' not in flow['inputs'][0]
    field['description'] = '保留Sheet2，勿上传文档或旧预测结果'
    graph(client, pid, [node('s', 'start', inputs=[field]), node('e', 'end', outputs={'完整报告': 'results/report.md'})], [edge('s', 'e')])
    assert client.get(f'/api/v1/projects/{pid}/topology').json()['flows'][pid]['inputs'][0]['description'] == field['description']
    assert client.get(f'/api/v1/projects/{pid}/tasks').json() == []


def test_topology_uses_runtime_input_defaults_and_preserves_explicit_optional(configured):
    from agent_platform.blocks import StartConfig
    client, app, project, settings = configured
    pid = project['id']
    fields = [{'name': 'source_path', 'label': '原表', 'type': 'file'},
              {'name': 'note', 'required': False, 'default': 'private-note'}]
    graph(client, pid, [node('s', 'start', inputs=fields), node('e', 'end')], [edge('s', 'e')])
    saved = client.get(f'/api/v1/applications/{pid}/draft').json()['snapshot']['workflow']
    runtime = StartConfig.model_validate(saved['nodes'][0]['config'])
    inputs = client.get(f'/api/v1/projects/{pid}/topology').json()['flows'][pid]['inputs']
    assert inputs == [field.model_dump(include={'name', 'label', 'type', 'required', 'description'}) for field in runtime.inputs]
    assert inputs[0]['required'] is True
    assert inputs[1]['required'] is False
    assert inputs[1]['type'] == 'string'
    assert all('default' not in field for field in inputs)
    assert client.get(f'/api/v1/projects/{pid}/tasks').json() == []
