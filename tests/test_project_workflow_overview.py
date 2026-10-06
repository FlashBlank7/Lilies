"""Business explanations stay project scoped; routes reflect actual draft edges."""
from tests.test_projects import configured, fixture_graphs, start, settled, graph, node, edge, ref  # noqa: F401


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
    assert main['inputs'] == [{'name': 'request_id', 'label': '', 'type': 'string',
                              'required': True, 'description': ''}]
    assert main['outputs'] == ['result']
    assert topology['flows'][validate]['outputs'] == ['request_id']
    assert [(c['node_id'], c['target']) for c in topology['calls'] if c['source'] == pid] == [('check', validate), ('allocate', allocate)]
    assert {'source': 'check', 'target': 'allocate', 'branch': None} in main['edges']
    conditional = topology['flows'][allocate]
    assert conditional['outputs'] == ['allocated', 'owner', 'task_status', 'message']
    assert {'source': 'available', 'target': 'wait', 'branch': 'else'} in conditional['edges']
    assert next(n for n in conditional['nodes'] if n['id'] == 'available')['branches'][0]['conditions'][0]['expected'] == ''
    assert client.get(base+'/tasks').json() == []
    graph(client, validate, [node('s', 'start', inputs=[{'name': 'quantity', 'label': '数量', 'type': 'number',
        'required': False, 'description': '本次申请数量', 'default': 123, 'example': 456}]),
        node('e', 'end', outputs={'checked': True})], [edge('s', 'e')])
    updated = client.get(base+'/topology').json()['flows'][validate]
    assert updated['revision'] > topology['flows'][validate]['revision']
    assert [n['id'] for n in updated['nodes']] == ['s', 'e']
    assert updated['inputs'] == [{'name': 'quantity', 'label': '数量', 'type': 'number',
                                 'required': False, 'description': '本次申请数量'}]
    assert updated['outputs'] == ['checked']


def test_topology_contract_keeps_nested_and_called_workflow_definitions_separate(configured):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    child = client.post(base+'/members', json={'name': '内部处理'}).json()['id']
    graph(client, child, [node('s', 'start', inputs=[{'name': 'child_input'}]),
        node('e', 'end', outputs={'child_output': 'private child value'})], [edge('s', 'e')])
    nested = {'nodes': [node('s', 'start', inputs=[{'name': 'item'}]),
                       node('e', 'end', outputs={'nested_output': 'private nested value'})],
              'edges': [edge('s', 'e')]}
    graph(client, pid, [node('s', 'start', inputs=[{'name': 'request', 'default': 'private default'}]),
        node('items', 'iteration', items=[], workflow=nested, output_node_id='e'),
        node('call', 'tool', tool_name='workflow:'+child, input={'child_input': ref('$inputs', 'request')}),
        node('choice', 'if_else', cases=[{'id': 'yes', 'conditions': [{'value': True, 'expected': True}]}]),
        node('answer', 'answer', answer=ref('call', 'output', 'child_output')),
        node('e', 'end', outputs={'result': ref('items', 'items'), 'answer': 'private output value'})],
        [edge('s', 'items'), edge('items', 'call'), edge('call', 'choice'),
         edge('choice', 'answer', 'yes'), edge('choice', 'e', 'else')])
    topology = client.get(base+'/topology').json()
    main = topology['flows'][pid]
    assert main['inputs'] == [{'name': 'request', 'label': '', 'type': 'string',
                              'required': True, 'description': ''}]
    assert main['outputs'] == ['answer', 'result']
    assert topology['flows'][child]['inputs'] == [{'name': 'child_input', 'label': '', 'type': 'string',
                                                 'required': True, 'description': ''}]
    assert topology['flows'][child]['outputs'] == ['child_output']
    assert topology['calls'] == [{'source': pid, 'target': child, 'node_id': 'call', 'label': 'call'}]
    assert client.get(base+'/tasks').json() == []


def test_topology_contract_accepts_empty_drafts_and_legacy_empty_definitions(configured):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    empty = client.get(base+'/topology').json()['flows'][pid]
    assert empty['inputs'] == []
    assert empty['outputs'] == []
    graph(client, pid, [node('s', 'start'), node('e', 'end')], [edge('s', 'e')])
    legacy = client.get(base+'/topology').json()['flows'][pid]
    assert legacy['inputs'] == []
    assert legacy['outputs'] == []
