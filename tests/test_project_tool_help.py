"""Project agents can discover and apply the real incremental edit contract."""
from tests.test_projects import configured, graph, node, edge  # noqa: F401
from agent_platform.requirement_discussion import save_discussion


def prepare(configured):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    save_discussion(settings.workspace_root / pid, {'enabled': True, 'status': 'confirmed',
        'revision': 2, 'document': '# 已确认测试需求\n返回实际配置的数量', 'turns': []})
    result = client.put(base+'/progress', json={'expected_revision': 0, 'value': {'goal': '返回数量', 'items': [{'id': 'quantity', 'title': '数量', 'goal': '返回配置数量', 'status': 'working', 'next_action': '修改并运行'}]}})
    assert result.status_code == 200, result.text
    manager = app.state.services.local_agents
    state = manager.load(pid)
    state.update(phase='build', conversation_enabled=True, active_item_id='quantity')
    manager.save(pid, state)
    return client, manager, pid, base


def test_help_example_can_edit_a_project_member_and_run_its_changed_output(configured):
    client, manager, pid, base = prepare(configured)
    member = client.post(base+'/members', json={'name': '数量', 'purpose': 'test'}).json()['id']
    graph(client, member, [node('start', 'start'), node('end', 'end', outputs={'quantity': 1})], [edge('start', 'end')])
    help = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'workflow_draft'}})
    assert help.status_code == 200, help.text
    spec = help.json()
    assert 'workflow_id' in spec['input_schema']['properties']
    draft_path = '/api/v1/applications/'+member+'/draft'
    before = client.get(draft_path).json()
    for wrong in [{'patch': {'title': 'ignored'}}, {'config': {'outputs': {'quantity': 2}}}, {'node': {'title': 'ignored'}}]:
        response = client.post(base+'/agent-tools', json={'name': 'workflow_draft', 'arguments': {'workflow_id': member,
            'operation': {'op': 'update_node', 'expected_revision': before['revision'], 'idempotency_key': 'bad-shape',
                          'data': {'node_id': 'end', **wrong}}}})
        assert response.status_code == 422
        assert 'changes' in response.text
        assert client.get(draft_path).json()['revision'] == before['revision']
    example = next(e for e in spec['examples'] if 'batch' in e)
    example['workflow_id'] = member
    example['batch'].update(expected_revision=before['revision'], expected_content_hash=before['content_hash'])
    example['batch']['operations'] = [{'op': 'update_node', 'data': {
        'node_id': 'end', 'changes': {'config': {'outputs': {'quantity': 2}}}, 'merge_config': True}}]
    edited = client.post(base+'/agent-tools', json={'name': 'workflow_draft', 'arguments': example})
    assert edited.status_code == 200, edited.text
    run = client.post(base+'/agent-tools', json={'name': 'workflow_run', 'arguments': {'action': 'start', 'workflow_id': member}})
    assert run.status_code == 200, run.text
    assert run.json()['outputs'] == {'quantity': 2}
    assert client.get(base+'/requirements').json()['status'] == 'confirmed'


def test_old_pause_state_does_not_block_direct_editing(configured):
    client, manager, pid, base = prepare(configured)
    state = manager.load(pid)
    state.update(phase='coordinate', blocked_this_request=['quantity'])
    manager.save(pid, state)
    result = client.post(base+'/agent-tools', json={'name': 'project_file', 'arguments': {
        'action': 'write', 'path': 'solution/test.txt', 'content': 'x'}})
    assert result.status_code == 200, result.text
    assert client.get(base+'/requirements').json()['status'] == 'confirmed'
    help = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'project_action'}})
    assert help.status_code == 200
    assert 'build' in help.json()['input_schema']['properties']['action']['enum']
    runtime_help = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'Bash'}})
    assert runtime_help.status_code == 200 and 'command' in runtime_help.json()['input_schema']['properties']
    assert client.get(base+'/tasks').json() == []
