"""Project agents can discover and apply the real incremental edit contract."""
import json
from types import SimpleNamespace

import jsonschema

from tests.test_projects import configured, graph, node, edge  # noqa: F401
from agent_platform.requirement_discussion import save_discussion
from agent_platform.local_agent_tools import ProjectTools
from agent_platform.connected_model import completion_events
from agent_platform.model_session import ModelSession
from agent_platform.project_agent_tools import MODELING_MANUAL, WorkspaceProjectTools, project_tool_specs


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
    help = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'lilies__workflow_draft'}})
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


def test_catalog_accepts_advertised_namespace_names_and_keeps_specialized_help(configured):
    client, _, _, base = prepare(configured)
    for spec in project_tool_specs():
        if not spec['deferLoading']:
            continue
        def query(name):
            return client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': name, 'view': 'full'}})
        plain = query(spec['name']); qualified = query('lilies__'+spec['name'])
        assert plain.status_code == qualified.status_code == 200, qualified.text
        assert plain.json() == qualified.json()
    for invalid in ['lilies__does_not_exist', 'other__workflow_draft', 'lilies__Bash']:
        response = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': invalid}})
        assert response.status_code == 422 and '未找到此工具说明' in response.text
    runtime = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'Bash'}})
    assert runtime.status_code == 200 and 'command' in runtime.json()['input_schema']['properties']


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


def test_compact_manuals_keep_config_help_and_graph_schema_builds_a_real_flow(configured):
    client, manager, pid, base = prepare(configured)
    def catalog(**arguments):
        response = client.post(base+'/agent-tools', json={'name':'block_catalog', 'arguments':arguments})
        assert response.status_code == 200, response.text
        return response.json()
    compact, full = [], []
    for kind in ('start', 'end'):
        small = catalog(block_type=kind)
        large = catalog(block_type=kind, view='full')
        assert small['manual'] == large['manual']  # No lost config, ports or examples.
        compact.append(small); full.append(large)
    shape = catalog(schema_type='workflow')
    # The previously ambiguous request has a usable answer, not an invented block.
    assert catalog(block_type='workflow') == shape
    workflow = {'nodes':[node('start','start',inputs=[{'name':'quantity','type':'number','default':1}]),
                        node('end','end',outputs={'quantity':{'$ref':{'node_id':'start','path':['quantity']}}})],
                'edges':[edge('start','end')]}
    jsonschema.validate(workflow, shape['schema'])
    for name in ('node', 'edge', 'test'):
        assert catalog(schema_type=name)['schema'] == full[0][name+'_schema']
    before = client.get('/api/v1/applications/'+pid+'/draft').json()
    edited = client.post(base+'/agent-tools', json={'name':'workflow_draft','arguments':{'operation':{
        'op':'replace_workflow','expected_revision':before['revision'],'idempotency_key':'from-compact-help','data':{'workflow':workflow}}}})
    assert edited.status_code == 200, edited.text
    for values, expected in [({},1), ({'quantity':4},4)]:
        run = client.post(base+'/agent-tools',json={'name':'workflow_run','arguments':{'action':'start','inputs':values}})
        assert run.status_code == 200 and run.json()['outputs'] == {'quantity':expected}
    # Compare useful data returned by the same two queries, including one shared
    # graph schema read. This measures payload, not model tokens or response quality.
    assert len(json.dumps(compact+[shape])) < len(json.dumps(full))
    # Non-project legacy callers retain the previous combined response by default.
    legacy = ProjectTools(manager.services, pid, manager)
    result = client.portal.call(legacy.call, 'block_catalog', {'block_type':'start'})
    assert result == full[0]


def test_project_default_catalog_matches_editor_business_blocks_and_full_keeps_legacy(configured):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    assert client.put(base + '/capabilities', json={'agent_modules_enabled': True}).status_code == 200

    def catalog(**arguments):
        response = client.post(base + '/agent-tools', json={'name': 'block_catalog', 'arguments': arguments})
        assert response.status_code == 200, response.text
        return response.json()

    available = client.get('/api/v1/blocks', params={'application_id': pid}).json()
    editor_types = {block['type'] for block in available
                    if block['block_kind'] == 'business_workflow' and not block['editor'].get('advanced')}
    compact = catalog()
    assert compact == catalog(view='compact')
    assert {block['type'] for block in compact} == editor_types
    assert {'start', 'end', 'llm', 'code', 'tool', 'if_else', 'iteration', 'human_input'} <= editor_types
    assert not {'model_turn', 'subagent_spawn', 'claude_agent', 'budget_gate'} & editor_types
    full = catalog(view='full')
    assert {block['type'] for block in full} == {block['type'] for block in available}
    assert len(json.dumps(compact)) < len(json.dumps(full))
    for block_type in ('model_turn', 'subagent_spawn', 'claude_agent'):
        manual = catalog(block_type=block_type)
        assert manual['manual']['type'] == block_type
        assert manual['manual'] == catalog(block_type=block_type, view='full')['manual']

    # The old tool surface keeps its complete listing for default and compact calls.
    manager = app.state.services.local_agents
    legacy = ProjectTools(app.state.services, pid, manager)
    for arguments in ({}, {'view': 'compact'}, {'view': 'full'}):
        assert client.portal.call(legacy.call, 'block_catalog', arguments) == full


def test_wrong_draft_selector_returns_actionable_help_without_editing(configured):
    client, _, pid, base = prepare(configured)
    before = client.get('/api/v1/applications/'+pid+'/draft').json()
    wrong = client.post(base+'/agent-tools',json={'name':'workflow_draft','arguments':{'action':'summary'}})
    assert wrong.status_code == 422 and 'view=' in wrong.text
    corrected = client.post(base+'/agent-tools',json={'name':'workflow_draft','arguments':{'view':'summary'}})
    assert corrected.status_code == 200 and corrected.json()['revision'] == before['revision']
    assert client.get('/api/v1/applications/'+pid+'/draft').json()['content_hash'] == before['content_hash']


def test_short_catalog_keeps_all_contracts_and_named_help(configured):
    client, _, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    brief = client.get(base+'/agent-tools').json()['tools']
    detailed = {spec['name']: spec for spec in project_tool_specs(detailed=True)}
    assert {spec['name'] for spec in brief} == set(detailed)
    for spec in brief:
        full = detailed[spec['name']]
        assert {k: v for k, v in spec.items() if k != 'description'} == {
            k: v for k, v in full.items() if k != 'description'}
        help = client.post(base+'/agent-tools', json={
            'name': 'block_catalog', 'arguments': {'tool_name': spec['name']}})
        assert help.status_code == 200, help.text
        expected = MODELING_MANUAL if spec['name'] == 'project_modeling' else full['description']
        assert help.json()['description'] == expected
        assert help.json()['input_schema'] == spec['inputSchema']
    assert len(json.dumps(brief)) < len(json.dumps(list(detailed.values())))


def test_api_session_can_save_and_execute_without_loading_help(configured, tmp_path):
    client, app, project, _ = configured
    pid = project['id']
    manager = app.state.services.local_agents
    workspace = WorkspaceProjectTools(app.state.services, pid, manager)
    specs = project_tool_specs()
    calls, results = [], []

    async def stream(**kwargs):
        advertised = {tool.name: tool for tool in kwargs['tools']}
        assert set(advertised) == {spec['name'] for spec in specs}
        for spec in specs:
            assert advertised[spec['name']].input_schema == spec['inputSchema']
        if len(calls) == 0:
            name, arguments = 'workflow_draft', {'view': 'summary'}
        elif len(calls) == 1:
            name, arguments = 'workflow_draft', {'operation': {
                'op': 'replace_workflow', 'expected_revision': results[0]['revision'],
                'idempotency_key': 'direct-api-edit', 'data': {'workflow': {
                    'nodes': [node('start', 'start'), node('end', 'end', outputs={'quantity': 4})],
                    'edges': [edge('start', 'end')]}}}}
        elif len(calls) == 2:
            name, arguments = 'workflow_run', {'action': 'start', 'request_key': 'direct-api-run'}
        else:
            assert results[-1]['outputs'] == {'quantity': 4}
            for item in completion_events([{'type': 'text', 'text': '数量为4'}]):
                yield item
            return
        jsonschema.validate(arguments, advertised[name].input_schema)
        for item in completion_events([{'type': 'tool_use', 'id': str(len(calls)),
                                       'name': name, 'input': arguments}], stop_reason='tool_use'):
            yield item

    async def tool(name, arguments):
        calls.append(name)
        result = await workspace.call(name, arguments)
        results.append(result)
        return result

    async def event(*args):
        pass

    async def exercise():
        session = ModelSession(SimpleNamespace(stream=stream), tmp_path/'session')
        await session.start(specs, '直接修改并运行，无需先读手册。')
        return await session.turn('返回数量4', event, tool)

    assert client.portal.call(exercise)['status'] == 'completed'
    assert calls == ['workflow_draft', 'workflow_draft', 'workflow_run']
    assert len(client.get('/api/v1/projects/'+pid+'/tasks').json()) == 1
